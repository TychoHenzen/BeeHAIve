import os
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore, Stage
from beehaiive.agent import (
    DEFAULT_DEMO_TASK,
    DEMO_TASK_NAME,
    AgentWorkerManager,
    CancellableModelExecutor,
    CodexExecModelExecutor,
)
from beehaiive.conflict_repair import ConflictRepairAgent, ConflictRepairService
from beehaiive.dashboard import build_dashboard_state
from beehaiive.meta_review import MetaReviewError, MetaReviewService
from beehaiive.models import RunState, RunStatus
from beehaiive.provider import ProviderError
from beehaiive.review import (
    REQUIRED_CONCERNS,
    PullRequestReviewProvider,
    ReaderStatus,
    ReviewAction,
    ReviewAdapterError,
    ReviewAuthorizer,
    ReviewConcern,
    ReviewError,
    ReviewReader,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_github import GitHubReviewProvider, github_review_readers
from beehaiive.routing import (
    ModelExecutor,
    ModelRouter,
    RoutingConfig,
    RoutingError,
    RoutingStore,
)
from beehaiive.scheduler import AgentScheduler, SchedulerConfig
from beehaiive.storage import (
    DEFAULT_EVENT_LIMIT,
    MAX_EVENT_LIMIT,
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    StoreError,
)
from beehaiive.workflow import (
    CommandCheck,
    Constitution,
    DeterministicCheck,
    LeaseStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowService,
    WorkflowStore,
)


class AdvanceRequest(BaseModel):
    target: Stage


class HandoffRequest(BaseModel):
    branch: str
    base_branch: str | None = None
    body: str = ""


class FailureRequest(BaseModel):
    error: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    recursive_spawn_depth: int = Field(default=0, ge=0)


class TaskQuestionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=1_000)


class RoutingAttemptRequest(BaseModel):
    outcome: Literal["failure", "retry", "success"]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    failure_context: str = Field(default="", max_length=2_000)
    recursive_spawn_depth: int = Field(default=0, ge=0)


class WorkflowWorkspaceRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=400)
    branch: str = Field(min_length=1, max_length=400)
    worktree: str = Field(min_length=1, max_length=1_000)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=400)


class WorkflowHandoffRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=100)
    source_role: WorkflowRole
    target_role: WorkflowRole
    commit_sha: str = Field(default="", max_length=200)
    source_state: str = Field(default="", max_length=1_000)
    approval_required: bool = False


class WorkflowApprovalRequest(BaseModel):
    note: str = Field(default="", max_length=1_000)


class WorkflowClarificationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1_000)


class WorkflowClarificationAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=1_000)


class WorkflowModelCallRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=100)


class WorkflowStopRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)


class ConflictRepairRequest(BaseModel):
    repository: str = Field(min_length=1, max_length=300)
    pull_request_number: int = Field(gt=0)


class ReviewStartRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)
    head_sha: str = Field(min_length=1, max_length=200)


class ReviewReadyRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)


class ReviewReaderRequest(BaseModel):
    concern: ReviewConcern
    status: ReaderStatus
    findings: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    reader: str = Field(default="automated", min_length=1, max_length=100)


class ReviewFindingRequest(BaseModel):
    concern: ReviewConcern
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


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


class DashboardCommitPushRequest(DashboardActionBase):
    action: Literal["commit_push"]
    repository: str
    pbi_number: int
    run_id: str


class MetaReviewRequest(BaseModel):
    since: str | None = Field(default=None, max_length=40)
    record_limit: int = Field(
        default=MAX_META_REVIEW_RECORDS, ge=1, le=MAX_META_REVIEW_RECORDS
    )
    input_token_limit: int = Field(
        default=MAX_META_REVIEW_INPUT_TOKENS,
        ge=1,
        le=MAX_META_REVIEW_INPUT_TOKENS,
    )


class MetaReviewDecisionRequest(BaseModel):
    decision: Literal["accept", "reject"]


DashboardActionRequest = Annotated[
    DashboardStartRequest
    | DashboardStopRequest
    | DashboardApproveRequest
    | DashboardClarifyRequest
    | DashboardCommitPushRequest,
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
    review_actor: str | None = None,
    require_review_adapters: bool = False,
    routing_store: RoutingStore | None = None,
    model_router: ModelRouter | None = None,
    model_executor: ModelExecutor | None = None,
    agent_worker: AgentWorkerManager | None = None,
    meta_review_service: MetaReviewService | None = None,
    workflow_service: WorkflowService | None = None,
    workflow_actor: WorkflowRole | str | None = None,
    conflict_repair_service: ConflictRepairService | None = None,
) -> FastAPI:
    owns_orchestrator = orchestrator is None
    if orchestrator is not None and orchestrator.model_router is not None:
        if model_router is not None and model_router is not orchestrator.model_router:
            raise ValueError("The orchestrator and API must share one model router")
        if (
            routing_store is not None
            and routing_store is not orchestrator.model_router.store
        ):
            raise ValueError("The orchestrator and API must share one routing store")
        routing_service = orchestrator.model_router
    else:
        if (
            model_router is not None
            and routing_store is not None
            and model_router.store is not routing_store
        ):
            raise ValueError("The model router and API must share one routing store")
        if model_router is None:
            routing_database = os.environ.get(
                "BEEHAIIVE_ROUTING_DB", ".beehaiive/routing.db"
            )
            model_router = ModelRouter(
                routing_store or RoutingStore(routing_database),
                _routing_config_from_environment(),
            )
        routing_service = model_router

    if orchestrator is None:
        if store is None:
            database = os.environ.get("BEEHAIIVE_STATE_DB", ".beehaiive/state.db")
            store = OrchestratorStore(
                database if database == ":memory:" else Path(database)
            )
        if model_executor is None:
            model_executor = CodexExecModelExecutor.from_environment()
        orchestrator = Orchestrator(
            store, EnvironmentGitHubProvider(), routing_service, model_executor
        )
    else:
        orchestrator.model_router = routing_service
    if orchestrator.model_executor is None:
        orchestrator.model_executor = model_executor
    if (
        conflict_repair_service is None
        and workflow_service is not None
        and orchestrator.model_executor is not None
        and callable(getattr(orchestrator.provider, "get_pull_request", None))
        and callable(getattr(orchestrator.provider, "update_source_branch", None))
        and callable(getattr(orchestrator.model_executor, "execute_repair", None))
    ):
        conflict_repair_service = ConflictRepairService(
            workflow_service,
            orchestrator.provider,
            cast(ConflictRepairAgent, orchestrator.model_executor),
        )
    if (
        meta_review_service is not None
        and meta_review_service.store is not orchestrator.store
    ):
        raise ValueError("The meta-review service and API must share one state store")
    if (
        meta_review_service is not None
        and meta_review_service.routing_store is not routing_service.store
    ):
        raise ValueError("The meta-review service and API must share one routing store")
    if meta_review_service is None:
        meta_review_service = MetaReviewService(
            orchestrator.store, routing_service.store
        )
    effective_executor = orchestrator.model_executor
    if agent_worker is None and effective_executor is not None:
        required_worker_methods = (
            "cancel",
            "prepare_run",
            "release_run",
        )
        if not all(
            callable(getattr(effective_executor, name, None))
            for name in required_worker_methods
        ):
            if owns_orchestrator:
                raise ValueError(
                    "The model executor must support cancellation and "
                    "repository-bound worker preparation"
                )
        else:
            agent_worker = AgentWorkerManager(
                orchestrator,
                cast(CancellableModelExecutor, effective_executor),
                workflow_service,
            )
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
    review_operations_enabled = (
        os.environ.get("BEEHAIIVE_REVIEW_MODE", "").strip().lower() != "demo"
    )
    if (
        review_service is None
        and require_review_adapters
        and review_operations_enabled
        and review_provider is None
        and review_readers is None
    ):
        review_provider = GitHubReviewProvider()
        review_readers = github_review_readers()
    if review_service is None:
        review_database = os.environ.get("BEEHAIIVE_REVIEW_DB", ".beehaiive/reviews.db")
        review_service = ReviewService(
            review_store or ReviewStore(review_database),
            provider=review_provider,
            readers=review_readers,
            authorizer=review_authorizer,
        )

    configured_projects = _configured_project_ids(allowed_project_ids)
    scheduler_config = SchedulerConfig.from_environment()
    scheduler = None
    if scheduler_config.enabled:
        if agent_worker is None:
            raise ValueError(
                "The scheduler requires a configured dashboard agent worker"
            )
        scheduler = AgentScheduler(
            orchestrator, agent_worker, configured_projects, scheduler_config
        )

    app = FastAPI(title="BeeHAIve")

    if agent_worker is not None:

        @app.on_event("shutdown")  # pyright: ignore[reportDeprecated]
        async def shutdown_background_workers() -> None:  # pyright: ignore[reportUnusedFunction]
            if scheduler is not None:
                scheduler.shutdown()
            agent_worker.shutdown()

    if require_review_adapters and review_operations_enabled:

        @app.on_event("startup")  # pyright: ignore[reportDeprecated]
        async def require_configured_review_adapters() -> None:  # pyright: ignore[reportUnusedFunction]
            missing = [
                concern.value
                for concern in REQUIRED_CONCERNS
                if concern not in review_service.readers
            ]
            if review_service.provider is None or missing:
                configured = "pull-request provider"
                if missing:
                    configured = f"{configured} and readers: {', '.join(missing)}"
                raise RuntimeError(
                    f"Production review adapters are not configured: {configured}"
                )
            validate_configuration = getattr(
                review_service.provider, "validate_configuration", None
            )
            if callable(validate_configuration):
                validate_configuration()

    if agent_worker is not None:

        @app.on_event("startup")  # pyright: ignore[reportDeprecated]
        async def recover_agent_workers() -> None:  # pyright: ignore[reportUnusedFunction]
            if scheduler is not None:
                agent_worker.recover(scheduler.project_ids)
            else:
                agent_worker.recover()
            if scheduler is not None:
                scheduler.start()

    configured_api_key = (
        api_key if api_key is not None else os.environ.get("BEEHAIIVE_API_KEY")
    )
    configured_review_actor = (
        review_actor
        if review_actor is not None
        else os.environ.get("BEEHAIIVE_REVIEW_ACTOR")
    )
    configured_workflow_actor = (
        workflow_actor
        if workflow_actor is not None
        else os.environ.get("BEEHAIIVE_WORKFLOW_ACTOR")
    )

    def require_api_key(supplied_api_key: str | None) -> None:
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
    ) -> str:
        if not review_operations_enabled:
            raise HTTPException(
                status_code=503, detail="Review operations are disabled in demo mode"
            )
        require_api_key(supplied_api_key)
        if configured_review_actor is None or not configured_review_actor.strip():
            raise HTTPException(
                status_code=503, detail="Review actor is not configured"
            )
        return configured_review_actor

    @app.post("/reviews/ready")
    def run_ready_review(  # pyright: ignore[reportUnusedFunction]
        request: ReviewReadyRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(request.pull_request_id, actor, ReviewAction.START)
            return review_service.run_ready_review(request.pull_request_id).as_dict()

        return _handle_review_error(operation)

    def require_routing_run_access(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
    ) -> None:
        require_api_key(supplied_api_key)
        run_id = request.path_params.get("run_id")
        if not isinstance(run_id, str):
            raise HTTPException(status_code=403, detail="Run is not authorized")
        run = orchestrator.store.get_run(run_id)
        if (
            run is None
            or run.project_id not in configured_projects
            or not orchestrator.store.is_active_repository(
                run.project_id, run.repository
            )
        ):
            raise HTTPException(status_code=403, detail="Run is not authorized")
        if run.status is RunStatus.ACTIVE:
            try:
                orchestrator.store.validate_lease(
                    run_id, _required_header(lease_token, "X-Lease-Token")
                )
            except StoreError as exc:
                raise HTTPException(
                    status_code=403, detail="Run is not authorized"
                ) from exc

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

    def require_workflow_access(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        require_api_key(supplied_api_key)

    def require_workflow_service() -> WorkflowService:
        if workflow_service is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow coordination is not configured",
            )
        return workflow_service

    def require_workflow_operator(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        require_api_key(supplied_api_key)
        if configured_workflow_actor is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow operator identity is not configured",
            )
        try:
            actor = WorkflowRole(configured_workflow_actor)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail="Workflow operator identity is not configured",
            ) from exc
        if actor is not WorkflowRole.OPERATOR:
            raise HTTPException(
                status_code=403,
                detail="Workflow operator approval is required",
            )
        return actor.value

    def require_handoff_lease_token(
        handoff_id: str,
        supplied_lease_token: str | None,
    ) -> None:
        service = require_workflow_service()
        handoff = service.get_handoff(handoff_id)
        service.store.require_lease_token(handoff.lease_id, supplied_lease_token)

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
            review_service.authorize(request.pull_request_id, actor, ReviewAction.START)
            return review_service.start_cycle(
                request.pull_request_id, request.head_sha
            ).as_dict()

        return _handle_review_error(operation)

    @app.get("/reviews/pull-requests/{pull_request_id:path}")
    def review_state(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, ReviewAction.READ)
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
            review_service.authorize(pull_request_id, actor, ReviewAction.READER)
            return review_service.record_reader(
                cycle_id,
                request.concern,
                request.status,
                request.findings,
                request.reader,
                evidence_refs=request.evidence_refs,
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
            review_service.authorize(pull_request_id, actor, ReviewAction.WRITER)
            return review_service.add_finding(
                cycle_id,
                request.concern,
                request.summary,
                evidence_refs=request.evidence_refs,
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
            review_service.authorize(pull_request_id, actor, ReviewAction.WRITER)
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
            review_service.authorize(pull_request_id, actor, ReviewAction.APPROVE)
            return review_service.approve_for_merge(
                cycle_id, request.reason, actor
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/pull-requests/{pull_request_id}/handoff")
    def review_handoff(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        request: ReviewHandoffRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, ReviewAction.HANDOFF)
            return review_service.merge_handoff(
                pull_request_id, request.head_sha
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/workflow/workspaces")
    def acquire_workflow_workspace(  # pyright: ignore[reportUnusedFunction]
        request: WorkflowWorkspaceRequest,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        return _handle_workflow_error(
            lambda: (
                require_workflow_service()
                .acquire_workspace(
                    request.agent_id,
                    request.branch,
                    request.worktree,
                    request.base_ref,
                )
                .as_dict()
            )
        )

    @app.post("/workflow/workspaces/{lease_id}/release")
    def release_workflow_workspace(  # pyright: ignore[reportUnusedFunction]
        lease_id: str,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_workflow_service().store.require_lease_token(
                lease_id, supplied_lease_token, allow_stopped=True
            )
            return require_workflow_service().release_workspace(lease_id).as_dict()

        return _handle_workflow_error(operation)

    @app.post("/workflow/workspaces/{lease_id}/stop")
    def stop_workflow_workspace(  # pyright: ignore[reportUnusedFunction]
        lease_id: str,
        request: WorkflowStopRequest,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_workflow_service().store.require_lease_token(
                lease_id, supplied_lease_token
            )
            return require_workflow_service().stop(lease_id, request.reason).as_dict()

        return _handle_workflow_error(operation)

    @app.post("/workflow/workspaces/{lease_id}/renew")
    def renew_workflow_workspace(  # pyright: ignore[reportUnusedFunction]
        lease_id: str,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_workflow_service().store.require_lease_token(
                lease_id, supplied_lease_token
            )
            return (
                require_workflow_service()
                .store.renew_lease(lease_id, supplied_lease_token)
                .as_dict()
            )

        return _handle_workflow_error(operation)

    @app.post("/workflow/model-calls")
    def authorize_workflow_model_call(  # pyright: ignore[reportUnusedFunction]
        request: WorkflowModelCallRequest,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_workflow_service().store.require_lease_token(
                request.lease_id, supplied_lease_token
            )
            return (
                require_workflow_service().before_model_call(request.lease_id).as_dict()
            )

        return _handle_workflow_error(operation)

    @app.post("/workflow/conflict-repairs")
    def repair_conflict(  # pyright: ignore[reportUnusedFunction]
        request: ConflictRepairRequest,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        if conflict_repair_service is None:
            raise HTTPException(
                status_code=503, detail="Conflict repair is not configured"
            )
        service = conflict_repair_service
        return _handle_workflow_error(
            lambda: service.repair(
                request.repository, request.pull_request_number
            ).as_dict()
        )

    @app.get("/workflow/conflict-repairs/{repair_id}")
    def get_conflict_repair(  # pyright: ignore[reportUnusedFunction]
        repair_id: str,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        service = require_workflow_service()
        if conflict_repair_service is None:
            raise HTTPException(
                status_code=503, detail="Conflict repair is not configured"
            )
        record = service.store.get_repair(repair_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Repair not found")
        return record.as_dict()

    @app.post("/workflow/handoffs")
    def create_workflow_handoff(  # pyright: ignore[reportUnusedFunction]
        request: WorkflowHandoffRequest,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_workflow_service().store.require_lease_token(
                request.lease_id, supplied_lease_token
            )
            return (
                require_workflow_service()
                .handoff(
                    request.lease_id,
                    request.source_role,
                    request.target_role,
                    request.commit_sha,
                    request.source_state,
                    request.approval_required,
                )
                .as_dict()
            )

        return _handle_workflow_error(operation)

    @app.get("/workflow/handoffs/{handoff_id}")
    def get_workflow_handoff(  # pyright: ignore[reportUnusedFunction]
        handoff_id: str,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        return _handle_workflow_error(
            lambda: require_workflow_service().get_handoff(handoff_id).as_dict()
        )

    @app.post("/workflow/handoffs/{handoff_id}/approve")
    def approve_workflow_handoff(  # pyright: ignore[reportUnusedFunction]
        handoff_id: str,
        request: WorkflowApprovalRequest,
        actor: str = Depends(require_workflow_operator),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_handoff_lease_token(handoff_id, supplied_lease_token)
            return (
                require_workflow_service()
                .approve_handoff(handoff_id, actor, request.note)
                .as_dict()
            )

        return _handle_workflow_error(operation)

    @app.post("/workflow/handoffs/{handoff_id}/clarify")
    def request_workflow_clarification(  # pyright: ignore[reportUnusedFunction]
        handoff_id: str,
        request: WorkflowClarificationRequest,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_handoff_lease_token(handoff_id, supplied_lease_token)
            return (
                require_workflow_service()
                .request_clarification(handoff_id, request.question)
                .as_dict()
            )

        return _handle_workflow_error(operation)

    @app.post("/workflow/handoffs/{handoff_id}/clarify/answer")
    def answer_workflow_clarification(  # pyright: ignore[reportUnusedFunction]
        handoff_id: str,
        request: WorkflowClarificationAnswer,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        supplied_lease_token: str | None = Header(
            default=None, alias="X-Workflow-Lease-Token"
        ),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            require_api_key(supplied_api_key)
            require_handoff_lease_token(handoff_id, supplied_lease_token)
            return (
                require_workflow_service()
                .answer_clarification(handoff_id, request.answer)
                .as_dict()
            )

        return _handle_workflow_error(operation)

    def routing_snapshot(run_id: str, *, required: bool) -> dict[str, object] | None:
        router = orchestrator.model_router
        assert router is not None
        if not required and router.store.get_problem(run_id) is None:
            return None
        try:
            return router.snapshot(run_id).as_dict()
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    @app.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": "Hello World"}

    @app.get("/hello/{name}")
    async def say_hello(name: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": f"Hello {name}"}

    @app.get("/runs/{run_id}/routing")
    def routing_problem(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        _auth: None = Depends(require_routing_run_access),
    ) -> dict[str, object]:
        try:
            return routing_service.snapshot(run_id).as_dict()
        except RoutingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/routing/attempts")
    def record_routing_attempt(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: RoutingAttemptRequest,
        _auth: None = Depends(require_routing_run_access),
    ) -> dict[str, object]:
        try:
            return routing_service.record(
                run_id,
                request.outcome,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                failure_context=request.failure_context,
                recursive_spawn_depth=request.recursive_spawn_depth,
            ).as_dict()
        except RoutingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        return _handle_store_error(
            lambda: orchestrator.synchronize(project_id, force_refresh=True)
        )

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
        archived: bool = Query(default=False),
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_store_error(
            lambda: _dashboard_state(
                orchestrator,
                project_id,
                event_limit,
                archived,
                workflow_service,
                scheduler,
                scheduler_config,
                agent_worker,
            )
        )

    @app.get("/projects/{project_id}/actions")
    def dashboard_actions(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_store_error(
            lambda: {"actions": orchestrator.store.actions_for_project(project_id)}
        )

    @app.post("/projects/{project_id}/meta-review")
    def run_meta_review(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        request: MetaReviewRequest,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _handle_meta_review_error(
            lambda: meta_review_service.run(
                project_id,
                since=request.since,
                record_limit=request.record_limit,
                input_token_limit=request.input_token_limit,
            )
        )

    @app.get("/projects/{project_id}/meta-review/suggestions")
    def meta_review_suggestions(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        status: Literal["pending", "accepted", "rejected"] | None = None,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_meta_review_error(
            lambda: {"suggestions": meta_review_service.suggestions(project_id, status)}
        )

    @app.post("/projects/{project_id}/meta-review/suggestions/{suggestion_id}")
    def decide_meta_review_suggestion(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        suggestion_id: str,
        request: MetaReviewDecisionRequest,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _handle_meta_review_error(
            lambda: {
                "suggestion": meta_review_service.decide(
                    project_id, suggestion_id, request.decision
                )
            }
        )

    @app.post("/projects/{project_id}/actions")
    def dashboard_action(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        request: DashboardActionRequest,
        archived: bool = Query(default=False),
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
        if isinstance(
            request,
            (
                DashboardApproveRequest,
                DashboardClarifyRequest,
                DashboardCommitPushRequest,
            ),
        ):
            pbi = _dashboard_pbi(
                orchestrator, project_id, request.repository, request.pbi_number
            )
            if pbi is None:
                raise HTTPException(status_code=403, detail="PBI is not authorized")
            if isinstance(request, DashboardCommitPushRequest):
                _require_dashboard_delivery_run(
                    orchestrator,
                    project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                )
            else:
                _require_active_dashboard_run(
                    orchestrator,
                    project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                )
        pbi_number = (
            request.pbi_number
            if isinstance(
                request,
                (
                    DashboardApproveRequest,
                    DashboardClarifyRequest,
                    DashboardCommitPushRequest,
                ),
            )
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
                    DashboardCommitPushRequest,
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
            result = _execute_dashboard_action(
                orchestrator, project_id, request, agent_worker
            )
        except (ProviderError, StoreError, WorkflowError) as exc:
            failed = orchestrator.store.finish_action(
                str(action["id"]), "failed", error=str(exc)
            )
            return {
                "action": failed,
                "result": None,
                "state": _dashboard_state_or_none(
                    orchestrator,
                    project_id,
                    DEFAULT_EVENT_LIMIT,
                    archived,
                    workflow_service,
                    scheduler,
                    scheduler_config,
                    agent_worker,
                ),
            }
        completed = orchestrator.store.finish_action(
            str(action["id"]), "succeeded", result=result
        )
        return {
            "action": completed,
            "result": result,
            "state": _dashboard_state(
                orchestrator,
                project_id,
                DEFAULT_EVENT_LIMIT,
                archived,
                workflow_service,
                scheduler,
                scheduler_config,
                agent_worker,
            ),
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
        run = _handle_store_error(
            lambda: orchestrator.advance(
                run_id,
                request.target,
                _required_header(lease_token, "X-Lease-Token"),
            )
        )
        routing = (
            _handle_store_error(lambda: routing_snapshot(run_id, required=True))
            if request.target is Stage.IMPLEMENT
            else None
        )
        return _run_dict(run, routing)

    @app.post("/runs/{run_id}/attempt")
    def run_attempt(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        routing = _handle_store_error(
            lambda: orchestrator.run_implementation_attempt(
                run_id, _required_header(lease_token, "X-Lease-Token")
            )
        )
        run = orchestrator.store.get_run(run_id)
        if run is None:  # pragma: no cover - the service already validated the run
            raise HTTPException(status_code=404, detail="Run not found")
        return {"run": _run_dict(run), "routing": routing.as_dict()}

    @app.post("/runs/{run_id}/question/answer")
    def answer_task_question(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: TaskQuestionAnswer,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(
                lambda: orchestrator.answer_task_question(run_id, request.answer)
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
        failed = _handle_store_error(
            lambda: orchestrator.fail(
                run_id,
                request.error,
                _required_header(lease_token, "X-Lease-Token"),
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                recursive_spawn_depth=request.recursive_spawn_depth,
            )
        )
        routing = _handle_store_error(lambda: routing_snapshot(run_id, required=False))
        return _run_dict(failed, routing)

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


def _routing_config_from_environment() -> RoutingConfig:
    config = RoutingConfig()
    override = os.environ.get("BEEHAIIVE_CODEX_MODEL", "").strip()
    if not override:
        return config
    return replace(
        config,
        writer=replace(config.writer, model=override),
        triage=tuple(replace(spec, model=override) for spec in config.triage),
    )


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


def _handle_meta_review_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except (MetaReviewError, StoreError):
        raise HTTPException(
            status_code=409, detail="Meta-review request could not be completed"
        ) from None


def _handle_review_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except ReviewAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _handle_workflow_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _run_dict(
    run: RunState, routing: dict[str, object] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
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
        "result": run.last_result,
        "owner_id": run.owner_id,
        "lease_token": run.lease_token,
        "lease_expires_at": run.lease_expires_at,
        "task_contract": run.task_contract,
        "task_result": run.task_result,
        "task_answer": run.task_answer,
    }
    if routing is not None:
        result["routing"] = routing
    return result


def _dashboard_state(
    orchestrator: Orchestrator,
    project_id: str,
    event_limit: int,
    archived: bool = False,
    workflow_service: WorkflowService | None = None,
    scheduler: AgentScheduler | None = None,
    scheduler_config: SchedulerConfig | None = None,
    agent_worker: AgentWorkerManager | None = None,
) -> dict[str, object]:
    orchestrator.synchronize(project_id)
    state = orchestrator.store.project_state(project_id, event_limit)
    actions = orchestrator.store.actions_for_project(project_id)
    dashboard = build_dashboard_state(state, actions, archived)
    if scheduler is not None:
        scheduler_status = scheduler.status_for(project_id)
        if scheduler_status is not None:
            dashboard["scheduler"] = scheduler_status
    elif scheduler_config is not None and not scheduler_config.enabled:
        dashboard["scheduler"] = {
            "enabled": False,
            "running": False,
            "poll_interval_seconds": scheduler_config.poll_interval_seconds,
            "max_concurrency": scheduler_config.max_concurrency,
            "active_workers": getattr(agent_worker, "active_worker_count", 0),
            "last_poll_at": None,
            "last_error": None,
            "last_started_run_ids": [],
        }
    if workflow_service is None:
        return dashboard
    repositories = cast(list[dict[str, object]], dashboard["repositories"])
    for repository in repositories:
        for pbi in cast(list[dict[str, object]], repository["pbis"]):
            run_id = pbi.get("run_id")
            if isinstance(run_id, str):
                delivery = _dashboard_delivery(workflow_service, run_id)
                if delivery is not None:
                    pbi["delivery"] = delivery
    return dashboard


def _dashboard_state_or_none(
    orchestrator: Orchestrator,
    project_id: str,
    event_limit: int,
    archived: bool = False,
    workflow_service: WorkflowService | None = None,
    scheduler: AgentScheduler | None = None,
    scheduler_config: SchedulerConfig | None = None,
    agent_worker: AgentWorkerManager | None = None,
) -> dict[str, object] | None:
    try:
        return _dashboard_state(
            orchestrator,
            project_id,
            event_limit,
            archived,
            workflow_service,
            scheduler,
            scheduler_config,
            agent_worker,
        )
    except (ProviderError, StoreError):
        return None


def _dashboard_delivery(
    workflow_service: WorkflowService, run_id: str
) -> dict[str, object] | None:
    lease = workflow_service.workspace_for_run(run_id)
    if lease is None:
        return None
    gate = workflow_service.store.latest_gate(lease.lease_id, "git_delivery")
    if gate is None:
        return None
    checks = {check.name: check.evidence for check in gate.checks}
    status = checks.get("delivery_status")
    if status is None:
        return None
    return {
        "status": status,
        "commit_sha": checks.get("commit_sha") or None,
        "branch": lease.branch,
        "evidence": checks.get("evidence", ""),
        "retry_available": lease.status is LeaseStatus.RETAINED
        and status not in {"pushed", "no_changes"},
    }


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


def _require_dashboard_delivery_run(
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
        or run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}
    ):
        raise HTTPException(
            status_code=403, detail="Run is not authorized for Git delivery"
        )
    return run


def _execute_dashboard_action(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None = None,
) -> dict[str, object]:
    if isinstance(request, DashboardStartRequest):
        if request.repository is None:
            return orchestrator.synchronize(project_id, force_refresh=True)
        if agent_worker is None:
            raise StoreError("Agent worker is not configured")
        task = getattr(getattr(agent_worker, "executor", None), "task", None)
        if not isinstance(task, str) or not task.strip():
            task = DEFAULT_DEMO_TASK
        run = agent_worker.claim(
            project_id,
            request.repository,
            request.worker_id or "dashboard-operator",
            task=task,
        )
        if run is None:
            raise StoreError("No claimable PBI is available for this repository")
        try:
            agent_worker.start(run)
        except Exception as exc:
            try:
                orchestrator.stop(run.run_id, f"Agent worker failed to start: {exc}")
            except StoreError as stop_error:
                raise StoreError(
                    f"Agent worker failed to start and cleanup failed: {stop_error}"
                ) from exc
            raise StoreError(f"Agent worker failed to start: {exc}") from exc
        return {
            "run": _public_run_dict(run),
            "worker": {"status": "started", "task": DEMO_TASK_NAME},
        }
    if isinstance(request, DashboardStopRequest):
        if agent_worker is not None:
            agent_worker.cancel(request.run_id)
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
    if isinstance(request, DashboardCommitPushRequest):
        if agent_worker is None:
            raise StoreError("Agent worker is not configured")
        return {"delivery": agent_worker.commit_and_push(request.run_id).as_dict()}
    return {"approved": True}


def _public_run_dict(run: RunState) -> dict[str, object]:
    result = _run_dict(run)
    result.pop("lease_token", None)
    return result


def _production_workflow_service() -> WorkflowService:
    repository = Path(
        os.environ.get("BEEHAIIVE_WORKFLOW_REPOSITORY", str(Path(__file__).parent))
    )
    database = os.environ.get(
        "BEEHAIIVE_WORKFLOW_DB",
        str(repository / ".beehaiive" / "workflow.db"),
    )
    constitution_path = Path(__file__).parent / "constitution.json"
    return WorkflowService(
        WorkflowStore(database),
        repository,
        Constitution.load(constitution_path),
        (
            cast(
                DeterministicCheck, CommandCheck("tests", ("uv", "run", "pytest", "-q"))
            ),
        ),
    )


app = (
    None
    if os.environ.get("BEEHAIIVE_SKIP_PRODUCTION_APP") == "1"
    else create_app(
        workflow_service=_production_workflow_service(),
        workflow_actor=os.environ.get("BEEHAIIVE_WORKFLOW_ACTOR"),
        require_review_adapters=True,
    )
)
