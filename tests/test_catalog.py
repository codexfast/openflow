import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from test_manager import SECRET, pair, reply

from flow_mcp.api import create_app
from flow_mcp.jobs import Jobs
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import Scope
from flow_mcp.operations import CatalogInput, CatalogItem, ProjectMediaCatalog, ProjectMediaLookup
from flow_mcp.server import create_server


@pytest.mark.parametrize("kind", ["image", "video", "unknown"])
def test_catalog_metadata_and_legacy_defaults(kind):
    value = {"media_id": str(uuid4()), "scene_id": str(uuid4())}
    legacy = CatalogItem.model_validate(value)
    assert legacy.kind == "unknown" and legacy.thumbnail_url is None
    item = CatalogItem.model_validate({
        **value, "kind": kind,
        "thumbnail_url": "https://lh3.googleusercontent.com/synthetic-thumb=w320",
    })
    assert item.kind == kind
    assert item.thumbnail_url.endswith("=w320")


@pytest.mark.parametrize("url", [
    "http://lh3.googleusercontent.com/thumb",
    "https://lh3.googleusercontent.com.evil.invalid/thumb",
    "https://user@lh3.googleusercontent.com/thumb",
    "https://lh3.googleusercontent.com:443/thumb",
    "https://lh3.googleusercontent.com/thumb#fragment",
    "https://lh3.googleusercontent.com/",
    "https://lh3.googleusercontent.com/thumb\n",
    "https://lh3.googleusercontent.com/thumb with space",
    "https://lh3.googleusercontent.com/" + "x" * 8192,
])
def test_catalog_rejects_unsafe_thumbnail(url):
    with pytest.raises(ValidationError):
        CatalogItem.model_validate({
            "media_id": str(uuid4()), "scene_id": str(uuid4()), "thumbnail_url": url,
        })


def setup():
    manager = Manager(SECRET)
    session, socket = pair(manager)
    scope = Scope(
        tab_id=7, origin="https://flow.google.com", project_id=str(uuid4()), document_id="doc"
    )
    session.selected = scope
    return manager, session, socket, scope


def result(scope, count=7):
    return {
        "scope": scope.model_dump(),
        "state": "completed",
        "items": [
            {"media_id": str(uuid4()), "scene_id": str(uuid4()), "kind": "unknown"}
            for _ in range(count)
        ],
        "source": "remote_project_scenes",
        "completeness": "unknown",
        "truncated": False,
    }


def lookup(scope, item=None):
    return {
        "scope": scope.model_dump(),
        "state": "completed",
        "observed": item is not None,
        "item": item,
        "reason": None if item is not None else "not_found_in_observed_catalog",
        "source": "remote_project_scenes",
        "completeness": "unknown",
    }


async def test_manager_prepares_verifies_under_lock_and_never_uses_jobs(monkeypatch):
    manager, session, socket, scope = setup()
    calls = []

    async def prepare(project_id):
        assert session.lock.locked()
        calls.append(project_id)
        session.selected = scope

    async def verify(actual):
        assert session.lock.locked() and actual == scope
        calls.append("verify")

    socket.prepare_project = prepare
    socket.verify_scope = verify
    session.selected = None
    task = asyncio.create_task(manager.list_project_media(session.id, scope.project_id))
    request = await socket.sent.get()
    assert request["operation"] == "list_project_media"
    assert request["params"] == {"scope": scope.model_dump(), "input": {"limit": 100}}
    reply(manager, session, request, result(scope))
    catalog = await task
    assert len(catalog["items"]) == 7
    assert calls == [scope.project_id, "verify", "verify"]
    assert socket.sent.empty()


@pytest.mark.parametrize(
    "change", ["scope", "extra", "state", "kind", "source", "oversized", "duplicate", "limit"]
)
async def test_manager_rejects_invalid_response(change):
    manager, session, socket, scope = setup()
    task = asyncio.create_task(manager.list_project_media(session.id, scope.project_id, 7))
    request = await socket.sent.get()
    raw = result(scope)
    if change == "scope":
        raw["scope"]["document_id"] = "foreign"
    elif change == "extra":
        raw["url"] = "https://private.invalid"
    elif change == "state":
        raw["state"] = "submitted_unverified"
    elif change == "kind":
        raw["items"][0]["kind"] = "audio"
    elif change == "source":
        raw["source"] = "other"
    elif change == "oversized":
        raw = result(scope, 101)
    elif change == "duplicate":
        raw["items"].append(raw["items"][0])
    else:
        raw = result(scope, 8)
    reply(manager, session, request, raw)
    with pytest.raises(BridgeError, match="scope_changed|invalid_catalog_response"):
        await task


async def test_manager_rejects_invalidation_during_read():
    manager, session, socket, scope = setup()
    task = asyncio.create_task(manager.list_project_media(session.id, scope.project_id))
    request = await socket.sent.get()
    manager.invalidate(session)
    reply(manager, session, request, result(scope))
    with pytest.raises(BridgeError, match="scope_changed"):
        await task


async def test_extension_requires_selected_matching_project():
    manager, session, socket, scope = setup()
    for selected in [None, scope.model_copy(update={"project_id": None})]:
        session.selected = selected
        with pytest.raises(BridgeError, match="project_scope_mismatch"):
            await manager.list_project_media(session.id, scope.project_id)
    assert socket.sent.empty()


@pytest.mark.parametrize("limit", [0, 101, True, "1", 1.5])
async def test_limits_fail_before_transport(limit):
    manager, session, socket, scope = setup()
    with pytest.raises(ValidationError):
        await manager.list_project_media(session.id, scope.project_id, limit)
    assert socket.sent.empty()


async def test_rest_catalog_and_mcp_contract(monkeypatch):
    manager, session, _, scope = setup()
    calls = []

    async def catalog(connection_id, project_id, limit=100):
        calls.append((connection_id, project_id, limit))
        return result(scope, 0)

    monkeypatch.setattr(manager, "list_project_media", catalog)
    app = create_app(manager, 19333, Jobs(":memory:"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/projects/{scope.project_id}/media"
        response = await client.get(path, params={"connection_id": session.id, "limit": 2})
        assert response.status_code == 200
        assert response.json()["completeness"] == "unknown"
        assert calls == [(session.id, scope.project_id, 2)]
        for limit in [0, 101, "bad"]:
            assert (
                await client.get(path, params={"connection_id": session.id, "limit": limit})
            ).status_code == 422
        assert (
            await client.get("/projects/bad/media", params={"connection_id": session.id})
        ).status_code == 422
    server = create_server(manager, 19333, Jobs(":memory:"))
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["list_project_media"]
    assert tool.annotations.readOnlyHint
    assert tool.inputSchema["properties"]["limit"]["maximum"] == 100
    await server.call_tool(
        "list_project_media",
        {"connection_id": session.id, "project_id": scope.project_id, "limit": 2},
    )
    assert len(calls) == 2


def test_strict_catalog_rejects_private_fields_and_coercions():
    _, _, _, scope = setup()
    raw = result(scope, 0)
    assert ProjectMediaCatalog.model_validate(raw).items == []
    for field, value in [("truncated", 1), ("completeness", "complete")]:
        with pytest.raises(ValidationError):
            ProjectMediaCatalog.model_validate({**raw, field: value})
    assert CatalogInput(limit=100).limit == 100


async def test_manager_accepts_upsampled_media_not_scene_ids():
    manager, session, socket, scope = setup()
    task = asyncio.create_task(manager.list_project_media(session.id, scope.project_id))
    request = await socket.sent.get()
    raw = result(scope, 1)
    raw["items"][0]["media_id"] += "_upsampled"
    reply(manager, session, request, raw)
    assert (await task)["items"][0]["media_id"].endswith("_upsampled")
    raw["items"][0]["scene_id"] += "_upsampled"
    with pytest.raises(ValidationError):
        ProjectMediaCatalog.model_validate(raw)


async def test_manager_finds_catalog_media_beyond_list_limit():
    manager, session, socket, scope = setup()
    target = str(uuid4())
    task = asyncio.create_task(manager.find_project_media(session.id, scope.project_id, target))
    request = await socket.sent.get()
    assert request["operation"] == "find_project_media"
    assert request["params"] == {"scope": scope.model_dump(), "input": {"media_id": target}}
    raw = lookup(scope, {"media_id": target, "scene_id": str(uuid4()), "kind": "unknown"})
    reply(manager, session, request, raw)
    found = await task
    assert found["observed"] is True
    assert found["item"]["media_id"] == target
    assert found["completeness"] == "unknown"


async def test_manager_lookup_absence_stays_partial_and_preserves_scope_checks():
    manager, session, socket, scope = setup()
    target = str(uuid4())
    task = asyncio.create_task(manager.find_project_media(session.id, scope.project_id, target))
    request = await socket.sent.get()
    reply(manager, session, request, lookup(scope))
    assert await task == {
        "connection_id": session.id,
        "scope": scope.model_dump(),
        "state": "completed",
        "observed": False,
        "item": None,
        "reason": "not_found_in_observed_catalog",
        "source": "remote_project_scenes",
        "completeness": "unknown",
    }
    session.selected = None
    with pytest.raises(BridgeError, match="project_scope_mismatch"):
        await manager.find_project_media(session.id, scope.project_id, target)


@pytest.mark.parametrize("media_id", ["external", str(uuid4()) + "_other", True])
async def test_manager_lookup_rejects_external_media_ids_before_transport(media_id):
    manager, session, socket, scope = setup()
    with pytest.raises(ValidationError):
        await manager.find_project_media(session.id, scope.project_id, media_id)
    assert socket.sent.empty()


async def test_rest_and_mcp_lookup_contract(monkeypatch):
    manager, session, _, scope = setup()
    target = str(uuid4()) + "_upsampled"
    calls = []

    async def lookup(connection_id, project_id, media_id):
        calls.append((connection_id, project_id, media_id))
        return {
            "connection_id": connection_id,
            "scope": scope.model_dump(),
            "state": "completed",
            "observed": False,
            "item": None,
            "reason": "not_found_in_observed_catalog",
            "source": "remote_project_scenes",
            "completeness": "unknown",
        }

    monkeypatch.setattr(manager, "find_project_media", lookup)
    app = create_app(manager, 19333, Jobs(":memory:"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/projects/{scope.project_id}/media/{target}"
        response = await client.get(path, params={"connection_id": session.id})
        assert response.status_code == 200
        assert response.json()["reason"] == "not_found_in_observed_catalog"
        assert (await client.get(
            f"/projects/{scope.project_id}/media/external", params={"connection_id": session.id}
        )).status_code == 422
    server = create_server(manager, 19333, Jobs(":memory:"))
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["find_project_media"]
    assert tool.annotations.readOnlyHint
    await server.call_tool(
        "find_project_media",
        {"connection_id": session.id, "project_id": scope.project_id, "media_id": target},
    )
    assert calls == [(session.id, scope.project_id, target), (session.id, scope.project_id, target)]


def test_lookup_model_requires_truthful_observation_shape():
    _, _, _, scope = setup()
    item = {"media_id": str(uuid4()), "scene_id": str(uuid4())}
    assert ProjectMediaLookup.model_validate({
        "scope": scope.model_dump(), "state": "completed", "observed": True, "item": item,
        "source": "remote_project_scenes", "completeness": "unknown",
    }).observed
    with pytest.raises(ValidationError):
        ProjectMediaLookup.model_validate({
            "scope": scope.model_dump(), "state": "completed", "observed": False, "item": item,
            "source": "remote_project_scenes", "completeness": "unknown",
        })
