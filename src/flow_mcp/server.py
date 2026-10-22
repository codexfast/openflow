import argparse
import logging
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from flow_mcp.bridge import load_secret
from flow_mcp.jobs import Jobs
from flow_mcp.managed import backend_lifespan
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import TabId
from flow_mcp.operations import (
    CatalogLimit,
    Identifier,
    ImageInput,
    ImageUpscaleInput,
    ProjectTitle,
    VideoInput,
    VideoUpscaleInput,
)
from flow_mcp.pairing import Pairings
from flow_mcp.service import Operations, upload_input


def create_server(
    manager: Manager,
    port: int,
    jobs: Jobs | None = None,
    host: str = "127.0.0.1",
    mcp_port: int = 8000,
    backend: str = "extension",
) -> FastMCP[None]:
    operations = Operations(manager, jobs if jobs is not None else Jobs())

    @asynccontextmanager
    async def lifespan(app: FastMCP[None]) -> AsyncIterator[None]:
        async with backend_lifespan(manager, port, backend):
            yield None

    app: FastMCP[None] = FastMCP(
        "openflow",
        lifespan=lifespan,
        host=host,
        port=mcp_port,
        log_level="CRITICAL",
        instructions="Use the managed browser (run openflow setup for manual login first). "
        "List connections; previews navigate to the requested project before binding. "
        "In legacy extension mode only, ask the user "
        "to confirm the pairing ID shown in their extension before approve_connection. "
        "Never approve automatically. Authorization is once per installation. "
        "Pin a project tab before operations. Preview generation/upload with confirm=false, "
        "show the preview, then confirm=true with the same inputs and preview_id only after "
        "human approval. Never retry submitted_unverified jobs. "
        "Account and credits may be unknown.",
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    async def safe(work: Awaitable[dict[str, object]]) -> dict[str, object]:
        try:
            return await work
        except (BridgeError, ValueError) as error:
            return {"error": str(error)[:240]}
        except Exception:
            return {"error": "internal_error"}

    @app.tool(
        description="List paired browser profiles; does not inspect pages.", annotations=readonly
    )
    async def list_connections() -> dict[str, object]:
        return {
            "connections": [
                {
                    "connection_id": session.id,
                    "label": "Browser connection",
                    "state": "connected",
                    "installation_id": session.id,
                    "connected_at": session.connected_at,
                }
                for session in manager.sessions.values()
            ],
            "pending_pairings": manager.pairings.listed(),
        }

    @app.tool(
        description="List pending authorization requests, with no credentials.",
        annotations=readonly,
    )
    async def list_pairing_requests() -> dict[str, object]:
        return {"pending_pairings": manager.pairings.listed()}

    @app.tool(
        description="Authorize ONLY after the user explicitly confirms the pairing ID "
        "displayed in their extension. Never approve automatically."
    )
    async def approve_connection(pairing_id: str) -> dict[str, object]:
        return await safe(manager.pairings.approve(pairing_id))

    @app.tool(
        description="Revoke installation authorization and disconnect; approval required again."
    )
    async def revoke_connection(connection_id: str) -> dict[str, object]:
        async def work() -> dict[str, object]:
            manager.pairings.revoke(connection_id)
            for pending in list(manager.pairings.pending.values()):
                if pending.installation_id == connection_id:
                    manager.pairings.remove_socket(pending.socket)
                    await pending.socket.close(1000, "revoked")
            if connection_id in manager.sessions:
                session = manager.resolve(connection_id)
                manager.disconnected(session)
                await session.socket.close(1000, "revoked")
            return {"revoked": True}

        return await safe(work())

    @app.tool(
        description="Discover Flow tabs without activating them. Auto-selects only a sole profile.",
        annotations=readonly,
    )
    async def get_connection(connection_id: str | None = None) -> dict[str, object]:
        return await safe(manager.snapshot(connection_id))

    @app.tool(
        description="Pin an exact Flow tab and document, without navigating or activating it."
    )
    async def select_flow_tab(tab_id: TabId, connection_id: str | None = None) -> dict[str, object]:
        return await safe(manager.select(connection_id, tab_id))

    @app.tool(
        description="Read visible account evidence and, in managed browser mode, current credits "
        "by HTTPS without opening panels. Unavailable balances remain unknown.",
        annotations=readonly,
    )
    async def get_account_status(connection_id: str | None = None) -> dict[str, object]:
        return await safe(manager.account(connection_id))

    @app.tool(
        description="Refresh account evidence and credits. Managed browser queries credits "
        "by HTTPS without opening panels; unavailable balances remain unknown. "
        "Legacy extension may open account details.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def refresh_account(connection_id: str | None = None) -> dict[str, object]:
        return await safe(manager.account(connection_id, refresh=True))

    @app.tool(
        description="Read remote media referenced by project scenes, not all project media. "
        "Returns image/video kind when identified, otherwise unknown, and optional temporary "
        "thumbnail_url. Completeness remains unknown. Limit 1..100; truncated is local only, "
        "no cursor or automatic pagination. No generation, downloads or ownership ledger.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    )
    async def list_project_media(
        connection_id: Identifier, project_id: Identifier, limit: CatalogLimit = 100
    ) -> dict[str, object]:
        return await safe(manager.list_project_media(connection_id, project_id, limit))

    @app.tool(description="List projects by HTTPS; managed browser returns to Flow home.")
    async def list_projects(connection_id: Identifier) -> dict[str, object]:
        return await safe(manager.projects(connection_id))

    @app.tool(
        description="Preview creation in an exact pinned Flow home tab. After human approval, "
        "confirm with the same preview_id. Sends jHPbke once; "
        "managed browser then opens the project; "
        "uncertain outcomes are never resubmitted. Optional title defaults to OpenFlow project. "
        "Use the same title on confirmation. No paid generation."
    )
    async def create_project(
        connection_id: Identifier,
        confirm: bool = False,
        preview_id: Identifier | None = None,
        diagnostics: bool = False,
        title: ProjectTitle = "OpenFlow project",
    ) -> dict[str, object]:
        return await safe(
            operations.create_project(connection_id, confirm, preview_id, diagnostics, title)
        )

    @app.tool(
        description="Preview irreversible project deletion (managed browser only). Requires "
        "visible verified account and real title in home list (first 100). Show preview; "
        "confirm same preview_id only after human approval. Never retry unknown outcomes.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
    )
    async def delete_project(
        connection_id: Identifier, project_id: Identifier, confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        return await safe(operations.delete_project(connection_id, project_id, confirm, preview_id))

    @app.tool(description="Disconnect one profile; never reroutes pending requests to another.")
    async def disconnect_connection(connection_id: str) -> dict[str, object]:
        async def work() -> dict[str, object]:
            await manager.disconnect(connection_id)
            return {"disconnected": True}

        return await safe(work())

    @app.tool(
        description=(
            "Preview one image; image generation is currently free. "
            "Human confirmation is still required. "
            "References must be uploaded/generated in this exact scope. "
            "Optional seed fixes the input seed, not guaranteed identical output."
        )
    )
    async def generate_image(
        connection_id: Identifier,
        project_id: Identifier,
        input: ImageInput,
        confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        return await safe(
            operations.submit(
                "generate_image",
                connection_id,
                project_id,
                input,
                confirm,
                preview_id,
            )
        )

    @app.tool(
        description=(
            "Preview video; confirm only after approval. Omni: text, start, start+end, "
            "or references, 4/6/8/10s, 360p/720p. Veo lite: start-only 8s 720p."
        )
    )
    async def generate_video(
        connection_id: Identifier,
        project_id: Identifier,
        input: VideoInput,
        confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        return await safe(
            operations.submit(
                "generate_video",
                connection_id,
                project_id,
                input,
                confirm,
                preview_id,
            )
        )

    @app.tool(
        description=(
            "Preview upload of explicit local absolute file or base64 PNG/JPEG/WebP, max 8MiB. "
            "No URLs. Repeat same bytes to confirm."
        )
    )
    async def upload(
        connection_id: Identifier,
        project_id: Identifier,
        file_path: str | None = None,
        data_base64: str | None = None,
        name: str = "upload.png",
        confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        async def work() -> dict[str, object]:
            value = upload_input(file_path, data_base64, name)
            return await operations.submit(
                "upload",
                connection_id,
                project_id,
                value,
                confirm,
                preview_id,
            )

        return await safe(work())

    @app.tool(
        description=(
            "Preview image upscale 2K/4K; confirmation consumes credits. "
            "4K requires ultra tier. Media must be owned in this exact scope."
        )
    )
    async def upscale_image(
        connection_id: Identifier,
        project_id: Identifier,
        input: ImageUpscaleInput,
        confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        return await safe(
            operations.submit(
                "upscale_image",
                connection_id,
                project_id,
                input,
                confirm,
                preview_id,
            )
        )

    @app.tool(
        description=(
            "Preview video upscale to 1080p; confirmation consumes credits. "
            "Needs a real scene_id from the Flow UI. Media id gains _upsampled suffix."
        )
    )
    async def upscale_video(
        connection_id: Identifier,
        project_id: Identifier,
        input: VideoUpscaleInput,
        confirm: bool = False,
        preview_id: Identifier | None = None,
    ) -> dict[str, object]:
        return await safe(
            operations.submit(
                "upscale_video",
                connection_id,
                project_id,
                input,
                confirm,
                preview_id,
            )
        )

    @app.tool(
        description=(
            "Read persistent job, optionally poll only its original exact browser scope; "
            "never resubmits."
        ),
        annotations=readonly,
    )
    async def get_job(
        job_id: Identifier,
        connection_id: Identifier,
        project_id: Identifier,
        refresh: bool = False,
    ) -> dict[str, object]:
        return await safe(operations.get(job_id, connection_id, project_id, refresh))

    @app.tool(
        description="Read cached persistent job status without a browser request.",
        annotations=readonly,
    )
    async def status(
        job_id: Identifier,
        connection_id: Identifier,
        project_id: Identifier,
    ) -> dict[str, object]:
        return await safe(operations.get(job_id, connection_id, project_id, False))

    @app.tool(
        description=(
            "Download only an asset from a completed scoped job to explicit new local file. "
            "No URL input, redirects, overwrites, or proxy environment."
        )
    )
    async def download(
        job_id: Identifier,
        connection_id: Identifier,
        project_id: Identifier,
        destination: str,
        asset_index: TabId = 0,
    ) -> dict[str, object]:
        return await safe(
            operations.download(
                job_id,
                connection_id,
                project_id,
                destination,
                asset_index,
            )
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="openflow")
    parser.add_argument("--backend", choices=["browser", "extension"], default="browser")
    parser.add_argument("--port", type=int, default=9233)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    parser.add_argument("--pairing-dir", type=Path, default=base / "openflow")
    parser.add_argument("--init-pairing", action="store_true")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mcp-port", type=int, default=8000)
    args: Any = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 9222:
        parser.error("port must be 1024..65535, excluding 9222")
    if not 1024 <= args.mcp_port <= 65535:
        parser.error("mcp-port must be 1024..65535")
    logging.disable(logging.CRITICAL)
    try:
        secret = ""
        if args.init_pairing or (
            args.backend == "extension" and (args.pairing_dir / "secret").exists()
        ):
            secret = load_secret(args.pairing_dir)
        if not args.init_pairing:
            args.pairing_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            app = create_server(
                Manager(secret, pairings=Pairings(args.pairing_dir)),
                args.port,
                Jobs(args.pairing_dir / "jobs.sqlite3"),
                host=args.host,
                mcp_port=args.mcp_port,
                backend=args.backend,
            )
            app.run(transport=args.transport)
    except Exception:
        parser.exit(
            1,
            "openflow: startup failed; check browser login/profile lock, "
            "OS keyring session and backend ports. Run openflow setup if needed.\n",
        )


if __name__ == "__main__":
    main()
