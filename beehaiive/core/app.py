from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CoreConfig, CoreConfigurationError
from .database import SnapshotDatabase
from .project import ProjectDataError, ProjectProvider
from .rest import GithubRestError
from .snapshot import ProjectSnapshotService

WEB_DIR = Path(__file__).with_name("web")


def create_app(
    config: CoreConfig | None = None,
    *,
    database: SnapshotDatabase | None = None,
    provider: ProjectProvider | None = None,
    service: ProjectSnapshotService | None = None,
) -> FastAPI:
    resolved_config = config or CoreConfig.from_environment()
    owned_database = database is None
    resolved_database = database or SnapshotDatabase(resolved_config.db_path)
    resolved_provider = provider
    if resolved_provider is None:
        from .rest import GithubRestClient

        resolved_provider = ProjectProvider(
            resolved_config,
            GithubRestClient(resolved_config.github_token, resolved_database),
        )
    resolved_service = service or ProjectSnapshotService(
        resolved_provider,
        resolved_database,
        minimum_refresh_seconds=resolved_config.refresh_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owned_database:
            resolved_database.close()

    app = FastAPI(title="BeeHAIve Core", lifespan=lifespan)
    app.state.project_service = resolved_service
    app.state.database = resolved_database
    app.state.owned_database = owned_database

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/api/project")
    def project() -> dict[str, object]:
        return _read_snapshot(resolved_service)

    @app.post("/api/project/refresh")
    def refresh() -> dict[str, object]:
        return _read_snapshot(resolved_service, refresh=True)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="core-web")

    return app


def _read_snapshot(
    service: ProjectSnapshotService, *, refresh: bool = False
) -> dict[str, object]:
    try:
        snapshot = service.request_refresh() if refresh else service.get_snapshot()
    except (CoreConfigurationError, ProjectDataError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GithubRestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return snapshot.as_dict()
