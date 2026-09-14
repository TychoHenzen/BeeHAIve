from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from beehaiive.api.helpers.dashboard import (
    _dashboard_quality_gates as _dashboard_quality_gates,
)
from beehaiive.api.helpers.http import _handle_workflow_error as _handle_workflow_error
from beehaiive.api.models import ConflictRepairRequest as ConflictRepairRequest
from beehaiive.api.models import WorkflowApprovalRequest as WorkflowApprovalRequest
from beehaiive.api.models import (
    WorkflowClarificationAnswer as WorkflowClarificationAnswer,
)
from beehaiive.api.models import (
    WorkflowClarificationRequest as WorkflowClarificationRequest,
)
from beehaiive.api.models import WorkflowHandoffRequest as WorkflowHandoffRequest
from beehaiive.api.models import WorkflowModelCallRequest as WorkflowModelCallRequest
from beehaiive.api.models import WorkflowStopRequest as WorkflowStopRequest
from beehaiive.api.models import WorkflowWorkspaceRequest as WorkflowWorkspaceRequest


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    conflict_repair_service = context["conflict_repair_service"]
    require_api_key = context["require_api_key"]
    require_handoff_lease_token = context["require_handoff_lease_token"]
    require_routing_run_access = context["require_routing_run_access"]
    require_workflow_access = context["require_workflow_access"]
    require_workflow_operator = context["require_workflow_operator"]
    require_workflow_service = context["require_workflow_service"]

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

    @app.get("/workflow/runs/{run_id}/quality-gates")
    def workflow_quality_gates(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        _auth: None = Depends(require_routing_run_access),
    ) -> dict[str, object]:
        quality_gates = _dashboard_quality_gates(require_workflow_service(), run_id)
        if quality_gates is None:
            raise HTTPException(status_code=404, detail="Run quality gates not found")
        return {"run_id": run_id, "quality_gates": quality_gates}

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
