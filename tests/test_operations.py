import asyncio
import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_manager import SECRET, pair, reply

from flow_mcp.jobs import Jobs
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import Observation, Reply, Scope
from flow_mcp.operations import ImageInput, VideoInput
from flow_mcp.service import Operations, local_path, media_url, upload_input

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path):
    manager = Manager(SECRET)
    session, socket = pair(manager)
    scope = Scope(
        tab_id=7,
        origin="https://flow.google.com",
        project_id=str(uuid4()),
        document_id="document-1",
    )
    session.selected = scope
    jobs = Jobs(tmp_path / "jobs.db")
    return manager, session, socket, scope, jobs, Operations(manager, jobs)


async def test_create_project_preview_transition_and_replay(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    scope = scope.model_copy(update={"project_id": None})
    session.selected = scope
    preview = await ops.create_project(session.id, False, None)
    assert preview["may_consume_credits"] is False
    assert socket.sent.empty()
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"]))
    request = await socket.sent.get()
    assert request["operation"] == "create_project"
    assert session.selected == scope
    selected = scope.model_copy(update={"project_id": str(uuid4())})
    reply(manager, session, request, {
        "state": "completed", "scope": scope.model_dump(), "selected": selected.model_dump()
    })
    assert (await task)["state"] == "completed"
    assert session.selected == selected
    assert (await ops.create_project(session.id, True, preview["job_id"]))["not_resubmitted"]
    assert socket.sent.empty()


@pytest.mark.parametrize("change", ["document_id", "tab_id", "origin"])
async def test_create_project_rejects_response_rebinding(tmp_path, change):
    manager, session, socket, scope, _, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"]))
    request = await socket.sent.get()
    selected = scope.model_dump()
    selected[change] = {
        "document_id": "other-doc", "tab_id": 99, "origin": "https://labs.google",
    }[change]
    reply(manager, session, request, {
        "scope": home.model_dump(), "state": "completed", "selected": selected,
    })
    assert (await task)["state"] == "submitted_unverified"
    assert session.selected is None


@pytest.mark.parametrize("state", ["completed", "not_submitted", "submitted_unverified"])
@pytest.mark.parametrize("omitted", [False, True])
async def test_create_actual_serialized_manager_payload(tmp_path, state, omitted):
    manager, session, socket, scope, _, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"]))
    request = await socket.sent.get()
    assert request["params"]["scope"] == home.model_dump()
    script = """
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {flowScope, sameScope, validRequest} from './extension/protocol.mjs';
const request = JSON.parse(process.argv[1]);
const source = readFileSync('./extension/background.js', 'utf8');
const helper = source.slice(source.indexOf('async function inspect(tabId'),
  source.indexOf('async function connect()'));
let main = 0;
const scope = request.params.scope;
const context = vm.createContext({request, scope, flowScope, sameScope,
  inspectPage() {}, flowOperation() {}, chrome: {
    tabs: {get: async () => ({url: 'https://flow.google.com/', status: 'complete'})},
    scripting: {executeScript: async spec => {
      if (spec.world === 'MAIN') {
        main++;
        return [{frameId: 0, documentId: scope.document_id,
          result: {scope, state: 'not_submitted', error: 'page_auth_unavailable'}}];
      }
      return [{frameId: 0, documentId: scope.document_id,
        result: {origin: scope.origin, pathname: '/', email: 'test@example.test'}}];
    }}
  }});
vm.runInContext(`let selected = scope; let derived = null; let epoch = 0; ${helper}`, context);
const result = await vm.runInContext('dispatch(request)', context);
const missing = {...scope}; delete missing.project_id;
context.missingRequest = {...request, params: {...request.params, scope: missing}};
let missingError;
try { await vm.runInContext('dispatch(missingRequest)', context); }
catch (error) { missingError = error.message; }
console.log(JSON.stringify({valid: validRequest(request), main, result, missingError,
  equal: sameScope(scope, missing), missingValid: validRequest(context.missingRequest)}));
"""
    process = await asyncio.create_subprocess_exec(
        "node", "--input-type=module", "-e", script, json.dumps(request),
        cwd=ROOT, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    evidence = json.loads(stdout)
    assert evidence["valid"] and evidence["main"] == 1
    assert not evidence["equal"] and not evidence["missingValid"]
    assert evidence["missingError"] == "scope_changed"
    result = evidence["result"]
    result["state"] = state
    if state == "completed":
        result.pop("error")
        result["selected"] = scope.model_dump()
    result["scope"] = home.model_dump(exclude_none=omitted)
    reply(manager, session, request, result)
    assert (await task)["state"] == state


@pytest.mark.parametrize("change", [
    {"project_id": ""}, {"project_id": "-" * 36}, {"project_id": 0},
    {"project_id": "12345678-1234-1234-1234-123456789abc"},
    {"document_id": "other"}, {"tab_id": 99}, {"origin": "https://labs.google"},
    {"extra": None},
])
async def test_create_home_response_normalization_rejects_invalid_scope(tmp_path, change):
    manager, session, socket, scope, _, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"]))
    request = await socket.sent.get()
    reply(manager, session, request, {
        "scope": {**home.model_dump(exclude_none=True), **change},
        "state": "not_submitted", "error": "page_auth_unavailable",
    })
    assert (await task)["state"] == "submitted_unverified"
    assert session.selected is None


async def test_old_click_preview_cannot_authorize_rpc(tmp_path):
    _, session, socket, scope, jobs, ops = setup(tmp_path)
    session.selected = scope.model_copy(update={"project_id": None})
    old = jobs.create(session.id, session.selected, "create_project", {})
    with pytest.raises(BridgeError, match="preview_scope_or_input_mismatch"):
        await ops.create_project(session.id, True, old)
    assert socket.sent.empty()


async def test_create_project_uncertainty_persists_without_retry(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    session.selected = scope.model_copy(update={"project_id": None})
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"]))
    await socket.sent.get()
    manager.disconnected(session)
    assert (await task)["state"] == "submitted_unverified"
    assert (await ops.create_project(session.id, True, preview["job_id"]))["not_resubmitted"]
    assert Jobs(tmp_path / "jobs.db").get(preview["job_id"])["state"] == "submitted_unverified"
    assert socket.sent.empty()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("state", ["not_submitted", "submitted_unverified"])
async def test_project_diagnostics_are_opt_in_sanitized_and_preserved(tmp_path, enabled, state):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"], enabled))
    request = await socket.sent.get()
    assert request["params"].get("diagnostics", False) is enabled
    reply(manager, session, request, {
        "scope": home.model_dump(), "state": state, "error": "rpc_http_error",
        "diagnostics": {"rpc": "jHPbke", "http_status": 403, "phase": "transport"},
        "raw": "private token must not persist",
    })
    assert (await task)["state"] == state
    stored = jobs.get(preview["job_id"])["result"]
    assert stored["error"] == "rpc_http_error"
    assert ("diagnostics" in stored) is enabled
    assert "private" not in str(stored)
    assert (await ops.create_project(session.id, True, preview["job_id"], enabled))[
        "not_resubmitted"
    ]
    assert socket.sent.empty()


async def test_project_diagnostics_reject_payload_secrets(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None)
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"], True))
    request = await socket.sent.get()
    reply(manager, session, request, {
        "scope": home.model_dump(), "state": "submitted_unverified",
        "diagnostics": {"rpc": "jHPbke", "raw": "private token"},
    })
    assert (await task)["state"] == "submitted_unverified"
    assert "private" not in str(jobs.get(preview["job_id"])["result"])
    assert socket.sent.empty()


def clean_shape():
    return {
        "type": "array",
        "length": 1,
        "items": [{"type": "array", "length": 1, "items": ["number"]}],
    }


def rejected_diagnostics(rpc):
    return {
        "rpc": rpc,
        "http_status": 200,
        "chunks": 1,
        "envelopes": 1,
        "rpc_code": 8,
        "phase": "parse",
        "rpc_error_shape": clean_shape(),
        "raw": "private token must never persist",
        "url": "https://flow.google.com/private?Signature=leak",
        "prompt": "private prompt",
        "title": "private title",
    }


@pytest.mark.parametrize(
    "operation,rpc,value",
    [
        ("generate_image", "ogiZ0b", ImageInput(prompt="test")),
        ("generate_video", "YhhmEf", VideoInput(prompt="test")),
        ("upscale_image", "SPrCad", None),
        ("upload", "maseQ", None),
    ],
)
async def test_any_submission_persists_sanitized_rpc_rejected_for_image_and_video(
    tmp_path, operation, rpc, value
):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    media = str(uuid4())
    if operation == "upscale_image":
        from flow_mcp.operations import ImageUpscaleInput

        source = jobs.create(session.id, scope, "upload", {})
        jobs.update(source, "completed", {"assets": [{"media_id": media, "kind": "image"}]})
        value = ImageUpscaleInput(media_id=media)
    if operation == "upload":
        from flow_mcp.operations import UploadInput

        value = UploadInput(data=base64.b64encode(b"png").decode(), mime="image/png", name="x.png")
    assert value is not None
    preview = await ops.submit(operation, session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit(operation, session.id, scope.project_id, value, True, preview["job_id"])
    )
    request = await socket.sent.get()
    reply(manager, session, request, {
        "id": request["id"], "scope": scope.model_dump(), "state": "submitted_unverified",
        "error": "rpc_rejected", "diagnostics": rejected_diagnostics(rpc),
    })
    result = await task
    assert result["state"] == "submitted_unverified"
    stored = jobs.get(preview["job_id"])["result"]
    diagnostics = stored["diagnostics"]
    assert diagnostics["rpc"] == rpc
    assert diagnostics["rpc_code"] == 8
    assert diagnostics["http_status"] == 200
    assert diagnostics["phase"] == "parse"
    assert diagnostics["rpc_error_shape"] == clean_shape()
    for key in ("raw", "url", "prompt", "title"):
        assert key not in diagnostics
    encoded = json.dumps(stored)
    assert "private" not in encoded and "leak" not in encoded
    assert (await ops.submit(operation, session.id, scope.project_id, value, True,
                             preview["job_id"]))["not_resubmitted"]
    assert socket.sent.empty()


async def test_absent_diagnostics_stay_absent_and_umbrella_failure(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, preview["job_id"])
    )
    request = await socket.sent.get()
    reply(manager, session, request, {
        "id": request["id"], "scope": scope.model_dump(), "state": "submitted_unverified",
        "error": "rpc_rejected",
    })
    assert (await task)["state"] == "submitted_unverified"
    stored = jobs.get(preview["job_id"])["result"]
    assert "diagnostics" not in stored
    stored.pop("scope", None)
    assert set(stored) == {
        "state", "assets", "encoded_image", "operation_id", "media_id", "error"
    }
    assert stored["error"] == "rpc_rejected"
    assert socket.sent.empty()


async def test_assumed_bridge_failure_persists_and_reports_code(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, preview["job_id"])
    )
    await socket.sent.get()
    manager.disconnected(session)
    result = await task
    assert result["state"] == "submitted_unverified"
    failure = result["result"]["failure"]
    assert failure["stage"] == "bridge_or_response_validation"
    assert failure["code"] == "connection_closed"
    persisted = Jobs(tmp_path / "jobs.db").get(preview["job_id"])["result"]["failure"]
    assert persisted["code"] == "connection_closed"
    assert socket.sent.empty()


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"rpc": "jHPbke", "rpc_code": 8.5},
        {"rpc": "https://evil.test/private", "http_status": 200},
        {"raw": "private token"},
        {"rpc_code": 999},
        {"phase": "arbitrary"},
        {"envelope_shape": {"type": "array", "length": 99999999, "items": []}},
        {"payload_shape": {"type": "object"}},
    ],
)
async def test_oversize_or_unknown_diagnostics_are_rejected_safely(tmp_path, diagnostics):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, preview["job_id"])
    )
    request = await socket.sent.get()
    reply(manager, session, request, {
        "id": request["id"], "scope": scope.model_dump(), "state": "submitted_unverified",
        "error": "rpc_rejected", "diagnostics": diagnostics,
    })
    result = await task
    assert result["state"] == "submitted_unverified"
    stored = jobs.get(preview["job_id"])["result"]
    assert "diagnostics" not in stored
    assert "private" not in json.dumps(stored)
    assert socket.sent.empty()


async def test_create_project_rejects_changed_scope(tmp_path):
    _, session, socket, scope, _, ops = setup(tmp_path)
    with pytest.raises(BridgeError, match="home_tab_required"):
        await ops.create_project(session.id, False, None)
    session.selected = scope.model_copy(update={"project_id": None})
    preview = await ops.create_project(session.id, False, None)
    session.selected = session.selected.model_copy(update={"document_id": "changed"})
    with pytest.raises(BridgeError, match="preview_scope"):
        await ops.create_project(session.id, True, preview["job_id"])
    assert socket.sent.empty()


@pytest.mark.parametrize("model", ["nano_banana_pro", "nano_banana_2", "nano_banana_lite"])
async def test_image_preview_is_free_but_still_requires_confirmation(tmp_path, model):
    _, session, socket, scope, _, ops = setup(tmp_path)
    preview = await ops.submit(
        "generate_image", session.id, scope.project_id,
        ImageInput(prompt="test", model=model), False, None,
    )
    assert preview["may_consume_credits"] is False
    assert preview["confirmation_required"] is True
    assert preview["state"] == "preview"
    assert socket.sent.empty()


async def test_video_preview_still_warns_about_credits(tmp_path):
    _, session, socket, scope, _, ops = setup(tmp_path)
    preview = await ops.submit(
        "generate_video", session.id, scope.project_id,
        VideoInput(prompt="test"), False, None,
    )
    assert preview["may_consume_credits"] is True
    assert preview["confirmation_required"] is True
    assert socket.sent.empty()


async def test_confirmation_is_bound_and_single_use(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    assert socket.sent.empty()
    identity = preview["job_id"]
    with pytest.raises(BridgeError, match="input_mismatch"):
        await ops.submit(
            "generate_image",
            session.id,
            scope.project_id,
            ImageInput(prompt="changed"),
            True,
            identity,
        )
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, identity)
    )
    request = await socket.sent.get()
    reply(
        manager,
        session,
        request,
        {
            "scope": scope.model_dump(),
            "state": "completed",
            "assets": [{"media_id": str(uuid4()), "kind": "image"}],
        },
    )
    assert (await task)["state"] == "completed"
    assert (
        await ops.submit("generate_image", session.id, scope.project_id, value, True, identity)
    )["not_resubmitted"]
    assert socket.sent.empty()
    assert Jobs(tmp_path / "jobs.db").get(identity)["state"] == "completed"


async def test_disconnect_uncertain_never_retries(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, preview["job_id"])
    )
    await socket.sent.get()
    manager.disconnected(session)
    assert (await task)["state"] == "submitted_unverified"
    assert socket.sent.empty()
    other, other_socket = pair(manager)
    other.selected = scope
    with pytest.raises(BridgeError, match="job_scope_mismatch"):
        await ops.get(preview["job_id"], other.id, scope.project_id, True)
    assert other_socket.sent.empty()


async def test_poll_recovery_exact_document_and_project(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    identity = jobs.create(session.id, scope, "generate_video", {})
    remote = str(uuid4())
    jobs.update(identity, "pending", {"operation_id": remote})
    task = asyncio.create_task(ops.get(identity, session.id, scope.project_id, True))
    request = await socket.sent.get()
    assert request["operation"] == "poll"
    assert request["params"]["input"]["operation_id"] == remote
    reply(manager, session, request, {"scope": scope.model_dump(), "state": "pending"})
    result = await task
    assert result["result"]["operation_id"] == remote
    assert result["source"] == "remote" and result["remote_checked"] is True
    assert result["last_remote_check_at"] == jobs.get(identity)["result"]["last_remote_check_at"]
    cached = await ops.get(identity, session.id, scope.project_id, False)
    assert cached["source"] == "cache" and cached["remote_checked"] is False
    assert cached["result"]["last_remote_check_at"] == result["last_remote_check_at"]
    assert socket.sent.empty()
    session.selected = scope.model_copy(update={"document_id": "new-document"})

    with pytest.raises(BridgeError, match="document_mismatch"):
        await ops.get(identity, session.id, scope.project_id, True)


@pytest.mark.parametrize("remote_state,error", [
    ("pending", None),
    ("completed", None),
    ("submitted_unverified", "rpc_rejected"),
])
async def test_poll_with_diagnostics_preserves_state_and_error(tmp_path, remote_state, error):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    identity = jobs.create(session.id, scope, "generate_image", {})
    remote = str(uuid4())
    jobs.update(identity, "pending", {"operation_id": remote})
    task = asyncio.create_task(ops.get(identity, session.id, scope.project_id, True))
    request = await socket.sent.get()
    payload = {
        "scope": scope.model_dump(), "state": remote_state,
        "diagnostics": {"phase": "parse", "rpc": "ogiZ0b"},
        "secret_context": "PRIVATE-TOKEN-MUST-NOT-PERSIST",
    }
    if remote_state == "completed":
        payload["assets"] = [{"media_id": str(uuid4()), "kind": "image"}]
    if error:
        payload["error"] = error
    reply(manager, session, request, payload)
    result = await task
    assert result.get("poll_error") == error
    assert result["source"] == "remote" and result["remote_checked"] is True
    assert result["last_remote_check_at"]
    if error:
        assert "last_remote_check_at" not in jobs.get(identity)["result"]
    else:
        assert jobs.get(identity)["result"]["last_remote_check_at"] == result[
            "last_remote_check_at"
        ]
    assert "PRIVATE-TOKEN-MUST-NOT-PERSIST" not in json.dumps(result)
    assert "PRIVATE-TOKEN-MUST-NOT-PERSIST" not in json.dumps(jobs.get(identity))
    if error:
        assert result["poll_diagnostics"] == {"rpc": "ogiZ0b", "phase": "parse"}
        assert jobs.get(identity)["state"] == "pending"
        assert jobs.get(identity)["result"]["operation_id"] == remote
    else:
        assert result["state"] == remote_state
        assert jobs.get(identity)["result"]["diagnostics"] == {
            "rpc": "ogiZ0b", "phase": "parse"
        }
    assert socket.sent.empty()


async def test_concurrent_poll_cannot_regress_completed_job(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    identity = jobs.create(session.id, scope, "generate_image", {})
    handle = str(uuid4())
    jobs.update(identity, "pending", {"operation_id": handle})
    first = asyncio.create_task(ops.get(identity, session.id, scope.project_id, True))
    request = await socket.sent.get()
    second = asyncio.create_task(ops.get(identity, session.id, scope.project_id, True))
    await asyncio.sleep(0)
    assert socket.sent.empty()
    media = str(uuid4())
    reply(manager, session, request, {
        "scope": scope.model_dump(), "state": "completed",
        "assets": [{"media_id": media, "kind": "image"}],
    })
    assert (await first)["state"] == "completed"
    cached = await second
    assert cached["state"] == "completed" and cached["source"] == "cache"
    assert jobs.get(identity)["result"]["assets"][0]["media_id"] == media
    assert socket.sent.empty()


async def test_references_require_owned_images(tmp_path):
    _, session, _, scope, jobs, ops = setup(tmp_path)
    media = str(uuid4())
    value = ImageInput(prompt="test", references=[media])
    with pytest.raises(BridgeError, match="reference_not_owned"):
        await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    identity = jobs.create(session.id, scope, "upload", {})
    jobs.update(identity, "completed", {"assets": [{"media_id": media, "kind": "image"}]})
    assert (await ops.submit("generate_image", session.id, scope.project_id, value, False, None))[
        "state"
    ] == "preview"


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "unknown"},
        {"end": str(uuid4())},
        {"model": "veo_lite"},
        {"start": str(uuid4()), "references": [str(uuid4())]},
        {"model": "veo_lite", "start": str(uuid4()), "duration": 4},
        {"prompt": ""},
        {"url": "https://evil.example"},
        {"duration": True},
    ],
)
def test_video_rejects_unsupported_modes(changes):
    with pytest.raises(ValidationError):
        VideoInput.model_validate({"prompt": "test", **changes})


def test_upload_validation_and_canonical_path(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    file = tmp_path / "image.png"
    file.write_bytes(raw)
    assert upload_input(str(file), None, "image.png").mime == "image/png"
    assert upload_input(None, base64.b64encode(raw).decode(), "image.png").mime == "image/png"
    for value in [
        "https://example.com/file",
        "relative.png",
        "\\\\host\\share\\x",
        "//host/share/x",
    ]:
        with pytest.raises(ValueError):
            local_path(value, False)
    with pytest.raises(ValueError):
        upload_input(None, "!!", "x")
    with pytest.raises(ValueError):
        upload_input(None, base64.b64encode(b"<script>").decode(), "x")
    with pytest.raises(ValueError):
        upload_input(str(file), "abc", "x")


@pytest.mark.parametrize(
    "url",
    [
        "http://flow-content.google/image/{id}?Signature=x",
        "https://flow-content.google.evil/image/{id}?Signature=x",
        "https://user@flow-content.google/image/{id}?Signature=x",
        "https://flow-content.google:443/image/{id}?Signature=x",
        "https://flow-content.google/image/{id}",
        "https://flow-content.google/image/{id}?arbitrary=x",
        "https://127.0.0.1/image/{id}?Signature=x",
        "https://flow-content.google/video/{id}?Signature=x",
    ],
)
def test_download_rejects_unsigned_and_external(url):
    media = str(uuid4())
    with pytest.raises(ValueError):
        media_url(url.format(id=media), media, "image")


def test_crash_recovery_never_resubmits(tmp_path):
    _, session, _, scope, jobs, _ = setup(tmp_path)
    identity = jobs.create(session.id, scope, "generate_video", {})
    jobs.update(identity, "submitting", {})
    recovered = Jobs(tmp_path / "jobs.db")
    assert recovered.get(identity)["state"] == "submitted_unverified"


def test_account_requires_explicit_source():
    scope = {"tab_id": 1, "origin": "https://flow.google.com", "document_id": "doc"}
    value = Observation.model_validate(
        {
            "scope": scope,
            "email": "test@example.com",
            "email_confirmed": True,
            "source": "google_account_aria",
        }
    )
    assert value.credits is None
    with pytest.raises(ValidationError):
        Observation.model_validate({"scope": scope, "email": "test@example.com"})


async def test_submission_timeout_is_persistent_uncertainty(tmp_path, monkeypatch):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    original = manager.request

    async def short_request(session, operation, params, deadline_seconds=None):
        return await original(session, operation, params, deadline_seconds=0.01)

    monkeypatch.setattr(manager, "request", short_request)
    value = ImageInput(prompt="timeout")
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    result = await ops.submit(
        "generate_image", session.id, scope.project_id, value, True, preview["job_id"]
    )
    assert result["state"] == "submitted_unverified"
    assert socket.sent.qsize() == 1 and socket.closed
    assert jobs.get(preview["job_id"])["state"] == "submitted_unverified"


async def test_download_pins_public_ip_and_tls_host(tmp_path, monkeypatch):
    import httpx

    import flow_mcp.service as service

    _, session, _, scope, jobs, ops = setup(tmp_path)
    media = str(uuid4())
    identity = jobs.create(session.id, scope, "generate_image", {})
    jobs.update(
        identity,
        "completed",
        {
            "assets": [
                {
                    "media_id": media,
                    "kind": "image",
                    "url": f"https://flow-content.google/image/{media}?Signature=synthetic",
                }
            ]
        },
    )

    async def resolve(*args, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    requests = []

    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jN1s"
        "AAAAASUVORK5CYII="
    )

    def handler(request):
        requests.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "flow-content.google"
        assert request.extensions["sni_hostname"] == "flow-content.google"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=content)

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(service.httpx, "AsyncClient", client)
    target = tmp_path / "output.png"
    result = await ops.download(identity, session.id, scope.project_id, str(target), 0)
    assert result["bytes"] == len(content) and target.read_bytes() == content
    assert result["width"] == result["height"] == 1
    assert result["signature_verified"] and not result["quality_verified"]
    with pytest.raises(ValueError, match="must_not_exist"):
        await ops.download(identity, session.id, scope.project_id, str(target), 0)
    assert len(requests) == 1


async def test_upscale_owned_kind_inline_download_and_replay(tmp_path):
    from flow_mcp.operations import ImageUpscaleInput, VideoUpscaleInput

    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    media = str(uuid4())
    value = ImageUpscaleInput(media_id=media)
    with pytest.raises(BridgeError, match="reference_not_owned"):
        await ops.submit("upscale_image", session.id, scope.project_id, value, False, None)
    source = jobs.create(session.id, scope, "upload", {})
    jobs.update(source, "completed", {"assets": [{"media_id": media, "kind": "image"}]})
    with pytest.raises(BridgeError, match="reference_not_owned"):
        await ops.submit("upscale_video", session.id, scope.project_id,
                         VideoUpscaleInput(media_id=media, scene_id=str(uuid4())), False, None)
    with pytest.raises(ValidationError):
        VideoUpscaleInput(media_id=media, scene_id=str(uuid4()), resolution="4K")
    preview = await ops.submit("upscale_image", session.id, scope.project_id, value, False, None)
    assert socket.sent.empty()
    task = asyncio.create_task(ops.submit("upscale_image", session.id, scope.project_id,
                                          value, True, preview["job_id"]))
    request = await socket.sent.get()
    content = b"\xff\xd8\xff" + b"synthetic" + b"\xff\xd9"
    reply(manager, session, request, {"scope": scope.model_dump(), "state": "completed",
                                    "encoded_image": base64.b64encode(content).decode()})
    result = await task
    assert result["result"]["inline_image_available"]
    assert "encoded_image" not in result["result"]
    downloaded = await ops.download(preview["job_id"], session.id, scope.project_id,
                                    str(tmp_path / "upscale.jpg"), 0)
    assert downloaded["signature_verified"] and not downloaded["decode_verified"]
    assert (await ops.submit("upscale_image", session.id, scope.project_id, value,
                             True, preview["job_id"]))["not_resubmitted"]
    assert socket.sent.empty()


async def test_private_dns_download_blocked(tmp_path, monkeypatch):
    _, session, _, scope, jobs, ops = setup(tmp_path)
    media = str(uuid4())
    identity = jobs.create(session.id, scope, "generate_image", {})
    jobs.update(
        identity,
        "completed",
        {
            "assets": [
                {
                    "media_id": media,
                    "kind": "image",
                    "url": f"https://flow-content.google/image/{media}?Signature=synthetic",
                }
            ]
        },
    )

    async def resolve(*args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="not_public"):
        await ops.download(identity, session.id, scope.project_id, str(tmp_path / "out.png"), 0)


REPLIES = json.loads((ROOT / "tests" / "fixtures" / "extension_replies.json").read_text())


async def confirmed(ops, session, scope, value):
    """Start a confirmed generate_image submission; caller feeds the reply.

    Returns the preview job id and the task awaiting the reply.
    """
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    task = asyncio.create_task(
        ops.submit("generate_image", session.id, scope.project_id, value, True, preview["job_id"])
    )
    return preview["job_id"], task


async def test_real_extension_success_reply_is_accepted_with_signature(tmp_path):
    """The exact reply background.js sends for a successful image must validate.

    Regression: a reply carrying unrelated envelope metadata (``rpc_verified``,
    echoed ``id``) used to collapse into ``invalid_response`` because Outcome is
    extra=forbid. The real reply shape comes from the captured fixture.
    """
    manager, session, socket, _, jobs, ops = setup(tmp_path)
    scope = Scope.model_validate(REPLIES["success"]["result"]["scope"])
    session.selected = scope
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    reply(manager, session, request, REPLIES["success"]["result"])
    result = await task
    assert result["state"] == "completed"
    stored = jobs.get(identity)["result"]
    assert stored["assets"][0]["media_id"] == REPLIES["success"]["result"]["assets"][0]["media_id"]
    assert "Signature=" not in json.dumps(stored) or stored["assets"][0]["url"].startswith(
        "https://flow-content.google/image/"
    )
    assert socket.sent.empty()


async def test_real_extension_rpc_rejected_reply_persists_diagnostics(tmp_path):
    """The exact rejected reply keeps state, error and sanitized diagnostics."""
    manager, session, socket, _, jobs, ops = setup(tmp_path)
    scope = Scope.model_validate(REPLIES["rejected"]["result"]["scope"])
    session.selected = scope
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    reply(manager, session, request, REPLIES["rejected"]["result"])
    result = await task
    assert result["state"] == "submitted_unverified"
    stored = jobs.get(identity)["result"]
    assert stored["error"] == "rpc_rejected"
    assert stored["diagnostics"]["rpc"] == "ogiZ0b"
    assert stored["diagnostics"]["rpc_code"] == 8
    assert stored["diagnostics"]["phase"] == "parse"
    assert socket.sent.empty()


@pytest.mark.parametrize(
    "extra",
    [
        {"rpc_verified": True},
        {"failure": {"stage": "x", "code": "y"}},
        {"completed_at": "2026-10-04T00:00:00.000Z"},
    ],
)
async def test_unknown_envelope_keys_are_tolerated_and_named_not_valued(tmp_path, extra):
    """Unknown reply metadata must not nuke an otherwise valid result.

    Tolerant-but-strict: known fields are validated strictly, unknown key *names*
    are surfaced in sanitized diagnostics, never their values.
    """
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    payload = {
        "scope": scope.model_dump(),
        "state": "completed",
        "assets": [{"media_id": str(uuid4()), "kind": "image"}],
        **extra,
    }
    reply(manager, session, request, payload)
    result = await task
    assert result["state"] == "completed"
    stored = jobs.get(identity)["result"]
    assert stored["diagnostics"]["reply_extra"] == sorted(extra)
    for value in extra.values():
        assert json.dumps(value) not in json.dumps(stored["diagnostics"])
    assert socket.sent.empty()


async def test_echoed_envelope_id_is_ignored_without_diagnostics(tmp_path):
    """An echoed ``id`` is a tolerated transport key, not reported as unexpected."""
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    reply(manager, session, request, {
        "scope": scope.model_dump(), "state": "completed", "id": request["id"],
        "assets": [{"media_id": str(uuid4()), "kind": "image"}],
    })
    result = await task
    assert result["state"] == "completed"
    assert "diagnostics" not in jobs.get(identity)["result"]
    assert socket.sent.empty()


async def test_rejected_reply_records_extra_keys_without_values(tmp_path):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    reply(manager, session, request, {
        "scope": scope.model_dump(), "state": "submitted_unverified", "error": "rpc_rejected",
        "secret_context": "PRIVATE-TOKEN-MUST-NOT-PERSIST",
    })
    result = await task
    assert result["state"] == "submitted_unverified"
    stored = jobs.get(identity)["result"]
    assert stored["error"] == "rpc_rejected"
    assert stored["diagnostics"]["reply_extra"] == ["secret_context"]
    assert "PRIVATE-TOKEN-MUST-NOT-PERSIST" not in json.dumps(stored)
    assert socket.sent.empty()


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"state": "weird"}, "invalid_response:shape:state"),
        ({"state": "completed", "assets": [{"media_id": "not-a-uuid", "kind": "image"}]},
         "invalid_response:shape:assets.0.media_id"),
        ({"state": "completed", "assets": []}, "invalid_response:completed_result_requires_assets"),
        ({"state": "pending"}, "invalid_response:pending_result_requires_remote_handle"),
    ],
)
async def test_malformed_reply_fails_strictly_with_diagnosable_code(tmp_path, payload, code):
    """A genuinely malformed reply keeps failing, but names the exact cause.

    Regression: every ValueError/ValidationError used to be flattened to a bare
    ``invalid_response``, so the persisted job could not be diagnosed.
    """
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    reply(manager, session, request, {"scope": scope.model_dump(), **payload})
    result = await task
    assert result["state"] == "submitted_unverified"
    assert jobs.get(identity)["result"]["failure"]["code"] == code
    assert socket.sent.empty()


async def test_reply_without_result_is_named_python_parse_not_generic(tmp_path):
    """The exact extension wire shape that produced an opaque live failure.

    ``background.js`` sends ``{type:"reply", id, result: undefined}`` when
    ``dispatch`` resolves to ``undefined``; JSON.stringify drops the ``result``
    key entirely. The bridge then sees a well-formed ``Reply`` with ``result=None``
    and used to raise a bare ``BridgeError("invalid_response")`` from
    ``manager.request`` -- before ``Operations.outcome`` was ever reached, so the
    tolerant-parsing fix there could never see it. It must now be named and
    carry the sanitized reply shape (key names + type tags only).
    """
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    # The literal bytes background.js emits when result is undefined.
    manager.receive(session, Reply(type="reply", id=request["id"]))
    result = await task
    assert result["state"] == "submitted_unverified"
    failure = jobs.get(identity)["result"]["failure"]
    assert failure["code"] == "invalid_response:python_parse"
    diagnostics = failure["diagnostics"]
    assert diagnostics["reply_stage"] == "python_parse"
    assert diagnostics["reply_types"]["result"] == "null"
    assert set(diagnostics["reply_keys"]) == {"type", "id", "result", "error"}
    # No values leak: only names and coarse types are persisted.
    assert "test" not in json.dumps(diagnostics)
    assert socket.sent.empty()


async def test_extension_error_reply_is_staged_as_extension_error(tmp_path):
    """An ``error`` reply from the extension is a distinct stage, not a python parse."""
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="test")
    identity, task = await confirmed(ops, session, scope, value)
    request = await socket.sent.get()
    manager.receive(session, Reply(type="reply", id=request["id"], error="inspection_failed"))
    result = await task
    assert result["state"] == "submitted_unverified"
    failure = jobs.get(identity)["result"]["failure"]
    assert failure["code"] == "inspection_failed"
    assert failure["diagnostics"]["reply_stage"] == "extension_error"
    assert socket.sent.empty()


def test_describe_reply_is_bounded_and_value_free():
    """The reply shape helper never returns values and bounds key names."""
    from flow_mcp.manager import describe_reply

    described = describe_reply(
        {
            "state": "submitted_unverified",
            "error": "rpc_rejected",
            "diagnostics": {"rpc": "ogiZ0b", "rpc_code": 8},
            "assets": [],
            "secret": "PRIVATE-TOKEN-MUST-NOT-PERSIST",
            "https://evil.test/private?Signature=leak": "x",
        }
    )
    assert described["reply_types"]["diagnostics"] == "object"
    assert described["reply_types"]["assets"] == "array"
    assert described["reply_types"]["secret"] == "str"
    # Non-identifier keys (URLs) are dropped entirely, values never persisted.
    assert "https://evil.test/private?Signature=leak" not in described["reply_keys"]
    assert "PRIVATE-TOKEN-MUST-NOT-PERSIST" not in json.dumps(described)
    assert "ogiZ0b" not in json.dumps(described)
    assert described["reply_types"]["secret"] == "str"


def test_describe_reply_bounds_many_keys():
    from flow_mcp.manager import describe_reply

    described = describe_reply({f"key_{index}": index for index in range(100)})
    assert len(described["reply_keys"]) == 32
    assert described["reply_keys_truncated"] == 100



async def test_inline_upscale_download_contains_real_png_dimensions(tmp_path):
    import struct
    import zlib

    _, session, _, scope, jobs, ops = setup(tmp_path)

    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data)))

    content = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x00\x80\x80' * 2) * 2))
               + chunk(b'IEND', b''))
    identity = jobs.create(session.id, scope, 'upscale_image', {'resolution': '2K'})
    jobs.update(identity, 'completed', {'encoded_image': base64.b64encode(content).decode()})
    public = ops.public(jobs.get(identity))
    assert public['result']['download_available']
    assert public['result']['download_asset_index'] == 0
    assert 'encoded_image' not in public['result']
    result = await ops.download(identity, session.id, scope.project_id,
                                str(tmp_path / 'inline.png'), 0)
    assert (result['width'], result['height']) == (2, 2)
    assert result['requested_resolution'] == '2K'
    # Requested resolution must not be confused with observed output dimensions.
    assert not result['quality_verified']


@pytest.mark.parametrize("seed", [None, 1, 42, 1_000_000_000])
def test_image_seed_accepts_local_range(seed):
    assert ImageInput(prompt="x", seed=seed).seed == seed
    assert ImageInput(prompt="x").seed is None

@pytest.mark.parametrize("seed", [True, False, "42", 1.0, 1.5, 0, -1, 1_000_000_001, [], {}])
def test_image_seed_rejects_coercion_and_out_of_range(seed):
    with pytest.raises(ValidationError):
        ImageInput(prompt="x", seed=seed)

@pytest.mark.parametrize("seed", [None, 1, 1_000_000_000])
async def test_seed_preview_confirmation_and_dispatch(tmp_path, seed):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    value = ImageInput(prompt="x", seed=seed)
    preview = await ops.submit("generate_image", session.id, scope.project_id, value, False, None)
    assert preview["preview"]["seed"] == seed
    changed = ImageInput(prompt="x", seed=2)
    with pytest.raises(BridgeError, match="preview_scope_or_input_mismatch"):
        await ops.submit(
            "generate_image", session.id, scope.project_id, changed, True, preview["job_id"]
        )
    assert socket.sent.empty()
    task = asyncio.create_task(ops.submit(
        "generate_image", session.id, scope.project_id, value, True, preview["job_id"]
    ))
    request = await socket.sent.get()
    assert request["params"]["input"]["seed"] == seed
    reply(manager, session, request, {"scope": scope.model_dump(), "state": "not_submitted"})
    assert (await task)["state"] == "not_submitted"

@pytest.mark.parametrize("title", ["", " \t\n", "x" * 201, None, True, 42, []])
async def test_project_title_validates_before_browser_or_job(tmp_path, title):
    _, session, socket, scope, _, ops = setup(tmp_path)
    session.selected = scope.model_copy(update={"project_id": None})
    with pytest.raises(ValidationError):
        await ops.create_project(session.id, False, None, title=title)
    assert socket.sent.empty()

@pytest.mark.parametrize("title", ["x", "x" * 200, '  Projeto "teste" <script>  '])
async def test_project_title_bound_to_preview_dispatch_and_replay(tmp_path, title):
    manager, session, socket, scope, jobs, ops = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    preview = await ops.create_project(session.id, False, None, title=title)
    assert preview["preview"]["title"] == title
    assert jobs.get(preview["job_id"])["input"]["title"] == title
    with pytest.raises(BridgeError, match="preview_scope_or_input_mismatch"):
        await ops.create_project(session.id, True, preview["job_id"], title="changed")
    assert socket.sent.empty()
    task = asyncio.create_task(ops.create_project(session.id, True, preview["job_id"], title=title))
    request = await socket.sent.get()
    assert request["params"]["title"] == title
    reply(manager, session, request, {
        "state": "completed", "scope": home.model_dump(), "selected": scope.model_dump(),
    })
    assert (await task)["state"] == "completed"
    replay = await ops.create_project(session.id, True, preview["job_id"], title=title)
    assert replay["not_resubmitted"]
    with pytest.raises(BridgeError, match="preview_scope_or_input_mismatch"):
        await ops.create_project(session.id, True, preview["job_id"], title="changed")
    assert socket.sent.empty()
