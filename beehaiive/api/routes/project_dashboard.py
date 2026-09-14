from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query

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
    _require_dashboard_delivery_run as _require_dashboard_delivery_run,
)
from beehaiive.api.helpers.http import (
    _handle_meta_review_error as _handle_meta_review_error,
)
from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.helpers.http import _required_header as _required_header
from beehaiive.api.helpers.serialization import _run_dict as _run_dict
from beehaiive.api.models import DashboardActionRequest as DashboardActionRequest
from beehaiive.api.models import DashboardApproveRequest as DashboardApproveRequest
from beehaiive.api.models import DashboardClarifyRequest as DashboardClarifyRequest
from beehaiive.api.models import (
    DashboardCommitPushRequest as DashboardCommitPushRequest,
)
from beehaiive.api.models import DashboardStopRequest as DashboardStopRequest
from beehaiive.api.models import MetaReviewDecisionRequest as MetaReviewDecisionRequest
from beehaiive.api.models import MetaReviewRequest as MetaReviewRequest
from beehaiive.provider import ProviderError
from beehaiive.storage import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT, StoreError
from beehaiive.workflow import WorkflowError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    agent_worker = context["agent_worker"]
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
