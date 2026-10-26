import asyncio
from uuid import uuid4

import pytest

from flow_mcp.jobs import Jobs
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import Scope
from flow_mcp.service import Operations


class BrowserSocket:
    supports_project_deletion = True

    def __init__(self, email):
        self.email = email
        self.sent = asyncio.Queue()
        self.manager = None
        self.session = None
        self.scope = None

    async def send(self, message):
        import json
        await self.sent.put(json.loads(message))

    async def close(self, code=1000, reason=""):
        pass

    async def prepare_project(self, project_id):
        self.scope = self.scope.model_copy(update={"project_id": project_id})
        self.session.selected = self.scope

    async def verify_scope(self, scope):
        if self.session.selected != scope:
            raise BridgeError("scope_changed")

    async def observe(self, scope):
        return {
            "scope": scope.model_dump(), "email": self.email, "email_confirmed": True,
            "source": "google_account_aria", "observed_at": "2026-10-26T00:00:00.000Z",
        }


def session_for(manager, project, email, document):
    socket = BrowserSocket(email)
    scope = Scope(tab_id=1, origin="https://flow.google.com",
                  project_id=project, document_id=document)
    socket.scope = scope
    session = manager.register_local(socket, str(uuid4()))
    socket.session = session
    session.selected = scope
    return session, socket, scope


async def test_recovery_after_restart_only_polls_persisted_handle(tmp_path):
    project = str(uuid4())
    jobs = Jobs(tmp_path / "jobs.sqlite3")
    old_manager = Manager()
    old, _, old_scope = session_for(old_manager, project, "owner@example.test", "old-doc")
    identity = jobs.create(old.id, old_scope, "generate_video", {},
                           account_email="owner@example.test")
    handle = str(uuid4())
    jobs.update(identity, "pending", {"operation_id": handle})
    old_manager.disconnected(old)

    restarted = Jobs(tmp_path / "jobs.sqlite3")
    manager = Manager()
    current, socket, current_scope = session_for(
        manager, project, "owner@example.test", "new-doc"
    )
    task = asyncio.create_task(Operations(manager, restarted).recover_job(
        identity, current.id, project
    ))
    request = await socket.sent.get()
    assert request["operation"] == "poll"
    assert request["params"]["input"]["operation_id"] == handle
    from flow_mcp.models import Reply
    manager.receive(current, Reply(type="reply", id=request["id"], result={
        "scope": current_scope.model_dump(), "state": "pending",
        "operation_id": handle, "diagnostics": {"rpc": "jwpduf"},
    }))
    result = await task
    assert result["state"] == "pending" and result["recovery_state"] == "remote_checked"
    assert restarted.get(identity)["connection_id"] == old.id
    assert restarted.get(identity)["scope"] == old_scope.model_dump()
    assert socket.sent.empty()
    assert "owner@example.test" not in str(restarted.get(identity))
    assert "owner@example.test" not in (tmp_path / "jobs.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )


@pytest.mark.parametrize("different", ["account", "project", "legacy", "no_handle"])
async def test_recovery_fails_closed_without_remote_rpc(tmp_path, different):
    project = str(uuid4())
    jobs = Jobs(tmp_path / "jobs.sqlite3")
    manager = Manager()
    old, _, scope = session_for(manager, project, "owner@example.test", "old-doc")
    identity = jobs.create(old.id, scope, "generate_video", {},
                           account_email=None if different == "legacy" else "owner@example.test")
    jobs.update(identity, "pending", {} if different == "no_handle" else {
        "operation_id": str(uuid4())
    })
    current, socket, _ = session_for(
        manager,
        str(uuid4()) if different == "project" else project,
        "other@example.test" if different == "account" else "owner@example.test",
        "new-doc",
    )
    with pytest.raises(BridgeError, match="recovery_|project_scope"):
        await Operations(manager, jobs).recover_job(
            identity, current.id, current.selected.project_id
        )
    assert socket.sent.empty()
    assert jobs.get(identity)["state"] == "pending"


async def test_completed_legacy_job_is_not_exposed_after_reconnect(tmp_path):
    project = str(uuid4())
    jobs = Jobs(tmp_path / "jobs.sqlite3")
    manager = Manager()
    old, _, scope = session_for(manager, project, "owner@example.test", "old-doc")
    identity = jobs.create(old.id, scope, "generate_image", {})
    jobs.update(identity, "completed", {"assets": [{"media_id": str(uuid4()), "kind": "image"}]})
    current, _, _ = session_for(manager, project, "owner@example.test", "new-doc")
    with pytest.raises(BridgeError, match="legacy_account_unbound"):
        await Operations(manager, jobs).recover_job(identity, current.id, project)


async def test_unknown_remote_outcome_does_not_replace_pending_job(tmp_path):
    from flow_mcp.models import Reply

    project = str(uuid4())
    jobs = Jobs(tmp_path / "jobs.sqlite3")
    manager = Manager()
    old, _, scope = session_for(manager, project, "owner@example.test", "old-doc")
    identity = jobs.create(old.id, scope, "generate_video", {},
                           account_email="owner@example.test")
    handle = str(uuid4())
    jobs.update(identity, "pending", {"operation_id": handle})
    current, socket, current_scope = session_for(
        manager, project, "owner@example.test", "new-doc"
    )
    task = asyncio.create_task(Operations(manager, jobs).recover_job(
        identity, current.id, project
    ))
    request = await socket.sent.get()
    manager.receive(current, Reply(type="reply", id=request["id"], result={
        "scope": current_scope.model_dump(), "state": "submitted_unverified",
        "error": "rpc_rejected", "diagnostics": {"rpc": "jwpduf", "phase": "parse"},
        "secret": "PRIVATE-TOKEN",
    }))
    result = await task
    assert result["recovery_state"] == "remote_query_failed"
    assert result["poll_error"] == "rpc_rejected"
    assert jobs.get(identity)["state"] == "pending"
    assert jobs.get(identity)["result"]["operation_id"] == handle
    assert "PRIVATE-TOKEN" not in str(result)
    assert socket.sent.empty()


def test_legacy_schema_migrates_without_inventing_account_identity(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, connection TEXT NOT NULL, "
        "scope TEXT NOT NULL, operation TEXT NOT NULL, input TEXT NOT NULL, "
        "state TEXT NOT NULL, result TEXT NOT NULL, updated TEXT NOT NULL)"
    )
    db.commit()
    db.close()
    jobs = Jobs(path)
    scope = Scope(tab_id=1, origin="https://flow.google.com",
                  project_id=str(uuid4()), document_id="legacy-doc")
    identity = jobs.create(str(uuid4()), scope, "generate_image", {})
    assert not jobs.has_account_binding(identity)
    assert jobs.get(identity)["state"] == "preview"
    assert len((tmp_path / "legacy.sqlite3.identity-key").read_bytes()) == 32
