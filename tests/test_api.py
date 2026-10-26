import re
from uuid import uuid4

import httpx
import pytest

from flow_mcp.api import create_app
from flow_mcp.jobs import Jobs
from flow_mcp.manager import Manager
from flow_mcp.server import create_server


def make_client(backend="extension"):
    manager = Manager("")
    app = create_app(manager, 19333, Jobs(":memory:"), backend=backend)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health_and_connections():
    async with make_client() as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "connections": 0}
        response = await client.get("/connections")
        assert response.status_code == 200
        assert response.json()["connections"] == []


async def test_openapi_exposed():
    async with make_client() as client:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == "application/json"
        spec = response.json()
        assert isinstance(spec, dict)
        assert spec["openapi"] == "3.1.0"
        assert set(spec["info"]) == {"title", "version", "description"}
        assert spec["info"]["title"] == "OpenFlow API"
        assert spec["info"]["version"] == "0.1.0"
        assert "Backend: extension" in spec["info"]["description"]
        assert "/images" in spec["paths"]
        assert "/videos" in spec["paths"]
        assert "/images/upscale" in spec["paths"]
        assert "/videos/upscale" in spec["paths"]
        assert "/uploads" in spec["paths"]
        assert spec["components"]["schemas"]["ImageInput"]["type"] == "object"
        assert "prompt" in spec["components"]["schemas"]["ImageInput"]["properties"]


@pytest.mark.parametrize(
    ("path", "schema_pattern"),
    [
        ("/docs", r"""url:\s*['"]/openapi\.json['"]"""),
        ("/redoc", r"""<redoc\b[^>]*\bspec-url=['"]/openapi\.json['"]"""),
    ],
)
async def test_documentation_pages_exposed(path, schema_pattern):
    async with make_client() as client:
        docs = await client.get(path)
        assert docs.status_code == 200
        assert docs.headers["content-type"].split(";")[0] == "text/html"
        assert "<html" in docs.text.lower()
        assert re.search(schema_pattern, docs.text)


@pytest.mark.parametrize("backend", ["browser", "extension"])
async def test_openapi_matches_backend(backend):
    legacy_paths = {
        "/pairings",
        "/pairings/{pairing_id}/approve",
        "/connections/{connection_id}/select",
    }
    async with make_client(backend) as client:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        for path in legacy_paths:
            assert (path in spec["paths"]) == (backend == "extension")
        for path in (
            "/health", "/connections", "/connections/{connection_id}",
            "/connections/{connection_id}/account", "/connections/{connection_id}/browser-status",
            "/projects", "/projects/{project_id}/open", "/images", "/videos",
        ):
            assert path in spec["paths"]
        assert f"Backend: {backend}" in spec["info"]["description"]


@pytest.mark.parametrize("backend", ["browser", "extension"])
async def test_legacy_routes_remain_available(backend):
    async with make_client(backend) as client:
        response = await client.get("/pairings")
        assert response.status_code == 200
        assert response.json() == {"pending_pairings": []}
        response = await client.post("/pairings/nope/approve")
        assert response.status_code == 409
        response = await client.post("/connections/nope/select", json={"tab_id": 1})
        assert response.status_code == 409


async def test_rest_browser_status_and_open_project_legacy_errors_are_explicit():
    from test_manager import pair

    manager = Manager("s" * 64)
    session, _ = pair(manager)
    project_id = str(uuid4())
    app = create_app(manager, 19333, Jobs(":memory:"), backend="extension")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get(f"/connections/{session.id}/browser-status")
        opened = await client.post(
            f"/projects/{project_id}/open", params={"connection_id": session.id}
        )
    assert status.status_code == 200
    assert status.json() == {
        "connection_id": session.id,
        "backend": "extension",
        "browser_state": "unsupported",
        "auth_state": "unknown",
        "project_id": None,
        "url": None,
    }
    assert opened.status_code == 409
    assert opened.json()["detail"] == "open_project_unsupported_legacy_extension"


async def test_rest_open_project_rejects_invalid_uuid_before_navigation():
    connection_id = str(uuid4())
    async with make_client("browser") as client:
        opened = await client.post(
            "/projects/not-a-uuid/open", params={"connection_id": connection_id}
        )
    assert opened.status_code == 422


async def test_mcp_explicit_tools_include_legacy_safe_status():
    from test_manager import pair

    manager = Manager("s" * 64)
    session, _ = pair(manager)
    server = create_server(manager, 19333, Jobs(":memory:"), backend="extension")
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"open_project", "browser_status"}.issubset(tools)
    result = await server.call_tool("browser_status", {"connection_id": session.id})
    assert "unsupported" in str(result)
    rejected = await server.call_tool(
        "open_project", {"connection_id": session.id, "project_id": str(uuid4())}
    )
    assert "open_project_unsupported_legacy_extension" in str(rejected)


async def test_invalid_inputs_are_400_not_500():
    async with make_client() as client:
        bad_image = {
            "connection_id": str(uuid4()),
            "project_id": str(uuid4()),
            "input": {"prompt": "", "model": "nano_banana_2", "ratio": "1:1",
                      "references": []},
        }
        response = await client.post("/images", json=bad_image)
        assert response.status_code == 422
        bad_video = {
            "connection_id": str(uuid4()),
            "project_id": str(uuid4()),
            "input": {"prompt": "x", "model": "veo_lite", "ratio": "16:9",
                      "duration": 8, "resolution": "720p",
                      "start": None, "end": None, "references": []},
        }
        response = await client.post("/videos", json=bad_video)
        assert response.status_code == 422
        response = await client.post("/pairings/nope/approve")
        assert response.status_code == 409


@pytest.mark.parametrize("title", [None, True, 1, "", " \t", "x" * 201])
async def test_rest_rejects_invalid_project_titles(title):
    async with make_client() as client:
        for path in ("/projects", "/projects/preview"):
            response = await client.post(path, json={"connection_id": str(uuid4()), "title": title})
            assert response.status_code == 422

@pytest.mark.parametrize("seed", [True, "42", 1.0, 1.5, 0, -1, 1_000_000_001])
async def test_rest_rejects_invalid_image_seed(seed):
    async with make_client() as client:
        response = await client.post("/images", json={
            "connection_id": str(uuid4()), "project_id": str(uuid4()),
            "input": {"prompt": "x", "seed": seed},
        })
        assert response.status_code == 422

@pytest.mark.parametrize("path", ["/projects", "/projects/preview"])
async def test_rest_project_title_preview_confirmation(tmp_path, path):
    import asyncio

    from test_manager import reply
    from test_operations import setup

    manager, session, socket, scope, jobs, _ = setup(tmp_path)
    home = scope.model_copy(update={"project_id": None})
    session.selected = home
    app = create_app(manager, 19333, jobs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"connection_id": session.id, "title": "Título REST"}
        preview = (await client.post(path, json=body)).json()
        assert preview["preview"]["title"] == body["title"]
        confirmation = {**body, "confirm": True, "preview_id": preview["job_id"]}
        mismatch = await client.post(path, json={**confirmation, "title": "changed"})
        assert mismatch.status_code == 409
        assert socket.sent.empty()
        task = asyncio.create_task(client.post(path, json=confirmation))
        request = await socket.sent.get()
        assert request["params"]["title"] == body["title"]
        reply(manager, session, request, {
            "state": "completed", "scope": home.model_dump(), "selected": scope.model_dump(),
        })
        assert (await task).json()["state"] == "completed"

@pytest.mark.parametrize("seed", [None, 1, 1_000_000_000])
async def test_rest_image_seed_preview_confirmation(tmp_path, seed):
    import asyncio

    from test_manager import reply
    from test_operations import setup

    manager, session, socket, scope, jobs, _ = setup(tmp_path)
    app = create_app(manager, 19333, jobs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"connection_id": session.id, "project_id": scope.project_id,
                "input": {"prompt": "x", "seed": seed}}
        preview = (await client.post("/images", json=body)).json()
        assert preview["preview"]["seed"] == seed
        task = asyncio.create_task(client.post("/images", json={
            **body, "confirm": True, "preview_id": preview["job_id"],
        }))
        request = await socket.sent.get()
        assert request["params"]["input"]["seed"] == seed
        reply(manager, session, request, {"scope": scope.model_dump(), "state": "not_submitted"})
        assert (await task).json()["state"] == "not_submitted"
