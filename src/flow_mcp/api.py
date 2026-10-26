import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from flow_mcp.bridge import load_secret
from flow_mcp.jobs import Jobs
from flow_mcp.managed import backend_lifespan
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.models import StrictModel, TabId
from flow_mcp.operations import (
    Identifier,
    ImageInput,
    ImageUpscaleInput,
    MediaIdentifier,
    ProjectTitle,
    UploadInput,
    VideoInput,
    VideoUpscaleInput,
)
from flow_mcp.pairing import Pairings
from flow_mcp.service import Operations, upload_input


def error_status(error: Exception) -> tuple[int, str]:
    if isinstance(error, BridgeError):
        return 409, str(error)
    if isinstance(error, ValueError | ValidationError):
        return 400, str(error)[:240] or "invalid_request"
    return 500, "internal_error"


def create_app(
    manager: Manager, port: int, jobs: Jobs | None = None, backend: str = "extension"
) -> FastAPI:
    operations = Operations(manager, jobs if jobs is not None else Jobs())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with backend_lifespan(manager, port, backend):
            yield None

    description = (
        "Backend: browser. Uses a managed Chromium session authenticated through "
        "manual Google login with openflow setup. No extension pairing is required."
        if backend == "browser"
        else "Backend: extension (legacy). Requires manual approval of the pairing "
        "code shown in the extension and selection of a Flow tab."
    )
    app = FastAPI(
        title="OpenFlow API", version="0.1.0", description=description, lifespan=lifespan
    )

    def fail(error: Exception) -> HTTPException:
        status, message = error_status(error)
        return HTTPException(status_code=status, detail=message)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "connections": len(manager.sessions)}

    @app.get("/connections")
    async def connections() -> dict[str, object]:
        return {
            "connections": [
                {
                    "connection_id": session.id,
                    "connected_at": session.connected_at,
                }
                for session in manager.sessions.values()
            ],
            "pending_pairings": manager.pairings.listed(),
        }

    @app.get("/pairings", include_in_schema=backend == "extension")
    async def pairings() -> dict[str, object]:
        return {"pending_pairings": manager.pairings.listed()}

    @app.post("/pairings/{pairing_id}/approve", include_in_schema=backend == "extension")
    async def approve(pairing_id: str) -> dict[str, object]:
        try:
            return await manager.pairings.approve(pairing_id)
        except Exception as error:
            raise fail(error) from None

    @app.get("/connections/{connection_id}")
    async def snapshot(connection_id: str) -> dict[str, object]:
        try:
            return await manager.snapshot(connection_id)
        except Exception as error:
            raise fail(error) from None

    class SelectBody(StrictModel):
        tab_id: TabId

    @app.post("/connections/{connection_id}/select", include_in_schema=backend == "extension")
    async def select(connection_id: str, body: SelectBody) -> dict[str, object]:
        try:
            return await manager.select(connection_id, body.tab_id)
        except Exception as error:
            raise fail(error) from None

    @app.get("/connections/{connection_id}/browser-status")
    async def browser_status(connection_id: Identifier) -> dict[str, object]:
        try:
            return manager.browser_status(connection_id)
        except Exception as error:
            raise fail(error) from None

    @app.get("/connections/{connection_id}/account")
    async def account(connection_id: str, refresh: bool = False) -> dict[str, object]:
        try:
            return await manager.account(connection_id, refresh)
        except Exception as error:
            raise fail(error) from None

    @app.post("/projects/{project_id}/open")
    async def open_project(project_id: Identifier, connection_id: Identifier) -> dict[str, object]:
        try:
            return await manager.open_project(connection_id, project_id)
        except Exception as error:
            raise fail(error) from None

    @app.get("/projects")
    async def list_projects(connection_id: Identifier) -> dict[str, object]:
        try:
            return await manager.projects(connection_id)
        except Exception as error:
            raise fail(error) from None

    @app.get("/projects/{project_id}/media")
    async def list_project_media(
        project_id: Identifier, connection_id: Identifier,
        limit: Annotated[int, Query(ge=1, le=100)] = 100
    ) -> dict[str, object]:
        try:
            return await manager.list_project_media(connection_id, project_id, limit)
        except Exception as error:
            raise fail(error) from None

    @app.get("/projects/{project_id}/media/{media_id}")
    async def find_project_media(
        project_id: Identifier, media_id: MediaIdentifier, connection_id: Identifier
    ) -> dict[str, object]:
        try:
            return await manager.find_project_media(connection_id, project_id, media_id)
        except Exception as error:
            raise fail(error) from None

    class PreviewBody(StrictModel):
        connection_id: Identifier
        confirm: bool = False
        preview_id: Identifier | None = None
        diagnostics: bool = False
        title: ProjectTitle = "OpenFlow project"

    @app.post("/projects/preview")
    @app.post("/projects")
    async def create_preview(body: PreviewBody) -> dict[str, object]:
        try:
            return await operations.create_project(
                body.connection_id, body.confirm, body.preview_id, body.diagnostics, body.title
            )
        except Exception as error:
            raise fail(error) from None

    class DeleteProjectBody(StrictModel):
        connection_id: Identifier
        confirm: bool = False
        preview_id: Identifier | None = None

    @app.post("/projects/{project_id}/delete")
    async def delete_project(project_id: Identifier, body: DeleteProjectBody) -> dict[str, object]:
        try:
            return await operations.delete_project(
                body.connection_id, project_id, body.confirm, body.preview_id
            )
        except Exception as error:
            raise fail(error) from None

    class ImageBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        input: ImageInput
        confirm: bool = False
        preview_id: Identifier | None = None

    class VideoBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        input: VideoInput
        confirm: bool = False
        preview_id: Identifier | None = None

    class ImageUpscaleBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        input: ImageUpscaleInput
        confirm: bool = False
        preview_id: Identifier | None = None

    class VideoUpscaleBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        input: VideoUpscaleInput
        confirm: bool = False
        preview_id: Identifier | None = None

    async def submit(
        operation: str, body: ImageBody | VideoBody | ImageUpscaleBody | VideoUpscaleBody
    ) -> dict[str, object]:
        try:
            return await operations.submit(
                operation,
                body.connection_id,
                body.project_id,
                body.input,
                body.confirm,
                body.preview_id,
            )
        except Exception as error:
            raise fail(error) from None

    @app.post("/images", tags=["generation"])
    async def image(body: ImageBody) -> dict[str, object]:
        return await submit("generate_image", body)

    @app.post("/videos", tags=["generation"])
    async def video(body: VideoBody) -> dict[str, object]:
        return await submit("generate_video", body)

    @app.post("/images/upscale", tags=["generation"])
    async def upscale_image(body: ImageUpscaleBody) -> dict[str, object]:
        return await submit("upscale_image", body)

    @app.post("/videos/upscale", tags=["generation"])
    async def upscale_video(body: VideoUpscaleBody) -> dict[str, object]:
        return await submit("upscale_video", body)

    class UploadBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        file_path: str | None = None
        data_base64: str | None = None
        name: str = "upload.png"
        confirm: bool = False
        preview_id: Identifier | None = None

    @app.post("/uploads")
    async def upload(body: UploadBody) -> dict[str, object]:
        try:
            value: UploadInput = upload_input(body.file_path, body.data_base64, body.name)
            return await operations.submit(
                "upload",
                body.connection_id,
                body.project_id,
                value,
                body.confirm,
                body.preview_id,
            )
        except Exception as error:
            raise fail(error) from None

    @app.get("/jobs/{job_id}")
    async def job(
        job_id: str, connection_id: str, project_id: str, refresh: bool = False
    ) -> dict[str, object]:
        try:
            return await operations.get(job_id, connection_id, project_id, refresh)
        except Exception as error:
            raise fail(error) from None

    @app.get("/jobs/{job_id}/recover")
    async def recover_job(
        job_id: Identifier, connection_id: Identifier, project_id: Identifier
    ) -> dict[str, object]:
        try:
            return await operations.recover_job(job_id, connection_id, project_id)
        except Exception as error:
            raise fail(error) from None

    class DownloadBody(StrictModel):
        connection_id: Identifier
        project_id: Identifier
        destination: str
        asset_index: TabId = 0

    @app.post("/jobs/{job_id}/download")
    async def download(job_id: str, body: DownloadBody) -> dict[str, object]:
        try:
            return await operations.download(
                job_id,
                body.connection_id,
                body.project_id,
                body.destination,
                body.asset_index,
            )
        except Exception as error:
            raise fail(error) from None

    return app


def main() -> None:
    import os

    import uvicorn

    parser = argparse.ArgumentParser(prog="openflow-api")
    parser.add_argument("--backend", choices=["browser", "extension"], default="browser")
    parser.add_argument("--port", type=int, default=9233)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    parser.add_argument("--pairing-dir", type=Path, default=base / "openflow")
    parser.add_argument("--init-pairing", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8100)
    args: Any = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 9222:
        parser.error("port must be 1024..65535, excluding 9222")
    if not 1024 <= args.api_port <= 65535:
        parser.error("api-port must be 1024..65535")
    try:
        secret = ""
        if args.init_pairing or (
            args.backend == "extension" and (args.pairing_dir / "secret").exists()
        ):
            secret = load_secret(args.pairing_dir)
        if not args.init_pairing:
            args.pairing_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            app = create_app(
                Manager(secret, pairings=Pairings(args.pairing_dir)),
                args.port,
                Jobs(args.pairing_dir / "jobs.sqlite3"),
                backend=args.backend,
            )
            uvicorn.run(app, host=args.host, port=args.api_port, log_level="warning")
    except Exception:
        parser.exit(1, "openflow-api: startup failed; check pairing file and ports.\n")


if __name__ == "__main__":
    main()
