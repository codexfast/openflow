import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from flow_mcp.jobs import Jobs
from flow_mcp.manager import Manager
from flow_mcp.models import Scope
from flow_mcp.server import create_server, streamable_http_service


async def test_http_clients_share_service_browser_and_pending_job(monkeypatch, tmp_path):
    import flow_mcp.server as server

    manager = Manager()
    driver = AsyncMock()
    driver.send = AsyncMock()
    driver.close = AsyncMock()
    entries = 0
    exits = 0

    @asynccontextmanager
    async def backend(_manager, _port, _backend):
        nonlocal entries, exits
        entries += 1
        session = _manager.register_local(driver, str(uuid4()))
        try:
            yield None
        finally:
            exits += 1
            _manager.disconnected(session)

    monkeypatch.setattr(server, "backend_lifespan", backend)
    jobs = Jobs(tmp_path / "jobs.sqlite3")
    app = create_server(manager, 9233, jobs, backend="browser")
    scope = Scope(tab_id=1, origin="https://flow.google.com",
                  project_id=str(uuid4()), document_id="doc-1")
    service = streamable_http_service(app, manager, 9233, "browser")

    async with service.router.lifespan_context(service):
        assert entries == 1
        connection = next(iter(manager.sessions))
        job = jobs.create(connection, scope, "generate_image", {})
        jobs.update(job, "pending", {"operation_id": str(uuid4())})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://127.0.0.1:8000"
        ) as http:
            async def connect():
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("list_connections", {})
                        assert result.structuredContent is not None
                        listed = result.structuredContent["connections"]
                        assert listed[0]["connection_id"] == connection

            await connect()
            assert entries == 1 and exits == 0
            await connect()
            await asyncio.gather(connect(), connect())
            assert entries == 1 and exits == 0
            assert len(manager.sessions) == 1
            assert jobs.get(job)["state"] == "pending"
            assert jobs.get(job)["connection_id"] == connection
    assert exits == 1
    assert not manager.sessions
