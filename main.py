import os
import secrets
from collections.abc import Callable, Collection
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore, Stage
from beehaiive.models import RunState
from beehaiive.provider import ProviderError
from beehaiive.storage import (
    DEFAULT_EVENT_LIMIT,
    MAX_EVENT_LIMIT,
    StoreError,
)


class AdvanceRequest(BaseModel):
    target: Stage


class HandoffRequest(BaseModel):
    branch: str
    base_branch: str | None = None
    body: str = ""


class FailureRequest(BaseModel):
    error: str


def create_app(
    store: OrchestratorStore | None = None,
    orchestrator: Orchestrator | None = None,
    api_key: str | None = None,
    allowed_project_ids: Collection[str] | None = None,
) -> FastAPI:
    if orchestrator is None:
        if store is None:
            database = os.environ.get("BEEHAIIVE_STATE_DB", ".beehaiive/state.db")
            store = OrchestratorStore(
                database if database == ":memory:" else Path(database)
            )
        orchestrator = Orchestrator(store, EnvironmentGitHubProvider())

    app = FastAPI(title="BeeHAIve")
    configured_api_key = (
        api_key if api_key is not None else os.environ.get("BEEHAIIVE_API_KEY")
    )
    configured_projects = _configured_project_ids(allowed_project_ids)

    def require_mutation_access(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        if not configured_api_key:
            raise HTTPException(
                status_code=503,
                detail="Mutation authorization is not configured",
            )
        if supplied_api_key is None or not secrets.compare_digest(
            supplied_api_key, configured_api_key
        ):
            raise HTTPException(status_code=401, detail="Invalid API key")

        path_params = request.path_params
        project_id = path_params.get("project_id")
        repository = path_params.get("repository")
        run_id = path_params.get("run_id")
        if isinstance(run_id, str):
            run = orchestrator.store.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=403, detail="Run is not authorized")
            project_id = run.project_id
            repository = run.repository
        if not isinstance(project_id, str) or project_id not in configured_projects:
            raise HTTPException(status_code=403, detail="Project is not authorized")
        if isinstance(repository, str) and not orchestrator.store.is_active_repository(
            project_id, repository
        ):
            raise HTTPException(status_code=403, detail="Repository is not authorized")

    @app.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": "Hello World"}

    @app.get("/hello/{name}")
    async def say_hello(name: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": f"Hello {name}"}

    @app.post("/projects/{project_id}/sync")
    def synchronize(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(lambda: orchestrator.synchronize(project_id))

    @app.get("/projects/{project_id}")
    def project_state(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        event_limit: int = Query(
            default=DEFAULT_EVENT_LIMIT,
            ge=1,
            le=MAX_EVENT_LIMIT,
        ),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(
            lambda: orchestrator.store.project_state(project_id, event_limit)
        )

    @app.post("/projects/{project_id}/repositories/{repository:path}/claim")
    def claim(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        worker_id: str | None = Header(default=None, alias="X-Worker-ID"),
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object] | None:
        run = _handle_store_error(
            lambda: orchestrator.claim(
                project_id,
                repository,
                _required_header(worker_id, "X-Worker-ID"),
                lease_token,
            )
        )
        return None if run is None else _run_dict(run)

    @app.post("/runs/{run_id}/advance")
    def advance(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: AdvanceRequest,
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(
                lambda: orchestrator.advance(
                    run_id,
                    request.target,
                    _required_header(lease_token, "X-Lease-Token"),
                )
            )
        )

    @app.post("/runs/{run_id}/lease")
    def renew_lease(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(
                lambda: orchestrator.renew_lease(
                    run_id, _required_header(lease_token, "X-Lease-Token")
                )
            )
        )

    @app.post("/runs/{run_id}/handoff")
    def handoff(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: HandoffRequest,
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        run = _handle_store_error(
            lambda: orchestrator.handoff(
                run_id,
                request.branch,
                request.base_branch,
                request.body,
                _required_header(lease_token, "X-Lease-Token"),
            )
        )
        return _run_dict(run)

    @app.post("/runs/{run_id}/fail")
    def fail(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: FailureRequest,
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(
                lambda: orchestrator.fail(
                    run_id,
                    request.error,
                    _required_header(lease_token, "X-Lease-Token"),
                )
            )
        )

    return app


def _configured_project_ids(
    project_ids: Collection[str] | None,
) -> frozenset[str]:
    if project_ids is not None:
        return frozenset(project_ids)
    configured = os.environ.get("BEEHAIIVE_ALLOWED_PROJECTS")
    if configured:
        return frozenset(
            project_id.strip()
            for project_id in configured.split(",")
            if project_id.strip()
        )
    owner = os.environ.get("GITHUB_PROJECT_OWNER")
    number = os.environ.get("GITHUB_PROJECT_NUMBER")
    if owner and number:
        return frozenset({f"{owner}:{number}"})
    return frozenset()


def _required_header(value: str | None, name: str) -> str:
    if not value or not value.strip():
        raise HTTPException(status_code=401, detail=f"{name} is required")
    return value


def _handle_store_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except StoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _run_dict(run: RunState) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "repository": run.repository,
        "pbi_number": run.pbi_number,
        "title": run.title,
        "stage": run.stage.value,
        "status": run.status.value,
        "attempt": run.attempt,
        "branch": run.branch,
        "pull_request_url": run.pull_request_url,
        "last_error": run.last_error,
        "owner_id": run.owner_id,
        "lease_token": run.lease_token,
        "lease_expires_at": run.lease_expires_at,
    }


app = create_app()
