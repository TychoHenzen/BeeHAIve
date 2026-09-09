import os
import secrets
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore, Stage
from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import RunState, RunStatus
from beehaiive.provider import ProviderError
from beehaiive.review import (
    PullRequestReviewProvider,
    ReviewAuthorizer,
    ReviewConcern,
    ReviewError,
    ReviewReader,
    ReviewService,
    ReviewStore,
)
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


class ReviewStartRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)
    head_sha: str = Field(min_length=1, max_length=200)


class ReviewReadyRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)


class ReviewReaderRequest(BaseModel):
    concern: Literal["security", "test_coverage", "clean_code", "performance"]
    status: Literal["pending", "pass", "fail"]
    findings: list[str] = Field(default_factory=list, max_length=20)
    reader: str = Field(default="automated", min_length=1, max_length=100)


class ReviewFindingRequest(BaseModel):
    concern: Literal["security", "test_coverage", "clean_code", "performance"]
    summary: str = Field(min_length=1, max_length=1_000)


class ReviewResolutionRequest(BaseModel):
    resolution: str = Field(min_length=1, max_length=1_000)


class ReviewApprovalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1_000)


class ReviewHandoffRequest(BaseModel):
    head_sha: str = Field(min_length=1, max_length=200)


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
    review_store: ReviewStore | None = None,
    review_service: ReviewService | None = None,
    review_provider: PullRequestReviewProvider | None = None,
    review_readers: Mapping[ReviewConcern, ReviewReader] | None = None,
    review_authorizer: ReviewAuthorizer | None = None,
) -> FastAPI:
    if orchestrator is None:
        if store is None:
            database = os.environ.get("BEEHAIIVE_STATE_DB", ".beehaiive/state.db")
            store = OrchestratorStore(
                database if database == ":memory:" else Path(database)
            )
        orchestrator = Orchestrator(store, EnvironmentGitHubProvider())
    if (
        review_service is not None
        and review_store is not None
        and review_service.store is not review_store
    ):
        raise ValueError("The review service and API must share one review store")
    if review_service is not None and any(
        value is not None
        for value in (review_provider, review_readers, review_authorizer)
    ):
        raise ValueError("Review adapters must be configured on the review service")
    if review_service is None:
        review_database = os.environ.get("BEEHAIIVE_REVIEW_DB", ".beehaiive/reviews.db")
        review_service = ReviewService(
            review_store or ReviewStore(review_database),
            provider=review_provider,
            readers=review_readers,
            authorizer=review_authorizer,
        )

    app = FastAPI(title="BeeHAIve")
    configured_api_key = (
        api_key if api_key is not None else os.environ.get("BEEHAIIVE_API_KEY")
    )
    configured_projects = _configured_project_ids(allowed_project_ids)

    def require_api_key(
        supplied_api_key: str | None,
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

    def require_review_access(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        actor: str | None = Header(default=None, alias="X-Review-Actor"),
    ) -> str:
        require_api_key(supplied_api_key)
        if actor is None or not actor.strip():
            raise HTTPException(status_code=401, detail="X-Review-Actor is required")
        return actor

    @app.post("/reviews/ready")
    def run_ready_review(  # pyright: ignore[reportUnusedFunction]
        request: ReviewReadyRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(request.pull_request_id, actor, "start")
            return review_service.run_ready_review(request.pull_request_id).as_dict()

        return _handle_review_error(operation)

    def require_mutation_access(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        require_api_key(supplied_api_key)

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

    @app.post("/reviews/cycles")
    def start_review_cycle(  # pyright: ignore[reportUnusedFunction]
        request: ReviewStartRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(request.pull_request_id, actor, "start")
            return review_service.start_cycle(
                request.pull_request_id, request.head_sha
            ).as_dict()

        return _handle_review_error(operation)

    @app.get("/reviews/pull-requests/{pull_request_id}")
    def review_state(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, "read")
            return review_service.snapshot(pull_request_id).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/readers")
    def record_review_reader(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewReaderRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, "reader")
            return review_service.record_reader(
                cycle_id,
                request.concern,
                request.status,
                request.findings,
                request.reader,
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/findings")
    def add_review_finding(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewFindingRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, "writer")
            return review_service.add_finding(
                cycle_id, request.concern, request.summary
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/findings/{finding_id}/resolve")
    def resolve_review_finding(  # pyright: ignore[reportUnusedFunction]
        finding_id: str,
        request: ReviewResolutionRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_finding(finding_id)
            review_service.authorize(pull_request_id, actor, "writer")
            return review_service.resolve_finding(
                finding_id, request.resolution
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/approve")
    def approve_review_cycle(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewApprovalRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, "approve")
            return review_service.approve_for_merge(cycle_id, request.reason).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/pull-requests/{pull_request_id}/handoff")
    def review_handoff(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        request: ReviewHandoffRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, "handoff")
            return review_service.merge_handoff(
                pull_request_id, request.head_sha
            ).as_dict()

        return _handle_review_error(operation)

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


def _handle_review_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
