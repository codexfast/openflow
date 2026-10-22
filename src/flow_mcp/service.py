import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import re
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from flow_mcp.jobs import Jobs
from flow_mcp.manager import BridgeError, Manager, Session
from flow_mcp.models import Scope, StrictModel
from flow_mcp.operations import (
    BRIDGE_OR_RESPONSE_VALIDATION,
    DeleteProjectInput,
    DeleteProjectReply,
    Outcome,
    PollInput,
    ProjectTitle,
    RpcDiagnostics,
    UploadInput,
)

MAX_UPLOAD = 8 * 1024 * 1024


def upload_input(file_path: str | None, data_base64: str | None, name: str) -> UploadInput:
    if (file_path is None) == (data_base64 is None):
        raise ValueError("provide_exactly_one_local_file_or_base64")
    if file_path is not None:
        path = local_path(file_path, exists=True)
        if not path.is_file() or path.stat().st_size > MAX_UPLOAD:
            raise ValueError("upload_requires_regular_file_max_8MiB")
        with path.open("rb") as stream:
            data = stream.read(MAX_UPLOAD + 1)
    else:
        if data_base64 is None or len(data_base64) > 11184812:
            raise ValueError("upload_too_large")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid_base64") from None
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("upload_requires_1_to_8MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("only_png_jpeg_webp_supported")
    return UploadInput.model_validate(
        {
            "data": base64.b64encode(data).decode("ascii"),
            "mime": mime,
            "name": name,
        }
    )


def local_path(value: str, exists: bool) -> Path:
    if not value or value.startswith(("\\\\", "//")) or "\x00" in value:
        raise ValueError("local_absolute_path_required")
    path = Path(value)
    if not path.is_absolute() or ":" in value[2:]:
        raise ValueError("local_absolute_path_required")
    resolved = path.resolve(strict=exists)
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("network_paths_forbidden")
    return resolved


def media_url(value: str, media: str, kind: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.hostname != "flow-content.google"
        or url.netloc != "flow-content.google"
        or url.fragment
        or url.path != f"/{kind}/{media}"
        or not re.fullmatch(
            r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}(?:_upsampled)?",
            media,
        )
        or not any(parse_qs(url.query).get(key) for key in ("Signature", "X-Goog-Signature"))
        or any(ord(char) < 33 for char in value)
    ):
        raise ValueError("unsafe_generated_url")
    return value


def inspect_media(data: bytes, kind: str) -> dict[str, object]:
    mime = None
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        mime = "image/png"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif len(data) >= 16 and data[4:8] == b"ftyp":
        mime = "video/mp4"
    if mime is None or not mime.startswith(kind + "/"):
        raise ValueError("invalid_media_signature")
    return {"mime": mime, "signature_verified": True, "width": width, "height": height,
            "quality_verified": False, "decode_verified": False}


async def file_metadata(path: Path, kind: str) -> dict[str, object]:
    metadata = inspect_media(await asyncio.to_thread(path.read_bytes), kind)
    executable = shutil.which("ffprobe")
    metadata["metadata_verified"] = False
    if executable is None:
        metadata["probe_status"] = "ffprobe_unavailable"
        return metadata
    process = await asyncio.create_subprocess_exec(
        executable, "-v", "error", "-protocol_whitelist", "file", "-show_entries",
        "stream=codec_name,codec_type,width,height,duration:format=duration",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    try:
        payload = json.loads(output)
        streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
        if process.returncode != 0 or not streams:
            raise ValueError
        stream = streams[0]
        metadata.update({"width": stream.get("width"), "height": stream.get("height"),
                         "codec": stream.get("codec_name"),
                         "duration": payload.get("format", {}).get("duration"),
                         "metadata_verified": True, "probe_status": "completed"})
    except (ValueError, TypeError, AttributeError):
        metadata["probe_status"] = "invalid_or_unrecognized_media"
    return metadata


class Operations:
    def __init__(self, manager: Manager, jobs: Jobs) -> None:
        self.manager = manager
        self.jobs = jobs

    def binding(self, connection_id: str, project_id: str) -> tuple[Session, Scope]:
        session = self.manager.resolve(connection_id)
        scope = session.selected
        if scope is None:
            raise BridgeError("select_flow_tab_required")
        if scope.origin != "https://flow.google.com" or scope.project_id != project_id:
            raise BridgeError("project_scope_mismatch")
        return session, scope.model_copy()

    def public(self, job: dict[str, Any]) -> dict[str, object]:
        value = dict(job)
        value.pop("input")
        value["result"] = dict(value["result"])
        if value["result"].pop("encoded_image", None):
            value["result"]["inline_image_available"] = True
            value["result"]["download_available"] = value["state"] == "completed"
            value["result"]["download_asset_index"] = 0
        return value

    @staticmethod
    def safe_diagnostics(result: dict[str, object]) -> dict[str, object] | None:
        value = result.get("diagnostics")
        if not isinstance(value, dict):
            return None
        allowed = {name: value[name] for name in RpcDiagnostics.model_fields if name in value}
        try:
            parsed = RpcDiagnostics.model_validate(allowed).model_dump(exclude_none=True)
        except ValidationError:
            return None
        return dict(parsed) or None

    @staticmethod
    def outcome(result: dict[str, object]) -> Outcome:
        """Parse a bridge reply into a validated :class:`Outcome`.

        Strict about the fields that carry meaning (``scope``/``state``/assets), but
        tolerant of *additional* envelope metadata an extension build may attach
        (``id`` echoed back, ``diagnostics``, ``rpc_verified``, new keys added by a
        newer Flow UI). Dropping unknown extras here is deliberate: a reply that is
        otherwise well formed must not collapse into a blanket ``invalid_response``
        merely because the extension learned a new field. The ignored key *names*
        are surfaced as sanitized diagnostics by the caller, never their values, so
        no token, URL or prompt can leak.
        """
        fields = {name: result[name] for name in Outcome.model_fields if name in result}
        return Outcome.model_validate(fields)

    @staticmethod
    def unexpected_keys(result: dict[str, object]) -> list[str]:
        allowed = set(Outcome.model_fields) | {"diagnostics", "id"}
        return sorted(
            name for name in result
            if name not in allowed and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name)
        )

    async def submit(
        self,
        operation: str,
        connection_id: str,
        project_id: str,
        value: StrictModel,
        confirm: bool,
        preview_id: str | None,
    ) -> dict[str, object]:
        if operation not in {"upload", "generate_image", "generate_video",
                             "upscale_image", "upscale_video"}:
            raise ValueError("unsupported_operation")
        session = self.manager.resolve(connection_id)
        async with session.lock:
            if not confirm:
                await self.manager.prepare_project(session, project_id)
            session, scope = self.binding(connection_id, project_id)
            await self.manager.verify_scope(session, scope)
            data = value.model_dump()
            stored = dict(data)
            if operation == "upload":
                stored.pop("data")
                stored["sha256"] = hashlib.sha256(data["data"].encode("ascii")).hexdigest()
                stored["encoded_size"] = len(data["data"])
            if operation.startswith("upscale_") and not self.jobs.owns(
                connection_id, scope, data["media_id"],
                "video" if operation == "upscale_video" else "image",
            ):
                raise BridgeError("reference_not_owned_by_exact_scope_upload_first")
            refs = list(data.get("references", []))
            refs.extend(data[key] for key in ("start", "end") if data.get(key))
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate_references")
            for media in refs:
                if not self.jobs.owns(connection_id, scope, media):
                    raise BridgeError("reference_not_owned_by_exact_scope_upload_first")
            if session.selected != scope or not session.alive:
                raise BridgeError("scope_changed")
            if not confirm:
                identity = self.jobs.create(connection_id, scope, operation, stored)
                return {
                    **self.public(self.jobs.get(identity)),
                    "preview": stored,
                    "confirmation_required": True,
                    "may_consume_credits": operation != "generate_image",
                }
            if preview_id is None:
                raise ValueError("preview_id_required")
            job = self.jobs.get(preview_id)
            if (
                job["connection_id"] != connection_id
                or job["scope"] != scope.model_dump()
                or job["operation"] != operation
                or job["input"] != stored
            ):
                raise BridgeError("preview_scope_or_input_mismatch")
            if job["state"] != "preview":
                return {**self.public(job), "not_resubmitted": True}
            self.jobs.update(preview_id, "submitting", {})
            epoch = session.generation
            raw: dict[str, object] | None = None
            try:
                raw = await self.manager.request(
                    session,
                    operation,
                    {"scope": scope.model_dump(), "input": data},
                    deadline_seconds=210,
                )
                safe = self.safe_diagnostics(raw)
                extras = self.unexpected_keys(raw)
                if extras:
                    safe = {**(safe or {}), "reply_extra": extras}
                result = self.outcome(raw)
                if result.scope != scope or epoch != session.generation:
                    raise BridgeError("scope_changed")
                self.validate_assets(result)
                if result.encoded_image:
                    if operation != "upscale_image":
                        raise ValueError("unexpected_inline_image")
                    inspect_media(base64.b64decode(result.encoded_image, validate=True), "image")
                if result.state == "completed" and not (result.assets or result.encoded_image):
                    raise ValueError("completed_result_requires_assets")
                if result.state == "pending" and not (result.operation_id or result.media_id):
                    raise ValueError("pending_result_requires_remote_handle")
                stored_result = result.model_dump()
                if safe is not None:
                    stored_result["diagnostics"] = safe
                self.jobs.update(preview_id, result.state, stored_result)
            except asyncio.CancelledError:
                self.jobs.update(preview_id, "submitted_unverified", {"error": "cancelled"})
                raise
            except Exception as error:
                failure: dict[str, object] = {
                    "stage": BRIDGE_OR_RESPONSE_VALIDATION,
                    "code": self.failure_code(error),
                }
                safe = self.safe_diagnostics(raw) if isinstance(raw, dict) else None
                extras = self.unexpected_keys(raw) if isinstance(raw, dict) else []
                if extras:
                    safe = {**(safe or {}), "reply_extra": extras}
                if isinstance(error, BridgeError) and error.diagnostics:
                    safe = {**(safe or {}), **error.diagnostics}
                if safe is not None:
                    failure["diagnostics"] = safe
                if isinstance(error, BridgeError) and str(error) == "request_timeout":
                    failure["submission"] = "outcome_unknown"
                self.jobs.update(
                    preview_id,
                    "submitted_unverified",
                    {
                        "error": "submission_outcome_unknown_do_not_retry",
                        "failure": failure,
                    },
                )
            return self.public(self.jobs.get(preview_id))

    @staticmethod
    def failure_code(error: Exception) -> str:
        """Map a submission failure to a bounded, non-sensitive code.

        Validation failures keep a specific, redacted cause so an ``invalid_response``
        can be diagnosed from the persisted job instead of collapsing every shape
        mismatch into the same opaque code.
        """
        if isinstance(error, BridgeError):
            code = error.code
            if code == "invalid_response":
                stage = error.diagnostics.get("reply_stage")
                if isinstance(stage, str) and stage:
                    return f"invalid_response:{stage}"
                return "invalid_response"
            known = {
                "scope_changed", "tab_unavailable", "invalid_request", "inspection_failed",
                "invalid_response", "request_timeout", "connection_closed",
            }
            return code if code in known else BRIDGE_OR_RESPONSE_VALIDATION
        if isinstance(error, ValidationError):
            details: list[dict[str, object]] = [dict(item) for item in error.errors()]
            first = details[0] if details else {}
            raw_location = first.get("loc", ())
            parts = raw_location if isinstance(raw_location, (list, tuple)) else ()
            location = ".".join(str(part) for part in parts) or "reply"
            kind = "extra" if first.get("type") == "extra_forbidden" else "shape"
            return f"invalid_response:{kind}:{location[:64]}" if location else "invalid_response"
        if isinstance(error, ValueError):
            message = str(error)
            return f"invalid_response:{message[:64]}" if message else "invalid_response"
        return BRIDGE_OR_RESPONSE_VALIDATION

    async def create_project(
        self, connection_id: str, confirm: bool, preview_id: str | None,
        diagnostics: bool = False,
        title: ProjectTitle = "OpenFlow project",
    ) -> dict[str, object]:
        title = TypeAdapter(ProjectTitle).validate_python(title)
        project_input = {"transport": "jHPbke", "title": title}
        if confirm:
            if preview_id is None:
                raise ValueError("preview_id_required")
            previous = self.jobs.get(preview_id)
            if (
                previous["connection_id"] != connection_id
                or previous["operation"] != "create_project"
                or previous["input"] != project_input
            ):
                raise BridgeError("preview_scope_or_input_mismatch")
            if previous["state"] != "preview":
                return {**self.public(previous), "not_resubmitted": True}
        session = self.manager.resolve(connection_id)
        async with session.lock:
            if not confirm:
                await self.manager.prepare_project(session, None)
            scope = session.selected
            if scope is None or scope.origin != "https://flow.google.com" or scope.project_id:
                raise BridgeError("select_flow_home_tab_required")
            if not confirm:
                identity = self.jobs.create(
                    connection_id, scope, "create_project",
                    project_input,
                )
                return {
                    **self.public(self.jobs.get(identity)),
                    "confirmation_required": True,
                    "may_consume_credits": False,
                    "preview": {
                        "rpc": "jHPbke", "title": title,
                        "navigation": False, "paid_generation": False,
                    },
                }
            assert preview_id is not None
            job = self.jobs.get(preview_id)
            if (
                job["scope"] != scope.model_dump()
                or job["input"] != project_input
            ):
                raise BridgeError("preview_scope_or_input_mismatch")
            if job["state"] != "preview":
                return {**self.public(job), "not_resubmitted": True}
            self.jobs.update(preview_id, "submitting", {})
            epoch = session.generation
            try:
                result = await self.manager.request(
                    session, "create_project",
                    {"scope": scope.model_dump(), "title": title,
                     **({"diagnostics": True} if diagnostics else {})},
                    deadline_seconds=210
                )
                reply_scope = Scope.model_validate(result.get("scope"))
                if reply_scope != scope or epoch != session.generation:
                    raise BridgeError("scope_changed")
                state = result.get("state")
                if state == "completed":
                    selected = Scope.model_validate(result.get("selected"))
                    if (
                        selected.tab_id != scope.tab_id
                        or selected.origin != scope.origin
                        or selected.document_id != scope.document_id
                        or not selected.project_id
                    ):
                        raise BridgeError("scope_changed")
                    from uuid import UUID

                    UUID(selected.project_id)
                    session.selected = selected
                    session.observed_email = None
                    session.generation += 1
                elif state not in {"not_submitted", "submitted_unverified"}:
                    raise BridgeError("submission_outcome_unknown_do_not_retry")
                safe_result: dict[str, object] = {"scope": scope.model_dump(), "state": state}
                if state == "completed":
                    safe_result["selected"] = selected.model_dump()
                else:
                    import re

                    error = result.get("error")
                    safe_result["error"] = error if isinstance(error, str) and re.fullmatch(
                        r"(?:scope_changed|page_auth_unavailable|account_evidence_required|"
                        r"rpc_(?:http_error|timeout|"
                        r"transport_error|body_read_error|rejected|envelope_missing)|"
                        r"invalid_(?:batch_response|rpc_payload|project_response|project_scope)|"
                        r"response_too_large|page_operation_failed)", error
                    ) else "page_operation_failed"
                if diagnostics:
                    safe = self.safe_diagnostics(result)
                    if safe is not None:
                        safe_result["diagnostics"] = safe
                if state == "submitted_unverified":
                    self.manager.invalidate(session)
                self.jobs.update(preview_id, str(state), safe_result)
            except asyncio.CancelledError:
                self.manager.invalidate(session)
                self.jobs.update(preview_id, "submitted_unverified", {"error": "cancelled"})
                raise
            except Exception as error:
                self.manager.invalidate(session)
                failure: dict[str, object] = {"stage": BRIDGE_OR_RESPONSE_VALIDATION,
                                              "code": self.failure_code(error)}
                if isinstance(error, BridgeError) and error.diagnostics:
                    failure["diagnostics"] = dict(error.diagnostics)
                self.jobs.update(
                    preview_id, "submitted_unverified",
                    {"error": "submission_outcome_unknown_do_not_retry",
                     "failure": failure},
                )
            completed = self.jobs.get(preview_id)
            if completed["state"] == "completed":
                try:
                    await self.manager.prepare_project(session, selected.project_id)
                except Exception:
                    return {
                        **self.public(completed), "navigation_error": "project_navigation_failed"
                    }
            return self.public(completed)

    async def delete_project(
        self, connection_id: str, project_id: str, confirm: bool = False,
        preview_id: str | None = None,
    ) -> dict[str, object]:
        # Separate from generation: destructive, home-scoped, verified account required.
        from uuid import UUID

        UUID(project_id)
        previous = None
        if confirm:
            if preview_id is None:
                raise ValueError("preview_id_required")
            previous = self.jobs.get(preview_id)
            if (previous["connection_id"] != connection_id
                    or previous["operation"] != "delete_project"
                    or previous["input"].get("project_id") != project_id):
                raise BridgeError("preview_scope_or_input_mismatch")
            if previous["state"] != "preview":
                return {**self.public(previous), "not_resubmitted": True}
        session = self.manager.resolve(connection_id)
        if not getattr(session.socket, "supports_project_deletion", False):
            raise BridgeError("delete_project_unsupported_managed_browser_only")
        async with session.lock:
            if self.jobs.delete_in_flight(connection_id, project_id):
                raise BridgeError("delete_project_outcome_unknown_do_not_retry")
            if not confirm:
                await self.manager.prepare_project(session, None)
            selected = session.selected
            if (selected is None or selected.origin != "https://flow.google.com"
                    or selected.project_id is not None):
                raise BridgeError("select_flow_home_tab_required")
            scope = selected.model_copy()
            epoch = session.generation
            if previous is not None and previous["scope"] != scope.model_dump():
                raise BridgeError("preview_scope_or_input_mismatch")
            observation = await self.manager.observe(session, scope)
            assert observation.email is not None
            account = hashlib.sha256(observation.email.lower().encode()).hexdigest()
            listing = await self.manager.request(
                session, "list_projects", {"scope": scope.model_dump()}, deadline_seconds=45,
            )
            projects = listing.get("projects")
            if (listing.get("state") != "completed" or listing.get("scope") != scope.model_dump()
                    or not isinstance(projects, list)):
                raise BridgeError("invalid_project_response")
            matches = [item for item in projects if isinstance(item, dict)
                       and item.get("project_id") == project_id]
            if len(matches) != 1:
                # GetProjects is capped at 100; absence is NOT proof of nonexistence.
                raise BridgeError("delete_target_not_found_in_limited_list")
            title = matches[0].get("title")
            if not isinstance(title, str) or len(title) >= 200:
                raise BridgeError("delete_title_truncated_or_invalid")
            data = DeleteProjectInput.model_validate({
                "project_id": project_id, "expected_title": title,
                "account_sha256": account,
            }).model_dump()
            # Identity/scope checked again after listing, immediately before dispatch.
            current = await self.manager.observe(session, scope)
            assert current.email is not None
            if (epoch != session.generation or session.selected != scope
                    or hashlib.sha256(current.email.lower().encode()).hexdigest() != account):
                raise BridgeError("scope_or_account_changed")
            if not confirm:
                identity = self.jobs.create_delete_preview(connection_id, scope, data)
                return {
                    **self.public(self.jobs.get(identity)), "confirmation_required": True,
                    "may_consume_credits": False,
                    "preview": {"rpc": "QI2zvc", "project_id": project_id,
                                "title": data["expected_title"], "irreversible": True,
                                "managed_only": True},
                }
            assert preview_id is not None
            job = self.jobs.get(preview_id)
            if job["input"] != data or job["scope"] != scope.model_dump():
                raise BridgeError("preview_scope_account_or_title_mismatch")
            if job["state"] != "preview":
                return {**self.public(job), "not_resubmitted": True}
            if not self.jobs.claim_delete(preview_id, connection_id, project_id):
                return {**self.public(self.jobs.get(preview_id)), "not_resubmitted": True}
            try:
                raw = await self.manager.request(
                    session, "delete_project", {"scope": scope.model_dump(), "input": data},
                    deadline_seconds=210,
                )
                result = DeleteProjectReply.model_validate({
                    key: raw[key] for key in DeleteProjectReply.model_fields if key in raw
                })
                if result.scope != scope or epoch != session.generation:
                    raise BridgeError("scope_changed")
                if result.state == "completed":
                    if result.deleted_project_id != project_id or result.error is not None:
                        raise ValueError("invalid_delete_response")
                    stored: dict[str, object] = {
                        "scope": scope.model_dump(), "state": "completed",
                        "deleted_project_id": project_id,
                    }
                else:
                    # No untrusted text (or account material) enters public job results.
                    stored = {"error": "delete_not_submitted" if result.state == "not_submitted"
                              else "submission_outcome_unknown_do_not_retry"}
                self.jobs.update(preview_id, result.state, stored)
                if result.state != "not_submitted":
                    self.manager.invalidate(session)
            except asyncio.CancelledError:
                self.manager.invalidate(session)
                self.jobs.update(preview_id, "submitted_unverified", {"error": "cancelled"})
                raise
            except Exception:
                self.manager.invalidate(session)
                self.jobs.update(preview_id, "submitted_unverified", {
                    "error": "submission_outcome_unknown_do_not_retry",
                })
            return self.public(self.jobs.get(preview_id))

    def validate_assets(self, result: Outcome) -> None:
        for asset in result.assets:
            if asset.url is not None:
                media_url(asset.url, asset.media_id, asset.kind)

    async def get(
        self,
        identity: str,
        connection_id: str,
        project_id: str,
        refresh: bool,
    ) -> dict[str, object]:
        job = self.jobs.get(identity)
        if job["connection_id"] != connection_id or (
            job["input"].get("project_id") if job["operation"] == "delete_project"
            else job["scope"]["project_id"]
        ) != project_id:
            raise BridgeError("job_scope_mismatch")
        if job["operation"] == "delete_project":
            return self.public(job)
        if not refresh or job["state"] in {"preview", "completed", "not_submitted"}:
            return self.public(job)
        session, scope = self.binding(connection_id, project_id)
        if job["scope"] != scope.model_dump():
            raise BridgeError("job_document_mismatch_no_rebinding")
        previous = job["result"]
        if not previous.get("operation_id") and not previous.get("media_id"):
            return {**self.public(job), "poll_unavailable": "no_verified_remote_handle"}
        async with session.lock:
            if session.selected != scope:
                raise BridgeError("scope_changed")
            epoch = session.generation
            value = PollInput(
                operation_id=previous.get("operation_id"),
                media_id=previous.get("media_id"),
                kind=("video" if job["operation"] in {"generate_video", "upscale_video"}
                      else "image"),
            )
            result = Outcome.model_validate(
                await self.manager.request(
                    session,
                    "poll",
                    {"scope": scope.model_dump(), "input": value.model_dump()},
                    deadline_seconds=210,
                )
            )
            if result.scope != scope or session.generation != epoch:
                raise BridgeError("scope_changed")
            self.validate_assets(result)
            if result.error:
                return {**self.public(job), "poll_error": result.error}
            merged = result.model_dump()
            for key in ("operation_id", "media_id"):
                merged[key] = merged[key] or previous.get(key)
            self.jobs.update(identity, result.state, merged)
            return self.public(self.jobs.get(identity))

    async def download(
        self,
        identity: str,
        connection_id: str,
        project_id: str,
        destination: str,
        index: int,
    ) -> dict[str, object]:
        await self.get(identity, connection_id, project_id, False)
        job = self.jobs.get(identity)
        session, scope = self.binding(connection_id, project_id)
        if job["scope"] != scope.model_dump() or job["state"] != "completed":
            raise BridgeError("completed_job_exact_scope_required")
        encoded = job["result"].get("encoded_image")
        if encoded:
            if index != 0:
                raise ValueError("asset_index_invalid")
            content = base64.b64decode(encoded, validate=True)
            metadata = inspect_media(content, "image")
            target = local_path(destination, exists=False)
            if not target.parent.is_dir() or target.exists():
                raise ValueError("destination_parent_required_and_file_must_not_exist")
            with target.open("xb") as stream:
                stream.write(content)
            metadata = await file_metadata(target, "image")
            return {"path": str(target), "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "requested_resolution": job["input"].get("resolution"), **metadata}
        assets = job["result"].get("assets", [])
        if index < 0 or index >= len(assets):
            raise ValueError("asset_index_invalid")
        asset = assets[index]
        url = media_url(asset.get("url") or "", asset["media_id"], asset["kind"])
        target = local_path(destination, exists=False)
        if not target.parent.is_dir() or target.exists():
            raise ValueError("destination_parent_required_and_file_must_not_exist")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            "flow-content.google",
            443,
            type=socket.SOCK_STREAM,
        )
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError("generated_host_not_public")
        total = 0
        digest = hashlib.sha256()
        async with session.lock:
            if session.selected != scope:
                raise BridgeError("scope_changed")
            epoch = session.generation
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=60
            ) as client:
                address = str(addresses[0][4][0])
                pinned_url = httpx.URL(url).copy_with(host=address)
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": "flow-content.google"},
                    extensions={"sni_hostname": "flow-content.google"},
                ) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";")[0]
                    if mime not in {"image/png", "image/jpeg", "image/webp", "video/mp4"}:
                        raise ValueError("unexpected_media_content_type")
                    created = False
                    try:
                        with target.open("xb") as stream:
                            created = True
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > 256 * 1024 * 1024:
                                    raise ValueError("download_exceeds_256MiB")
                                if session.selected != scope or session.generation != epoch:
                                    raise BridgeError("scope_changed")
                                stream.write(chunk)
                                digest.update(chunk)
                        metadata = await file_metadata(target, asset["kind"])
                        if metadata["mime"] != mime:
                            raise ValueError("media_content_type_mismatch")
                    except BaseException:
                        if created:
                            target.unlink(missing_ok=True)
                        raise
        return {"path": str(target), "bytes": total, "sha256": digest.hexdigest(), **metadata}
