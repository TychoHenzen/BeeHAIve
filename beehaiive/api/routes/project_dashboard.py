from collections.abc import Callable
from typing import Any, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query

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
from beehaiive.dashboard.values import safe_dashboard_value
from beehaiive.provider import ProviderError
from beehaiive.storage import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT, StoreError
from beehaiive.workflow import WorkflowError


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


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    agent_worker = context["agent_worker"]
    dashboard_secret_values = context["dashboard_secret_values"]
    meta_review_service = context["meta_review_service"]
    orchestrator = context["orchestrator"]
    pbi_creation_service = context["pbi_creation_service"]
    require_mutation_access = context["require_mutation_access"]
    require_project_access = context["require_project_access"]
    scheduler = context["scheduler"]
    scheduler_config = context["scheduler_config"]
    workflow_service = context["workflow_service"]

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
        archived: bool = Query(default=False),
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for dashboard actions",
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
        action_request = request.model_dump(exclude_none=True)
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
        try:
            result = _execute_dashboard_action(
                orchestrator,
                project_id,
                request,
                agent_worker,
                dashboard_secret_values,
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
