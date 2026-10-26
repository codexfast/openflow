import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from flow_mcp.operations import ProjectMediaCatalog
    from flow_mcp.pairing import Pairings
from uuid import UUID, uuid4

from pydantic import ValidationError

from flow_mcp.models import Hello, Observation, Reply, Scope, Snapshot

_REPLY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_MAX_KEYS = 32


def describe_reply(value: object) -> dict[str, object]:
    """Bounded, sanitized shape of a bridge reply that failed validation.

    Returns only *key names* and coarse type tags, never values, so a token, URL
    or prompt can never leak. Used to make an otherwise opaque
    ``invalid_response`` diagnosable from the persisted job without a new
    submission. The result is small and safe to persist.
    """
    if not isinstance(value, dict):
        return {"reply_type": type(value).__name__}
    names = sorted(name for name in value if isinstance(name, str) and _REPLY_KEY.fullmatch(name))
    shape = {
        name: ("null" if value[name] is None else
               "array" if isinstance(value[name], list) else
               "object" if isinstance(value[name], dict) else type(value[name]).__name__)
        for name in names[:_MAX_KEYS]
    }
    described: dict[str, object] = {"reply_keys": names[:_MAX_KEYS], "reply_types": shape}
    if len(names) > _MAX_KEYS:
        described["reply_keys_truncated"] = len(names)
    return described


class BridgeError(Exception):
    """Bridge failure. Optionally carries bounded, sanitized diagnostics."""

    def __init__(self, code: str, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics if diagnostics is not None else {}


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass
class Session:
    id: str
    nickname: str
    socket: Socket
    connected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future[Reply]] = field(default_factory=dict)
    selected: Scope | None = None
    generation: int = 0
    alive: bool = True
    observed_email: str | None = None


class Manager:
    def __init__(
        self, secret: str = "", timeout: float = 8.0, pairings: "Pairings | None" = None
    ) -> None:
        from flow_mcp.pairing import Pairings

        self._secret = secret
        self.pairings = pairings if pairings is not None else Pairings()
        self.timeout = timeout
        self.sessions: dict[str, Session] = {}
        self.bridge_sockets: set[Socket] = set()

    def pair(self, hello: Hello, socket: Socket, origin: str = "") -> Session:
        identity = hello.identity()
        authenticated = self.pairings.authenticate(identity, origin, hello.secret)
        legacy = (
            identity not in self.pairings.credentials
            and self._secret
            and secrets.compare_digest(hello.secret, self._secret)
        )
        if not authenticated and not legacy:
            raise BridgeError("pairing_rejected")
        if identity in self.sessions:
            raise BridgeError("duplicate_connection")
        if legacy and origin:
            self.pairings.migrate(identity, origin, hello.secret)
        session = Session(identity, hello.nickname or "Browser connection", socket)
        self.sessions[identity] = session
        return session

    def resolve(self, connection_id: str | None) -> Session:
        if connection_id is None:
            if len(self.sessions) != 1:
                raise BridgeError("connection_id_required")
            return next(iter(self.sessions.values()))
        session = self.sessions.get(connection_id)
        if session is None:
            raise BridgeError("connection_not_found")
        return session

    def invalidate(self, session: Session) -> None:
        session.selected = None
        session.observed_email = None
        session.generation += 1

    def disconnected(self, session: Session) -> None:
        session.alive = False
        self.invalidate(session)
        if self.sessions.get(session.id) is session:
            del self.sessions[session.id]
        for future in session.pending.values():
            if not future.done():
                future.set_exception(BridgeError("connection_closed"))

    async def disconnect(self, connection_id: str) -> None:
        session = self.resolve(connection_id)
        self.disconnected(session)
        await session.socket.close(1000, "disconnected")

    def receive(self, session: Session, reply: Reply) -> None:
        future = session.pending.get(reply.id)
        if future is not None and not future.done():
            future.set_result(reply)

    async def request(
        self,
        session: Session,
        operation: str,
        params: dict[str, object],
        deadline_seconds: float | None = None,
    ) -> dict[str, object]:
        if not session.alive:
            raise BridgeError("connection_closed")
        request_id = str(uuid4())
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        session.pending[request_id] = future
        try:
            async with asyncio.timeout(
                self.timeout if deadline_seconds is None else deadline_seconds
            ):
                await session.socket.send(
                    json.dumps(
                        {
                            "type": "request",
                            "id": request_id,
                            "operation": operation,
                            "params": params,
                        }
                    )
                )
                reply = await future
            if reply.error:
                self.invalidate(session)
                raise BridgeError(reply.error, {"reply_stage": "extension_error"})
            if reply.result is None:
                raise BridgeError(
                    "invalid_response",
                    {"reply_stage": "python_parse", **describe_reply(reply.model_dump())},
                )
            return reply.result
        except TimeoutError:
            self.disconnected(session)
            await session.socket.close(1000, "request_timeout")
            raise BridgeError("request_timeout") from None
        except BridgeError:
            raise
        except Exception:
            self.disconnected(session)
            raise BridgeError("connection_closed") from None
        finally:
            session.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def snapshot(self, connection_id: str | None) -> dict[str, object]:
        session = self.resolve(connection_id)
        async with session.lock:
            epoch = session.generation
            try:
                snapshot = Snapshot.model_validate(await self.request(session, "snapshot", {}))
            except ValidationError:
                self.invalidate(session)
                raise BridgeError("invalid_response") from None
            if epoch != session.generation:
                raise BridgeError("scope_changed")
            if session.selected != snapshot.selected:
                session.observed_email = None
            session.selected = snapshot.selected
            return {
                "connection_id": session.id,
                "nickname": session.nickname,
                "connected_at": session.connected_at,
                **snapshot.model_dump(),
            }

    def register_local(self, socket: Socket, identity: str) -> Session:
        if identity in self.sessions:
            raise BridgeError("duplicate_connection")
        session = Session(identity, "Managed browser", socket)
        self.sessions[identity] = session
        return session

    async def prepare_project(self, session: Session, project_id: str | None) -> None:
        prepare = getattr(session.socket, "prepare_project", None)
        if prepare is not None:
            await prepare(project_id)

    async def open_project(self, connection_id: str, project_id: str) -> dict[str, object]:
        """Open one project without dispatching a Flow operation."""
        session = self.resolve(connection_id)
        if not getattr(session.socket, "supports_project_deletion", False):
            raise BridgeError("open_project_unsupported_legacy_extension")
        try:
            project_id = str(UUID(project_id))
        except (TypeError, ValueError, AttributeError):
            raise BridgeError("invalid_project_id") from None
        async with session.lock:
            await self.prepare_project(session, project_id)
            scope = session.selected
            if (
                scope is None
                or scope.origin != "https://flow.google.com"
                or scope.project_id is None
                or scope.project_id.lower() != project_id.lower()
            ):
                raise BridgeError("project_scope_mismatch")
            epoch = session.generation
            await self.verify_scope(session, scope)
            if epoch != session.generation or session.selected != scope or not session.alive:
                raise BridgeError("scope_changed")
            return {"connection_id": session.id, "selected": scope.model_dump()}

    def browser_status(self, connection_id: str) -> dict[str, object]:
        """Return safe local browser state without inspecting page/account data."""
        session = self.resolve(connection_id)
        if not getattr(session.socket, "supports_project_deletion", False):
            return {
                "connection_id": session.id,
                "backend": "extension",
                "browser_state": "unsupported",
                "auth_state": "unknown",
                "project_id": None,
                "url": None,
            }
        status = getattr(session.socket, "browser_status", None)
        if status is None:
            raise BridgeError("browser_status_unavailable")
        result = status()
        if not isinstance(result, dict):
            raise BridgeError("invalid_browser_status")
        return {"connection_id": session.id, "backend": "browser", **result}

    async def verify_scope(self, session: Session, scope: Scope) -> None:
        verify = getattr(session.socket, "verify_scope", None)
        if verify is not None:
            await verify(scope)

    async def observe(self, session: Session, scope: Scope) -> Observation:
        """Private identity read; no credits query or account panel."""
        observe = getattr(session.socket, "observe", None)
        if not getattr(session.socket, "supports_project_deletion", False) or observe is None:
            raise BridgeError("delete_project_unsupported_managed_browser_only")
        epoch = session.generation
        try:
            await self.verify_scope(session, scope)
            result = Observation.model_validate(await observe(scope))
        except ValidationError:
            raise BridgeError("invalid_account_observation") from None
        if (epoch != session.generation or session.selected != scope or not session.alive
                or result.scope != scope):
            raise BridgeError("scope_changed")
        if not result.email or not result.email_confirmed or result.observed_at is None:
            raise BridgeError("account_evidence_required")
        return result

    async def projects(self, connection_id: str) -> dict[str, object]:
        session = self.resolve(connection_id)
        async with session.lock:
            await self.prepare_project(session, None)
            scope = session.selected
            if scope is None or scope.project_id is not None:
                raise BridgeError("select_flow_home_tab_required")
            return await self.request(
                session, "list_projects", {"scope": scope.model_dump()}, deadline_seconds=45
            )

    async def list_project_media(
        self, connection_id: str, project_id: str, limit: int = 100
    ) -> dict[str, object]:
        catalog, session = await self._read_project_media_catalog(connection_id, project_id, limit)
        return {"connection_id": session.id, **catalog.model_dump()}

    async def find_project_media(
        self, connection_id: str, project_id: str, media_id: str
    ) -> dict[str, object]:
        from pydantic import TypeAdapter

        from flow_mcp.operations import Identifier, MediaIdentifier, ProjectMediaLookup

        project_id = TypeAdapter(Identifier).validate_python(project_id)
        media_id = TypeAdapter(MediaIdentifier).validate_python(media_id)
        session = self.resolve(connection_id)
        async with session.lock:
            await self.prepare_project(session, project_id)
            scope = session.selected
            if (scope is None or scope.origin != "https://flow.google.com"
                    or scope.project_id is None
                    or scope.project_id.lower() != project_id.lower()):
                raise BridgeError("project_scope_mismatch")
            epoch = session.generation
            await self.verify_scope(session, scope)
            raw = await self.request(
                session, "find_project_media",
                {"scope": scope.model_dump(), "input": {"media_id": media_id}},
                deadline_seconds=190,
            )
            try:
                lookup = ProjectMediaLookup.model_validate(raw)
            except ValidationError:
                raise BridgeError("invalid_catalog_response") from None
            if epoch != session.generation or session.selected != scope or lookup.scope != scope:
                self.invalidate(session)
                raise BridgeError("scope_changed")
            await self.verify_scope(session, scope)
            if epoch != session.generation or session.selected != scope:
                self.invalidate(session)
                raise BridgeError("scope_changed")
            return {"connection_id": session.id, **lookup.model_dump()}

    async def _read_project_media_catalog(
        self, connection_id: str, project_id: str, limit: int
    ) -> tuple["ProjectMediaCatalog", Session]:
        from pydantic import TypeAdapter

        from flow_mcp.operations import CatalogInput, Identifier, ProjectMediaCatalog

        project_id = TypeAdapter(Identifier).validate_python(project_id)
        input = CatalogInput(limit=limit)
        session = self.resolve(connection_id)
        async with session.lock:
            await self.prepare_project(session, project_id)
            scope = session.selected
            if (scope is None or scope.origin != "https://flow.google.com"
                    or scope.project_id is None
                    or scope.project_id.lower() != project_id.lower()):
                raise BridgeError("project_scope_mismatch")
            epoch = session.generation
            await self.verify_scope(session, scope)
            raw = await self.request(
                session, "list_project_media",
                {"scope": scope.model_dump(), "input": input.model_dump()},
                deadline_seconds=190,
            )
            try:
                catalog = ProjectMediaCatalog.model_validate(raw)
            except ValidationError:
                raise BridgeError("invalid_catalog_response") from None
            if epoch != session.generation or session.selected != scope or catalog.scope != scope:
                self.invalidate(session)
                raise BridgeError("scope_changed")
            await self.verify_scope(session, scope)
            if epoch != session.generation or session.selected != scope:
                self.invalidate(session)
                raise BridgeError("scope_changed")
            if len(catalog.items) > input.limit:
                raise BridgeError("invalid_catalog_response")
            return catalog, session

    async def select(self, connection_id: str | None, tab_id: int) -> dict[str, object]:
        session = self.resolve(connection_id)
        async with session.lock:
            self.invalidate(session)
            epoch = session.generation
            try:
                scope = Scope.model_validate(
                    await self.request(session, "select", {"tab_id": tab_id})
                )
            except ValidationError:
                raise BridgeError("invalid_response") from None
            if epoch != session.generation or scope.tab_id != tab_id:
                raise BridgeError("scope_changed")
            session.selected = scope
            return {"connection_id": session.id, "selected": scope.model_dump()}

    async def account(
        self, connection_id: str | None, refresh: bool = False
    ) -> dict[str, object]:
        session = self.resolve(connection_id)
        if session.selected is None:
            await self.snapshot(session.id)
        async with session.lock:
            scope = session.selected
            if scope is None:
                raise BridgeError("select_flow_tab_required")
            epoch = session.generation
            try:
                result = Observation.model_validate(
                    await self.request(
                        session,
                        "refresh_account" if refresh else "inspect",
                        {"scope": scope.model_dump()},
                    )
                )
            except ValidationError:
                self.invalidate(session)
                raise BridgeError("invalid_response") from None
            if epoch != session.generation or result.scope != scope:
                self.invalidate(session)
                raise BridgeError("scope_changed")
            if (
                session.observed_email is not None
                and result.email is not None
                and session.observed_email != result.email.lower()
            ):
                self.invalidate(session)
                raise BridgeError("scope_changed")
            if result.email is not None:
                session.observed_email = result.email.lower()
            return {
                "connection_id": session.id,
                **result.model_dump(),
                "checked_at": datetime.now(UTC).isoformat(),
                "freshness": "observed" if result.observed_at else "unknown",
                "credits_freshness": "observed" if result.credits is not None else "unknown",
                "account_switch_detection": "on account inspection only",
            }
