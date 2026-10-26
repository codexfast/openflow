import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import Hello, Observation, Reply, Scope

SECRET = "s" * 64
SCOPE = {
    "tab_id": 7,
    "origin": "https://flow.google.com",
    "project_id": None,
    "document_id": "document-1",
}


class Socket:
    def __init__(self):
        self.sent = asyncio.Queue()
        self.closed = False

    async def send(self, message):
        await self.sent.put(json.loads(message))

    async def close(self, code=1000, reason=""):
        self.closed = True


def pair(manager, identity=None, secret=SECRET):
    socket = Socket()
    hello = Hello(
        type="hello",
        version=1,
        secret=secret,
        installation_id=identity or str(uuid4()),
        nickname="test",
    )
    return manager.pair(hello, socket), socket


def reply(manager, session, request, result):
    manager.receive(session, Reply(type="reply", id=request["id"], result=result))


async def test_routing_isolation_and_unique_ids():
    manager = Manager(SECRET)
    a, sa = pair(manager)
    b, sb = pair(manager)
    tasks = [asyncio.create_task(manager.snapshot(s.id)) for s in [a, b]]
    ra, rb = await sa.sent.get(), await sb.sent.get()
    assert ra["id"] != rb["id"]
    reply(manager, b, ra, {"tabs": []})
    await asyncio.sleep(0)
    assert not tasks[0].done()
    reply(manager, a, ra, {"tabs": []})
    reply(manager, b, rb, {"tabs": []})
    results = await asyncio.gather(*tasks)
    assert [r["connection_id"] for r in results] == [a.id, b.id]
    with pytest.raises(BridgeError, match="connection_id_required"):
        manager.resolve(None)


def test_pairing_and_duplicates():
    manager = Manager(SECRET)
    with pytest.raises(BridgeError, match="pairing_rejected"):
        pair(manager, secret="x" * 64)
    a, _ = pair(manager)
    with pytest.raises(BridgeError, match="duplicate_connection"):
        pair(manager, identity=a.id)
    assert manager.resolve(None) is a
    manager.disconnected(a)
    b, _ = pair(manager, identity=a.id)
    manager.disconnected(a)
    assert manager.resolve(a.id) is b


async def test_timeout_disconnects_and_does_not_retry():
    manager = Manager(SECRET, timeout=0.01)
    session, socket = pair(manager)
    with pytest.raises(BridgeError, match="request_timeout"):
        await manager.snapshot(session.id)
    assert socket.closed and socket.sent.qsize() == 1
    assert not session.pending and not manager.sessions


async def test_disconnect_fails_pending():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    task = asyncio.create_task(manager.snapshot(session.id))
    await socket.sent.get()
    await manager.disconnect(session.id)
    with pytest.raises(BridgeError, match="connection_closed"):
        await task
    assert not session.pending


async def test_serial_lock_and_no_duplicate_submission():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    first = asyncio.create_task(manager.snapshot(session.id))
    request = await socket.sent.get()
    second = asyncio.create_task(manager.snapshot(session.id))
    await asyncio.sleep(0)
    assert socket.sent.empty()
    reply(manager, session, request, {"tabs": []})
    reply(manager, session, request, {"tabs": []})
    await first
    request2 = await socket.sent.get()
    assert request2["id"] != request["id"]
    reply(manager, session, request2, {"tabs": []})
    await second


async def test_open_project_is_explicitly_unsupported_for_legacy_extension():
    manager = Manager(SECRET)
    session, _ = pair(manager)
    with pytest.raises(BridgeError, match="open_project_unsupported_legacy_extension"):
        await manager.open_project(session.id, str(uuid4()))


def test_legacy_browser_status_does_not_invent_browser_data():
    manager = Manager(SECRET)
    session, _ = pair(manager)
    assert manager.browser_status(session.id) == {
        "connection_id": session.id,
        "backend": "extension",
        "browser_state": "unsupported",
        "auth_state": "unknown",
        "project_id": None,
        "url": None,
    }


async def test_select_scope_and_invalidation():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    task = asyncio.create_task(manager.select(session.id, 7))
    request = await socket.sent.get()
    reply(manager, session, request, SCOPE)
    await task
    task = asyncio.create_task(manager.account(session.id))
    request = await socket.sent.get()
    manager.invalidate(session)
    reply(manager, session, request, {"scope": SCOPE})
    with pytest.raises(BridgeError, match="scope_changed"):
        await task
    assert session.selected is None


async def test_unknown_account_is_honest_and_sensitive_data_rejected():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    session.selected = Scope.model_validate(SCOPE)
    task = asyncio.create_task(manager.account(session.id))
    request = await socket.sent.get()
    reply(manager, session, request, {"scope": SCOPE})
    result = await task
    assert result["email"] is None and result["email_confirmed"] is False
    assert result["credits"] is None and result["observed_at"] is None
    assert SECRET not in json.dumps(result)
    task = asyncio.create_task(manager.account(session.id))
    request = await socket.sent.get()
    reply(manager, session, request, {"scope": SCOPE, "cookie": "sensitive"})
    with pytest.raises(BridgeError, match="^invalid_response$"):
        await task
    assert session.selected is None


@pytest.mark.parametrize(
    "extra",
    [
        {"email": "someone@example.com"},
        {"credits": 10},
        {"email_confirmed": True},
        {"token": "secret"},
    ],
)
def test_observation_schema(extra):
    with pytest.raises(ValidationError):
        Observation.model_validate({"scope": SCOPE, **extra})


async def test_verified_account_freshness_and_switch_invalidation():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    session.selected = Scope.model_validate(SCOPE)
    evidence = {
        "scope": SCOPE,
        "email": "reader@example.test",
        "email_confirmed": True,
        "source": "google_account_aria",
        "credits": 143,
        "credits_source": "account_settings_dialog",
        "observed_at": "2026-10-03T12:00:00.000Z",
    }
    for closed in [False, True]:
        task = asyncio.create_task(manager.account(session.id))
        request = await socket.sent.get()
        payload = dict(evidence)
        if closed:
            payload.update(credits=None, credits_source="unavailable")
        reply(manager, session, request, payload)
        result = await task
        assert result["observed_at"] == evidence["observed_at"]
        assert result["freshness"] == "observed"
        assert result["credits_freshness"] == ("unknown" if closed else "observed")
        assert result["credits"] == (None if closed else 143)
    task = asyncio.create_task(manager.account(session.id))
    request = await socket.sent.get()
    reply(manager, session, request, {**evidence, "email": "other@example.test"})
    with pytest.raises(BridgeError, match="scope_changed"):
        await task
    assert session.selected is None
    assert session.observed_email is None


@pytest.mark.parametrize("credits", [True, "143", -1, 1.5, 9007199254740992])
def test_credits_are_strict_nonnegative_safe_integers(credits):
    with pytest.raises(ValidationError):
        Observation.model_validate({
            "scope": SCOPE, "email": "reader@example.test", "email_confirmed": True,
            "source": "google_account_aria", "credits": credits,
            "credits_source": "account_settings_dialog",
            "observed_at": "2026-10-03T12:00:00.000Z",
        })
