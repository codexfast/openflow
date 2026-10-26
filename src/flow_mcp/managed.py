from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from flow_mcp.browser import ACCOUNT_PROBE, BrowserError, browser_session, classify_state
from flow_mcp.manager import BridgeError, Manager, Session
from flow_mcp.models import Observation, Reply, Scope

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

# Only visible, unambiguous account evidence; no opening panels or extracting cookies.
ACCOUNT = r"""() => {
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let n = node; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (n.hidden || n.getAttribute('aria-hidden') === 'true' || s.display === 'none' ||
          ['hidden', 'collapse'].includes(s.visibility) || s.opacity === '0') return false;
    }
    return true;
  };
  const emails = new Set(); let ambiguous = false;
  for (const node of document.querySelectorAll(
      'button[aria-label],a[aria-label],[role="button"][aria-label]')) {
    const label = node.getAttribute('aria-label') || '';
    if (!visible(node) || !/^(?:Google Account|Conta do Google):\s/i.test(label)) continue;
    const matches = label.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
    if (matches.length !== 1) ambiguous = true;
    else emails.add(matches[0].toLowerCase());
  }
  return {email: !ambiguous && emails.size === 1 ? [...emails][0] : null,
    observed_at: new Date().toISOString()};
}"""
READY = (
    "() => typeof globalThis.WIZ_global_data?.SNlM0e === 'string' && "
    "!!globalThis.WIZ_global_data.SNlM0e"
)


def runtime_source() -> str:
    resource = files("flow_mcp").joinpath("operations.mjs")
    if resource.is_file():
        source = resource.read_text(encoding="utf-8")
    else:
        source = (Path(__file__).resolve().parents[2] / "extension" / "operations.mjs").read_text(
            encoding="utf-8"
        )
    # The source is our bundled module, never downloaded page text.
    source = re.sub(r"\bexport (?=(?:async )?function|const )", "", source)
    return (
        "async ([operation, params]) => {\n"
        + source
        + ("\nreturn await flowOperation(operation, params, knownOperationErrors.source);\n}")
    )


def init_script_source() -> str:
    resource = files("flow_mcp").joinpath("hijack_bypass.js")
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    return (
        Path(__file__).resolve().parents[2] / "extension" / "hijack_bypass.js"
    ).read_text(encoding="utf-8")


class ManagedBrowser:
    supports_project_deletion = True

    def __init__(self, manager: Manager, page: Page) -> None:
        self.manager = manager
        self.page = page
        self.session: Session | None = None
        self.document_id = str(uuid4())
        self.source = runtime_source()
        self.prefix = ""
        self.email: str | None = None
        self.closed = False
        page.on("framenavigated", self.navigated)
        page.on("close", self.page_closed)

    def navigated(self, frame: Frame) -> None:
        if frame != self.page.main_frame:
            return
        self.document_id = str(uuid4())
        if self.session is not None:
            self.manager.invalidate(self.session)

    def page_closed(self, *_: object) -> None:
        if self.session is not None and self.session.alive:
            self.manager.disconnected(self.session)

    def scope(self) -> Scope:
        state = classify_state(self.page.url, False, True)
        if state.flow_origin != "https://flow.google.com":
            raise BridgeError("flow_login_required_run_openflow_setup")
        return Scope(
            tab_id=1,
            origin="https://flow.google.com",
            project_id=state.project_id,
            document_id=self.document_id,
        )

    def browser_status(self) -> dict[str, object]:
        """Local state only. Never query browser storage or return account evidence."""
        if self.closed:
            return {
                "browser_state": "closed",
                "auth_state": "unknown",
                "project_id": None,
                "url": None,
            }
        try:
            current_url = str(self.page.url)
            parsed = urlsplit(current_url)
            state = classify_state(current_url, self.email is not None, True)
        except (TypeError, ValueError):
            return {
                "browser_state": "unknown",
                "auth_state": "unknown",
                "project_id": None,
                "url": None,
            }
        # Deliberately omit userinfo, port, query, and fragment.
        url = f"{state.flow_origin}{parsed.path}" if state.flow_origin else None
        return {
            "browser_state": "ready" if state.flow_origin else state.state,
            "auth_state": "observed" if (
                self.email is not None and self.session is not None
                and self.session.selected is not None
                and self.session.selected.project_id == state.project_id
                and self.session.selected.document_id == self.document_id
                and state.flow_origin == "https://flow.google.com"
            ) else ("login_required" if state.state == "login_required" else "unknown"),
            "project_id": state.project_id,
            "url": url,
        }

    async def ready(self) -> None:
        from playwright.async_api import Error

        try:
            await self.page.wait_for_function(READY, timeout=30_000)
            await self.page.wait_for_function(ACCOUNT_PROBE, timeout=15_000)
        except Error:
            raise BridgeError("flow_not_ready_run_openflow_setup") from None
        path = urlsplit(self.page.url).path
        match = re.match(r"^(/u/\d+)(?:/|$)", path)
        self.prefix = match.group(1) if match else ""
        self.scope()

    async def observe(self, scope: Scope) -> Observation:
        evidence: Any = await self.page.evaluate(ACCOUNT)
        email = evidence.get("email") if isinstance(evidence, dict) else None
        if self.email is not None and email != self.email:
            if self.session is not None:
                self.manager.invalidate(self.session)
            raise BridgeError("scope_changed")
        if email is not None:
            self.email = email
        return Observation(
            scope=scope,
            email=email,
            email_confirmed=email is not None,
            source="google_account_aria" if email else "unavailable",
            observed_at=evidence["observed_at"] if email else None,
        )

    async def verify_scope(self, scope: Scope) -> None:
        if self.scope() != scope:
            if self.session is not None:
                self.manager.invalidate(self.session)
            raise BridgeError("scope_changed")
        observation = await self.observe(scope)
        if observation.email is None:
            raise BridgeError("account_evidence_required")
        if self.scope() != scope:
            raise BridgeError("scope_changed")

    async def prepare_project(self, project_id: str | None) -> None:
        if project_id is not None:
            UUID(project_id)
        assert self.session is not None
        try:
            current = self.scope()
        except BridgeError:
            current = None
        if current is None or current.project_id != project_id:
            url = "https://flow.google.com" + self.prefix
            url += f"/project/{project_id}" if project_id else "/"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self.ready()
        scope = self.scope()
        if scope.project_id != project_id:
            raise BridgeError("project_scope_mismatch")
        await self.verify_scope(scope)
        self.session.selected = scope

    async def send(self, message: str) -> None:
        assert self.session is not None
        request = json.loads(message)
        try:
            result = await self.dispatch(request["operation"], request["params"])
            reply = Reply(type="reply", id=request["id"], result=result)
        except BridgeError as error:
            code: Literal[
                "scope_changed", "tab_unavailable", "invalid_request", "inspection_failed"
            ]
            if error.code == "scope_changed":
                code = "scope_changed"
            elif error.code == "tab_unavailable":
                code = "tab_unavailable"
            elif error.code == "invalid_request":
                code = "invalid_request"
            else:
                code = "inspection_failed"
            reply = Reply(type="reply", id=request["id"], error=code)
        self.manager.receive(self.session, reply)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.closed = True
            await self.page.context.close()

    async def dispatch(self, operation: str, params: dict[str, Any]) -> dict[str, object]:
        scope = self.scope()
        if operation == "snapshot":
            return {
                "tabs": [scope.model_dump(exclude={"document_id"})],
                "selected": scope.model_dump(),
            }
        if operation == "select":
            if params.get("tab_id") != 1:
                raise BridgeError("tab_unavailable")
            return scope.model_dump()
        requested = Scope.model_validate(params["scope"])
        await self.verify_scope(requested)
        if operation in {"inspect", "refresh_account"}:
            from playwright.async_api import Error

            credits = None
            try:
                async with asyncio.timeout(15):
                    balance: Any = await self.page.evaluate(
                        self.source, ["get_credits", {"scope": requested.model_dump()}]
                    )
                if (
                    isinstance(balance, dict)
                    and balance.get("state") == "completed"
                    and balance.get("scope") == requested.model_dump()
                    and type(balance.get("credits")) is int
                    and 0 <= balance["credits"] <= 9007199254740991
                ):
                    credits = balance["credits"]
            except (TimeoutError, Error):
                pass  # No cached balance: an unavailable read must remain unknown.
            await self.verify_scope(requested)
            observation = await self.observe(requested)
            if observation.email is not None and credits is not None:
                observation = Observation(
                    **{
                        **observation.model_dump(),
                        "credits": credits,
                        "credits_source": "flow_credits_rpc",
                    }
                )
            return observation.model_dump()
        allowed = {
            "list_projects",
            "list_project_media",
            "find_project_media",
            "create_project",
            "delete_project",
            "probe",
            "poll",
            "upload",
            "generate_image",
            "generate_video",
            "upscale_image",
            "upscale_video",
        }
        if operation not in allowed:
            raise BridgeError("invalid_request")
        if (operation not in {"list_projects", "create_project", "delete_project"}
                and requested.project_id is None):
            raise BridgeError("project_scope_mismatch")
        if operation == "delete_project":
            from flow_mcp.operations import DeleteProjectInput

            if requested.project_id is not None or params.get("page_scope") is not None:
                raise BridgeError("invalid_request")
            try:
                DeleteProjectInput.model_validate(params.get("input"))
            except ValueError:
                raise BridgeError("invalid_request") from None
        result: Any = await self.page.evaluate(
            self.source, [operation, {**params, "diagnostics": True}]
        )
        await self.verify_scope(requested)
        if not isinstance(result, dict):
            raise BridgeError("invalid_response")
        return dict(result)


@asynccontextmanager
async def managed_browser(manager: Manager) -> AsyncIterator[None]:
    async with browser_session(True) as context:
        await context.add_init_script(init_script_source())
        page = context.pages[0] if context.pages else await context.new_page()
        driver = ManagedBrowser(manager, page)
        await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30_000)
        await driver.ready()
        scope = driver.scope()
        observation = await driver.observe(scope)
        if observation.email is None:
            raise BrowserError("Flow login unavailable. Close the server and run: openflow setup")
        driver.session = manager.register_local(driver, str(uuid4()))
        driver.session.selected = scope
        try:
            yield None
        finally:
            if driver.session.alive:
                manager.disconnected(driver.session)


@asynccontextmanager
async def backend_lifespan(manager: Manager, port: int, backend: str) -> AsyncIterator[None]:
    if backend == "extension":
        from flow_mcp.bridge import bridge

        async with bridge(manager, port):
            yield None
    else:
        async with managed_browser(manager):
            yield None
