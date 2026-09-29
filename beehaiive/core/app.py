from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CoreConfig, CoreConfigurationError
from .database import SnapshotDatabase
from .project import ProjectDataError, ProjectProvider
from .rest import GithubRestError
from .snapshot import ProjectSnapshotService
from .workflow_generator import WorkflowGenerationService
from .workflows import WorkflowStore, assign_layered_layout, validate_workflow

WEB_DIR = Path(__file__).with_name("web")


def create_app(
    config: CoreConfig | None = None,
    *,
    database: SnapshotDatabase | None = None,
    provider: ProjectProvider | None = None,
    service: ProjectSnapshotService | None = None,
    workflow_store: WorkflowStore | None = None,
    workflow_generator: WorkflowGenerationService | None = None,
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
    resolved_workflow_store = workflow_store or WorkflowStore(resolved_database)
    resolved_workflow_generator = workflow_generator or WorkflowGenerationService(
        resolved_config, resolved_service
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
    app.state.workflow_store = resolved_workflow_store
    app.state.workflow_generator = resolved_workflow_generator

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/api/project")
    def project() -> dict[str, object]:
        return _read_snapshot(resolved_service)

    @app.post("/api/project/refresh")
    def refresh() -> dict[str, object]:
        return _read_snapshot(resolved_service, refresh=True)

    @app.post("/api/workflows/generate")
    def generate_workflow(body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        prompt = str(body.get("prompt", ""))
        parameters = body.get("parameters", [])
        if not name or not prompt or not isinstance(parameters, list):
            raise HTTPException(
                status_code=422,
                detail="name, prompt, and a parameter list are required",
            )
        parameter_values = cast(list[Any], parameters)
        if not all(isinstance(parameter, dict) for parameter in parameter_values):
            raise HTTPException(status_code=422, detail="parameters must be objects")
        return resolved_workflow_generator.start(
            name,
            prompt,
            [cast(dict[str, Any], parameter) for parameter in parameter_values],
        )

    @app.get("/api/workflows/generate/{job_id}")
    def workflow_generation(job_id: str) -> dict[str, Any]:
        job = resolved_workflow_generator.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="generation job not found")
        return job

    @app.get("/api/workflows")
    def workflows() -> dict[str, Any]:
        return {"workflows": resolved_workflow_store.list_workflows()}

    @app.get("/api/workflows/{workflow_id}")
    def workflow(workflow_id: int) -> dict[str, Any]:
        value = resolved_workflow_store.get_workflow(workflow_id)
        if value is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return value

    @app.post("/api/workflows/validate")
    def validate_workflow_route(body: dict[str, Any]) -> dict[str, Any]:
        definition = _definition_from_body(body)
        validation = validate_workflow(
            definition,
            skills_dirs=resolved_config.skills_dirs,
            status_options=_status_options(resolved_service),
        )
        return validation.as_dict()

    @app.post("/api/workflows")
    def create_workflow(body: dict[str, Any]) -> dict[str, Any]:
        definition = _definition_from_body(body)
        validation = validate_workflow(
            definition,
            skills_dirs=resolved_config.skills_dirs,
            status_options=_status_options(resolved_service),
        )
        if not validation.valid:
            raise HTTPException(
                status_code=422,
                detail=validation.as_dict(),
            )
        prepared = assign_layered_layout(validation.definition)
        name = str(body.get("name", prepared.get("name", ""))).strip()
        source_prompt = str(
            body.get("source_prompt", prepared.get("source_prompt", ""))
        )
        if not name:
            raise HTTPException(status_code=422, detail="workflow name is required")
        return resolved_workflow_store.create_workflow(name, source_prompt, prepared)

    @app.post("/api/workflows/{workflow_id}/revisions")
    def add_workflow_revision(workflow_id: int, body: dict[str, Any]) -> dict[str, Any]:
        definition = _definition_from_body(body)
        validation = validate_workflow(
            definition,
            skills_dirs=resolved_config.skills_dirs,
            status_options=_status_options(resolved_service),
        )
        if not validation.valid:
            raise HTTPException(status_code=422, detail=validation.as_dict())
        prepared = assign_layered_layout(validation.definition)
        result = resolved_workflow_store.add_revision(
            workflow_id,
            str(body.get("source_prompt", prepared.get("source_prompt", ""))),
            prepared,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return result

    @app.delete("/api/workflows/{workflow_id}")
    def delete_workflow(workflow_id: int) -> dict[str, bool]:
        outcome = resolved_workflow_store.delete_workflow(workflow_id)
        if outcome == "missing":
            raise HTTPException(status_code=404, detail="workflow not found")
        if outcome == "assigned":
            raise HTTPException(
                status_code=409,
                detail="workflow cannot be deleted while an agent is assigned",
            )
        return {"deleted": True}

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


def _status_options(service: ProjectSnapshotService) -> tuple[str, ...]:
    try:
        snapshot = service.get_snapshot()
    except (CoreConfigurationError, ProjectDataError, GithubRestError, OSError):
        return ()
    return tuple(
        column.status for column in snapshot.columns if column.status != "No status"
    )


def _definition_from_body(body: dict[str, Any]) -> Any:
    value = body.get("definition", body)
    if not isinstance(value, dict):
        return value
    definition = dict(cast(dict[str, Any], value))
    for field in ("name", "source_prompt"):
        if field in body:
            definition[field] = body[field]
    return definition
