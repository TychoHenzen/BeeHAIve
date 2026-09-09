import os
import secrets
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore, Stage
from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import RunState, RunStatus
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


class DashboardActionBase(BaseModel):
    approved: bool = False


class DashboardStartRequest(DashboardActionBase):
    action: Literal["start"]
    repository: str | None = None
    worker_id: str | None = None


class DashboardStopRequest(DashboardActionBase):
    action: Literal["stop"]
    run_id: str
    repository: str | None = None
    reason: str = ""


class DashboardApproveRequest(DashboardActionBase):
    action: Literal["approve"]
    repository: str
    pbi_number: int
    run_id: str


class DashboardClarifyRequest(DashboardActionBase):
    action: Literal["clarify"]
    repository: str
    pbi_number: int
    run_id: str
    clarification: str = ""


DashboardActionRequest = Annotated[
    DashboardStartRequest
    | DashboardStopRequest
    | DashboardApproveRequest
    | DashboardClarifyRequest,
    Field(discriminator="action"),
]


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

    def require_project_access(request: Request) -> None:
        project_id = request.path_params.get("project_id")
        if not isinstance(project_id, str) or project_id not in configured_projects:
            raise HTTPException(status_code=403, detail="Project is not authorized")

    @app.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": "Hello World"}

    @app.get("/hello/{name}")
    async def say_hello(name: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": f"Hello {name}"}

    @app.get("/dashboard", response_class=FileResponse)
    def dashboard() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            Path(__file__).parent / "docs" / "dashboard.html",
            media_type="text/html",
        )

    @app.get("/dashboard.js", response_class=FileResponse)
    def dashboard_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            Path(__file__).parent / "docs" / "dashboard.js",
            media_type="application/javascript",
        )

    @app.get("/dashboard-client.mjs", response_class=FileResponse)
    def dashboard_client_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            Path(__file__).parent / "docs" / "dashboard-client.mjs",
            media_type="application/javascript",
        )

    @app.get("/dashboard-view.mjs", response_class=FileResponse)
    def dashboard_view_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            Path(__file__).parent / "docs" / "dashboard-view.mjs",
            media_type="application/javascript",
        )

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

    @app.get("/projects/{project_id}/dashboard")
    def dashboard_state(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        event_limit: int = Query(
            default=DEFAULT_EVENT_LIMIT,
            ge=1,
            le=MAX_EVENT_LIMIT,
        ),
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_store_error(
            lambda: _dashboard_state(orchestrator, project_id, event_limit)
        )

    @app.get("/projects/{project_id}/actions")
    def dashboard_actions(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_store_error(
            lambda: {"actions": orchestrator.store.actions_for_project(project_id)}
        )

    @app.post("/projects/{project_id}/actions")
    def dashboard_action(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        request: DashboardActionRequest,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for dashboard actions",
            )
        if (
            request.repository is not None
            and request.action != "stop"
            and not orchestrator.store.is_active_repository(
                project_id, request.repository
            )
        ):
            raise HTTPException(status_code=403, detail="Repository is not authorized")
        if isinstance(request, DashboardStopRequest):
            run = orchestrator.store.get_run(request.run_id)
            if run is None or run.project_id != project_id:
                raise HTTPException(status_code=403, detail="Run is not authorized")
            if request.repository is not None and run.repository != request.repository:
                raise HTTPException(
                    status_code=403, detail="Repository is not authorized"
                )
        if isinstance(request, (DashboardApproveRequest, DashboardClarifyRequest)):
            pbi = _dashboard_pbi(
                orchestrator, project_id, request.repository, request.pbi_number
            )
            if pbi is None:
                raise HTTPException(status_code=403, detail="PBI is not authorized")
            _require_active_dashboard_run(
                orchestrator,
                project_id,
                request.repository,
                request.pbi_number,
                request.run_id,
            )
        pbi_number = (
            request.pbi_number
            if isinstance(request, (DashboardApproveRequest, DashboardClarifyRequest))
            else None
        )
        run_id = (
            request.run_id
            if isinstance(
                request,
                (
                    DashboardStopRequest,
                    DashboardApproveRequest,
                    DashboardClarifyRequest,
                ),
            )
            else None
        )
        action = orchestrator.store.begin_action(
            project_id,
            request.action,
            request.model_dump(exclude_none=True),
            request.repository,
            pbi_number,
            run_id,
        )
        try:
            result = _execute_dashboard_action(orchestrator, project_id, request)
        except (ProviderError, StoreError) as exc:
            failed = orchestrator.store.finish_action(
                str(action["id"]), "failed", error=str(exc)
            )
            return {
                "action": failed,
                "result": None,
                "state": _dashboard_state_or_none(
                    orchestrator, project_id, DEFAULT_EVENT_LIMIT
                ),
            }
        completed = orchestrator.store.finish_action(
            str(action["id"]), "succeeded", result=result
        )
        return {
            "action": completed,
            "result": result,
            "state": _dashboard_state(orchestrator, project_id, DEFAULT_EVENT_LIMIT),
        }

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


def _dashboard_state(
    orchestrator: Orchestrator, project_id: str, event_limit: int
) -> dict[str, object]:
    orchestrator.synchronize(project_id)
    state = orchestrator.store.project_state(project_id, event_limit)
    actions = orchestrator.store.actions_for_project(project_id)
    return build_dashboard_state(state, actions)


def _dashboard_state_or_none(
    orchestrator: Orchestrator, project_id: str, event_limit: int
) -> dict[str, object] | None:
    try:
        return _dashboard_state(orchestrator, project_id, event_limit)
    except (ProviderError, StoreError):
        return None


def _dashboard_pbi(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    pbi_number: int,
) -> dict[str, object] | None:
    try:
        state = orchestrator.store.project_state(project_id)
    except StoreError:
        return None
    repositories = cast(list[dict[str, object]], state.get("repositories", []))
    for raw_repository in repositories:
        if raw_repository.get("name") != repository:
            continue
        pbis = cast(list[dict[str, object]], raw_repository.get("pbis", []))
        for raw_pbi in pbis:
            if raw_pbi.get("number") == pbi_number:
                return raw_pbi
    return None


def _require_active_dashboard_run(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    pbi_number: int,
    run_id: str,
) -> RunState:
    run = orchestrator.store.get_run(run_id)
    if (
        run is None
        or run.project_id != project_id
        or run.repository != repository
        or run.pbi_number != pbi_number
        or run.status is not RunStatus.ACTIVE
    ):
        raise HTTPException(status_code=403, detail="Run is not authorized")
    return run


def _execute_dashboard_action(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
) -> dict[str, object]:
    if isinstance(request, DashboardStartRequest):
        if request.repository is None:
            return orchestrator.synchronize(project_id)
        run = orchestrator.claim(
            project_id,
            request.repository,
            request.worker_id or "dashboard-operator",
        )
        if run is None:
            raise StoreError("No claimable PBI is available for this repository")
        return {"run": _public_run_dict(run)}
    if isinstance(request, DashboardStopRequest):
        return {
            "run": _public_run_dict(
                orchestrator.stop(
                    request.run_id, request.reason or "Stopped by operator"
                )
            )
        }
    if isinstance(request, DashboardClarifyRequest):
        if not request.clarification.strip():
            raise StoreError("A clarification message is required")
        return {"message": request.clarification.strip()}
    return {"approved": True}


def _public_run_dict(run: RunState) -> dict[str, object]:
    result = _run_dict(run)
    result.pop("lease_token", None)
    return result


app = create_app()
