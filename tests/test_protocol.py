import asyncio
import json
import socket
import sys
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from flow_mcp.bridge import bridge, load_secret
from flow_mcp.manager import Manager


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_bridge_rejects_invalid_pairing_and_duplicate(tmp_path):
    secret = load_secret(tmp_path)
    assert load_secret(tmp_path) == secret
    port = free_port()
    manager = Manager(secret)
    hello = {
        "type": "hello",
        "version": 1,
        "secret": secret,
        "installation_id": str(uuid4()),
        "nickname": "profile",
    }
    async with bridge(manager, port):
        async with connect(
            f"ws://127.0.0.1:{port}", origin="chrome-extension://" + "a" * 32
        ) as bad:
            await bad.send(json.dumps({**hello, "secret": "x" * 64}))
            with pytest.raises(ConnectionClosed):
                await bad.recv()
        async with connect(
            f"ws://127.0.0.1:{port}", origin="chrome-extension://" + "a" * 32
        ) as good:
            await good.send(json.dumps(hello))
            assert json.loads(await good.recv())["type"] == "paired"
            async with connect(
                f"ws://127.0.0.1:{port}", origin="chrome-extension://" + "a" * 32
            ) as duplicate:
                await duplicate.send(json.dumps(hello))
                with pytest.raises(ConnectionClosed):
                    await duplicate.recv()
            await good.send('{"type":"ping"}')
            assert json.loads(await good.recv()) == {"type": "pong"}


async def test_mcp_stdio_with_simulated_extension(tmp_path):
    port = free_port()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flow_mcp.server", "--backend", "extension",
              "--pairing-dir", str(tmp_path), "--port", str(port)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == {
                "list_connections",
                "get_connection",
                "select_flow_tab",
                "get_account_status",
                "refresh_account",
                "create_project",
                "delete_project",
                "list_projects",
                "list_project_media",
                "find_project_media",
                "open_project",
                "browser_status",
                "disconnect_connection",
                "list_pairing_requests",
                "approve_connection",
                "revoke_connection",
                "upload",
                "generate_image",
                "generate_video",
                "upscale_image",
                "upscale_video",
                "get_job",
                "recover_job",
                "status",
                "download",
            }
            schema = next(t for t in tools if t.name == "select_flow_tab").inputSchema
            assert schema["properties"]["tab_id"]["type"] == "integer"
            async with connect(
                f"ws://127.0.0.1:{port}", origin="chrome-extension://" + "a" * 32
            ) as ws:
                identity = str(uuid4())
                await ws.send(
                    json.dumps(
                        {
                            "type": "pair",
                            "version": 1,
                            "installation_id": identity,
                        }
                    )
                )
                pending = json.loads(await ws.recv())
                listed = await client.call_tool("list_pairing_requests", {})
                assert pending["pairing_id"] in str(listed)
                approved = await client.call_tool(
                    "approve_connection", {"pairing_id": pending["pairing_id"]}
                )
                secret = json.loads(await ws.recv())["secret"]
                assert secret not in str(approved)
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "version": 1,
                            "installation_id": identity,
                            "secret": secret,
                        }
                    )
                )
                assert json.loads(await ws.recv())["type"] == "paired"
                listed = await client.call_tool("list_connections", {})
                assert identity in str(listed)
                assert secret not in str(listed)
                scope = {
                    "tab_id": 5,
                    "origin": "https://flow.google.com",
                    "project_id": str(uuid4()),
                    "document_id": "doc-5",
                }

                async def respond(result):
                    request = json.loads(await ws.recv())
                    await ws.send(
                        json.dumps({"type": "reply", "id": request["id"], "result": result})
                    )
                    return request

                responder = asyncio.create_task(
                    respond(
                        {
                            "tabs": [{k: v for k, v in scope.items() if k != "document_id"}],
                            "selected": None,
                        }
                    )
                )
                result = await client.call_tool("get_connection", {})
                await responder
                assert not result.isError and "tab_id" in str(result)
                responder = asyncio.create_task(respond(scope))
                result = await client.call_tool("select_flow_tab", {"tab_id": 5})
                assert (await responder)["params"] == {"tab_id": 5}
                assert not result.isError
                responder = asyncio.create_task(respond({"scope": scope}))
                result = await client.call_tool("get_account_status", {})
                request = await responder
                assert request["params"]["scope"] == scope
                assert not result.isError
                assert "unavailable" in str(result) and secret not in str(result)
                responder = asyncio.create_task(respond({"scope": scope}))
                result = await client.call_tool("refresh_account", {})
                request = await responder
                assert request["operation"] == "refresh_account"
                assert request["params"]["scope"] == scope
                assert not result.isError
                annotation = next(t for t in tools if t.name == "refresh_account").annotations
                assert annotation.readOnlyHint is False
                arguments = {
                    "connection_id": identity,
                    "project_id": scope["project_id"],
                    "input": {"prompt": "synthetic test", "seed": 42},
                }
                preview = await client.call_tool("generate_image", arguments)
                preview_data = json.loads(preview.content[0].text)
                assert preview_data["state"] == "preview"
                assert preview_data["confirmation_required"] is True
                media = str(uuid4())
                responder = asyncio.create_task(
                    respond(
                        {
                            "scope": scope,
                            "state": "completed",
                            "assets": [
                                {
                                    "media_id": media,
                                    "kind": "image",
                                    "url": f"https://flow-content.google/image/{media}?Signature=synthetic",
                                }
                            ],
                        }
                    )
                )
                confirmed = await client.call_tool(
                    "generate_image",
                    {
                        **arguments,
                        "confirm": True,
                        "preview_id": preview_data["job_id"],
                    },
                )
                request = await responder
                assert request["operation"] == "generate_image"
                assert request["params"]["scope"] == scope
                assert request["params"]["input"]["seed"] == 42
                assert json.loads(confirmed.content[0].text)["state"] == "completed"
                repeated = await client.call_tool(
                    "generate_image",
                    {
                        **arguments,
                        "confirm": True,
                        "preview_id": preview_data["job_id"],
                    },
                )
                assert json.loads(repeated.content[0].text)["not_resubmitted"] is True
                wrong = await client.call_tool(
                    "get_job",
                    {
                        "job_id": preview_data["job_id"],
                        "connection_id": str(uuid4()),
                        "project_id": scope["project_id"],
                    },
                )
                assert "job_scope_mismatch" in str(wrong)
                home = {**scope, "project_id": None}
                responder = asyncio.create_task(respond(home))
                await client.call_tool("select_flow_tab", {"tab_id": 5})
                await responder
                preview = await client.call_tool("create_project", {
                    "connection_id": identity, "title": "Título MCP",
                })
                creation = json.loads(preview.content[0].text)
                assert creation["preview"]["rpc"] == "jHPbke"
                assert creation["preview"]["title"] == "Título MCP"
                assert creation["preview"]["navigation"] is False
                derived = {**home, "project_id": str(uuid4())}
                responder = asyncio.create_task(respond({
                    "scope": home, "state": "completed", "selected": derived,
                }))
                result = await client.call_tool("create_project", {
                    "connection_id": identity, "confirm": True,
                    "preview_id": creation["job_id"], "title": "Título MCP",
                })
                request = await responder
                assert request["operation"] == "create_project"
                assert request["params"]["title"] == "Título MCP"
                assert json.loads(result.content[0].text)["state"] == "completed"
                preview = await client.call_tool("generate_image", {
                    "connection_id": identity, "project_id": derived["project_id"],
                    "input": {"prompt": "derived scope preview only"},
                })
                assert json.loads(preview.content[0].text)["scope"] == derived
                result = await client.call_tool(
                    "disconnect_connection", {"connection_id": identity}
                )
                assert not result.isError
