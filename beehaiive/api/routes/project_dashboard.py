import json
from collections.abc import Callable
from typing import Any, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from beehaiive import Orchestrator
from beehaiive.agent import MAX_AGENT_OUTPUT_LENGTH, redact_worker_text
from beehaiive.api.helpers.dashboard import (
    DASHBOARD_ACTION_OWNERS as DASHBOARD_ACTION_OWNERS,
)
from beehaiive.api.helpers.dashboard import (
    DASHBOARD_ACTION_READBACK as DASHBOARD_ACTION_READBACK,
)
from beehaiive.api.helpers.dashboard import _dashboard_pbi as _dashboard_pbi
from beehaiive.api.helpers.dashboard import _dashboard_state as _dashboard_state
from beehaiive.api.helpers.dashboard import (
    _dashboard_state_or_none as _dashboard_state_or_none,
)
from beehaiive.api.helpers.dashboard import (
    _execute_dashboard_action as _execute_dashboard_action,
)
from beehaiive.api.helpers.dashboard import (
    _require_active_dashboard_run as _require_active_dashboard_run,
)
from beehaiive.api.helpers.dashboard import (
    _require_dashboard_advance_run as _require_dashboard_advance_run,
)
from beehaiive.api.helpers.dashboard import (
    _require_dashboard_delivery_run as _require_dashboard_delivery_run,
)
from beehaiive.api.helpers.dashboard import (
    _require_dashboard_operator_question as _require_dashboard_operator_question,
)
from beehaiive.api.helpers.dashboard import (
    _require_dashboard_retry_run as _require_dashboard_retry_run,
)
from beehaiive.api.helpers.http import (
    _handle_meta_review_error as _handle_meta_review_error,
)
from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.helpers.http import _required_header as _required_header
from beehaiive.api.helpers.serialization import _run_dict as _run_dict
from beehaiive.api.models import DASHBOARD_ACTIONS as DASHBOARD_ACTIONS
from beehaiive.api.models import DashboardActionRequest as DashboardActionRequest
from beehaiive.api.models import DashboardAdvanceRequest as DashboardAdvanceRequest
from beehaiive.api.models import (
    DashboardAnswerQuestionRequest as DashboardAnswerQuestionRequest,
)
from beehaiive.api.models import DashboardApproveRequest as DashboardApproveRequest
from beehaiive.api.models import DashboardClarifyRequest as DashboardClarifyRequest
from beehaiive.api.models import (
    DashboardCommitPushRequest as DashboardCommitPushRequest,
)
from beehaiive.api.models import DashboardRetryRequest as DashboardRetryRequest
from beehaiive.api.models import DashboardStartRequest as DashboardStartRequest
from beehaiive.api.models import DashboardStopRequest as DashboardStopRequest
from beehaiive.api.models import MetaReviewDecisionRequest as MetaReviewDecisionRequest
from beehaiive.api.models import MetaReviewRequest as MetaReviewRequest
from beehaiive.api.models import SchedulerConfigRequest as SchedulerConfigRequest
from beehaiive.dashboard.values import safe_dashboard_value
from beehaiive.provider import ProviderError
from beehaiive.scheduler import SchedulerConfig
from beehaiive.storage import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT, StoreError
from beehaiive.workflow import WorkflowError

MAX_DASHBOARD_ACTION_BYTES = 64_000


async def _require_dashboard_action_size(request: Request) -> None:
    body = await request.body()
    if len(body) > MAX_DASHBOARD_ACTION_BYTES:
        raise HTTPException(
            status_code=413, detail="Dashboard action request is too large"
        )


def _handle_dashboard_read[T](
    function: Callable[[], T], secret_values: tuple[str, ...]
) -> T:
    try:
        return function()
    except StoreError as exc:
        raise HTTPException(
            status_code=409,
            detail=redact_worker_text(
                str(exc), secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            ),
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail=redact_worker_text(
                str(exc), secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=redact_worker_text(
                str(exc), secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            ),
        ) from exc


def _project_state_without_graph_trace(
    orchestrator: Orchestrator, project_id: str, event_limit: int
) -> dict[str, object]:
    state = orchestrator.store.project_state(project_id, event_limit)
    repositories = cast(list[dict[str, object]], state.get("repositories", []))
    for repository in repositories:
        for pbi in cast(list[dict[str, object]], repository.get("pbis", [])):
            pbi.pop("graph_trace", None)
    return state


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    agent_worker = context["agent_worker"]
    autonomous_service = context["autonomous_service"]
    dashboard_secret_values = context["dashboard_secret_values"]
    meta_review_service = context["meta_review_service"]
    orchestrator = context["orchestrator"]
    pbi_creation_service = context["pbi_creation_service"]
    require_mutation_access = context["require_mutation_access"]
    require_project_access = context["require_project_access"]
    scheduler = context["scheduler"]
    scheduler_config = context["scheduler_config"]
    workflow_service = context["workflow_service"]
    graph_safety_service = context["graph_safety_service"]
    require_workflow_operator = context["require_workflow_operator"]

    @app.get("/projects/{project_id}")
    def project_state(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        event_limit: int = Query(
            default=DEFAULT_EVENT_LIMIT,
            ge=1,
            le=MAX_EVENT_LIMIT,
        ),
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(
            lambda: _project_state_without_graph_trace(
                orchestrator, project_id, event_limit
            )
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
        workflow_id: str | None = Query(default=None, max_length=128),
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        return _handle_dashboard_read(
            lambda: _dashboard_state(
                orchestrator,
                project_id,
                event_limit,
                archived,
                workflow_service,
                scheduler,
                scheduler_config,
                agent_worker,
                dashboard_secret_values,
                graph_safety_service,
                workflow_id,
                autonomous_service,
            ),
            dashboard_secret_values,
        )

    @app.get("/projects/{project_id}/actions")
    def dashboard_actions(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        def read_actions() -> dict[str, object]:
            actions = safe_dashboard_value(
                orchestrator.store.actions_for_project(project_id),
                dashboard_secret_values,
            )
            return {
                "actions": actions if isinstance(actions, list) else [],
                "supported_actions": sorted(DASHBOARD_ACTIONS),
                "supported_action_owners": dict(DASHBOARD_ACTION_OWNERS),
                "supported_action_readback": dict(DASHBOARD_ACTION_READBACK),
            }

        return _handle_dashboard_read(read_actions, dashboard_secret_values)

    @app.post("/projects/{project_id}/scheduler")
    def configure_scheduler(
        project_id: str,
        request: SchedulerConfigRequest,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for scheduler configuration",
            )
        if scheduler is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Scheduler is unavailable until a workflow-backed agent worker "
                    "is configured"
                ),
            )
        action = orchestrator.store.begin_action(
            project_id,
            "scheduler_configure",
            request.model_dump(exclude_none=True),
        )
        try:
            scheduler.configure(
                SchedulerConfig(
                    enabled=request.enabled,
                    poll_interval_seconds=request.poll_interval_seconds,
                    max_concurrency=request.max_concurrency,
                )
            )
            status = cast(dict[str, object], scheduler.status_for(project_id) or {})
            result: dict[str, object] = {"scheduler": status}
            completed = orchestrator.store.finish_action(
                str(action["id"]), "succeeded", result
            )
            return {"scheduler": status or {}, "action": completed}
        except Exception as exc:
            error = redact_worker_text(
                str(exc), dashboard_secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            )
            orchestrator.store.finish_action(str(action["id"]), "failed", error=error)
            raise HTTPException(
                status_code=409, detail="Scheduler configuration could not be applied"
            ) from exc

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
            lambda: meta_review_service.decide(
                project_id,
                suggestion_id,
                request.decision,
                pbi_creator=pbi_creation_service.create,
            )
        )

    @app.post("/projects/{project_id}/actions")
    def dashboard_action(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        request: DashboardActionRequest,
        http_request: Request,
        archived: bool = Query(default=False),
        workflow_id: str | None = Query(default=None, max_length=128),
        _auth: None = Depends(require_mutation_access),
        _size: None = Depends(_require_dashboard_action_size),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for dashboard actions",
            )
        if request.action in {"graph_review", "graph_activate", "graph_rollback"}:
            require_workflow_operator(http_request.headers.get("X-API-Key"))
        body_workflow_id = getattr(request, "workflow_id", None)
        if (
            workflow_id is not None
            and body_workflow_id is not None
            and workflow_id != body_workflow_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Selected workflow ID does not match the action workflow ID",
            )
        if (
            isinstance(request, DashboardStartRequest) and request.worker_id is not None
        ) or (
            isinstance(request, DashboardRetryRequest) and request.worker_id is not None
        ):
            request = request.model_copy(
                update={
                    "worker_id": redact_worker_text(
                        request.worker_id,
                        dashboard_secret_values,
                        max_length=200,
                    )
                }
            )
        elif isinstance(request, DashboardClarifyRequest):
            request = request.model_copy(
                update={
                    "clarification": redact_worker_text(
                        request.clarification,
                        dashboard_secret_values,
                        max_length=MAX_AGENT_OUTPUT_LENGTH,
                    )
                }
            )
        elif isinstance(request, DashboardAnswerQuestionRequest):
            request = request.model_copy(
                update={
                    "answer": redact_worker_text(
                        request.answer,
                        dashboard_secret_values,
                        max_length=1_000,
                    )
                }
            )
        elif isinstance(request, DashboardStopRequest):
            request = request.model_copy(
                update={
                    "reason": redact_worker_text(
                        request.reason,
                        dashboard_secret_values,
                        max_length=MAX_AGENT_OUTPUT_LENGTH,
                    )
                }
            )
        repository = getattr(request, "repository", None)
        if (
            isinstance(request, DashboardStartRequest)
            and request.action == "claim"
            and request.repository is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Repository is required for claim actions",
            )
        if (
            repository is not None
            and request.action != "stop"
            and not orchestrator.store.is_active_repository(project_id, repository)
        ):
            raise HTTPException(status_code=403, detail="Repository is not authorized")
        if isinstance(request, DashboardStopRequest):
            run = orchestrator.store.get_run(request.run_id)
            if run is None or run.project_id != project_id:
                raise HTTPException(status_code=403, detail="Run is not authorized")
            if repository is not None and run.repository != repository:
                raise HTTPException(
                    status_code=403, detail="Repository is not authorized"
                )
            pbi = _dashboard_pbi(
                orchestrator, project_id, run.repository, run.pbi_number
            )
            if pbi is None or pbi.get("active") is not True:
                raise HTTPException(status_code=403, detail="PBI is not authorized")
        if isinstance(request, DashboardRetryRequest):
            _require_dashboard_retry_run(
                orchestrator,
                project_id,
                request.repository,
                request.pbi_number,
                request.run_id,
                workflow_service,
            )
        if isinstance(
            request,
            (
                DashboardAdvanceRequest,
                DashboardApproveRequest,
                DashboardAnswerQuestionRequest,
                DashboardClarifyRequest,
                DashboardRetryRequest,
                DashboardCommitPushRequest,
            ),
        ):
            pbi = _dashboard_pbi(
                orchestrator, project_id, request.repository, request.pbi_number
            )
            if pbi is None or pbi.get("active") is not True:
                raise HTTPException(status_code=403, detail="PBI is not authorized")
            if isinstance(request, DashboardAnswerQuestionRequest):
                _require_dashboard_operator_question(
                    orchestrator,
                    project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                    request.question_id,
                    request.revision,
                )
            elif isinstance(request, DashboardCommitPushRequest):
                _require_dashboard_delivery_run(
                    orchestrator,
                    project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                )
            elif isinstance(request, DashboardAdvanceRequest):
                _require_dashboard_advance_run(
                    orchestrator,
                    project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                )
            elif isinstance(
                request, (DashboardApproveRequest, DashboardClarifyRequest)
            ):
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
                    DashboardStartRequest,
                    DashboardAdvanceRequest,
                    DashboardApproveRequest,
                    DashboardAnswerQuestionRequest,
                    DashboardClarifyRequest,
                    DashboardRetryRequest,
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
                    DashboardAdvanceRequest,
                    DashboardApproveRequest,
                    DashboardAnswerQuestionRequest,
                    DashboardClarifyRequest,
                    DashboardRetryRequest,
                    DashboardCommitPushRequest,
                ),
            )
            else None
        )
        raw_action_request = request.model_dump(exclude_none=True)
        if (
            len(
                json.dumps(
                    raw_action_request, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            > MAX_DASHBOARD_ACTION_BYTES
        ):
            raise HTTPException(
                status_code=413, detail="Dashboard action request is too large"
            )
        action_request = cast(
            dict[str, object],
            safe_dashboard_value(raw_action_request, dashboard_secret_values),
        )
        if (
            len(
                json.dumps(
                    action_request, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            > MAX_DASHBOARD_ACTION_BYTES
        ):
            raise HTTPException(
                status_code=413, detail="Dashboard action request is too large"
            )
        if isinstance(request, DashboardAnswerQuestionRequest):
            action_request["answer"] = "[redacted]"
        action = orchestrator.store.begin_action(
            project_id,
            request.action,
            action_request,
            repository,
            pbi_number,
            run_id,
        )
        workflow_id = workflow_id or getattr(request, "workflow_id", None)
        try:
            result = _execute_dashboard_action(
                orchestrator,
                project_id,
                request,
                agent_worker,
                dashboard_secret_values,
                graph_safety_service,
            )
        except (ProviderError, StoreError, WorkflowError) as exc:
            error = redact_worker_text(
                str(exc), dashboard_secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            )
            failed = orchestrator.store.finish_action(
                str(action["id"]), "failed", error=error
            )
            safe_action = cast(
                dict[str, object],
                safe_dashboard_value(failed, dashboard_secret_values),
            )
            return {
                "action": safe_action,
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
                    dashboard_secret_values,
                    graph_safety_service,
                    workflow_id,
                ),
            }
        except Exception as exc:
            error = redact_worker_text(
                str(exc), dashboard_secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            )
            failed = orchestrator.store.finish_action(
                str(action["id"]), "failed", error=error
            )
            safe_action = cast(
                dict[str, object],
                safe_dashboard_value(failed, dashboard_secret_values),
            )
            return {
                "action": safe_action,
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
                    dashboard_secret_values,
                    graph_safety_service,
                    workflow_id,
                ),
            }
        safe_result = safe_dashboard_value(result, dashboard_secret_values)
        safe_result_mapping = cast(dict[str, object], safe_result)
        action_status = "succeeded"
        action_error: str | None = None
        delivery = cast(dict[str, object] | None, safe_result_mapping.get("delivery"))
        if isinstance(delivery, dict) and delivery.get("status") == "blocked":
            action_status = "failed"
            action_error = redact_worker_text(
                str(delivery.get("evidence") or "Git delivery is blocked"),
                dashboard_secret_values,
                max_length=MAX_AGENT_OUTPUT_LENGTH,
            )
        completed = orchestrator.store.finish_action(
            str(action["id"]),
            action_status,
            result=safe_result_mapping,
            error=action_error,
        )
        safe_action = cast(
            dict[str, object], safe_dashboard_value(completed, dashboard_secret_values)
        )
        return {
            "action": safe_action,
            "result": safe_action["result"],
            "state": _dashboard_state_or_none(
                orchestrator,
                project_id,
                DEFAULT_EVENT_LIMIT,
                archived,
                workflow_service,
                scheduler,
                scheduler_config,
                agent_worker,
                dashboard_secret_values,
                graph_safety_service,
                workflow_id,
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
