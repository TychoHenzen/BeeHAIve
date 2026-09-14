from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from beehaiive import Stage
from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.helpers.http import _handle_workflow_error as _handle_workflow_error
from beehaiive.api.helpers.http import _required_header as _required_header
from beehaiive.api.helpers.serialization import _run_dict as _run_dict
from beehaiive.api.models import AdvanceRequest as AdvanceRequest
from beehaiive.api.models import FailureRequest as FailureRequest
from beehaiive.api.models import HandoffRequest as HandoffRequest
from beehaiive.api.models import TaskQuestionAnswer as TaskQuestionAnswer
from beehaiive.models import RunStatus
from beehaiive.storage import StoreError
from beehaiive.workflow import WorkflowError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    orchestrator = context["orchestrator"]
    require_mutation_access = context["require_mutation_access"]
    require_workflow_service = context["require_workflow_service"]
    routing_snapshot = context["routing_snapshot"]

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
        token = _required_header(lease_token, "X-Lease-Token")
        try:
            run = orchestrator.store.validate_lease(run_id, token)
        except StoreError as exc:
            raise HTTPException(
                status_code=403, detail="Run is not authorized"
            ) from exc
        if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
            raise HTTPException(
                status_code=409,
                detail="Only an active implementation run can execute a model",
            )
        service = require_workflow_service()
        workspace = _handle_workflow_error(lambda: service.workspace_for_run(run_id))
        if workspace is None:
            raise HTTPException(
                status_code=409,
                detail="A leased workspace is required before model execution",
            )
        gate = _handle_workflow_error(
            lambda: service.before_model_call(workspace.lease_id)
        )
        if not gate.allowed:
            raise HTTPException(status_code=409, detail=gate.as_dict())
        executor = orchestrator.model_executor
        prepare_run = getattr(executor, "prepare_run", None)
        release_run = getattr(executor, "release_run", None)
        if not callable(prepare_run) or not callable(release_run):
            raise HTTPException(
                status_code=503,
                detail="Model executor does not support leased workspace execution",
            )
        prepared = False
        repository = service.worktrees.repository
        executor_repository = getattr(executor, "repository", repository)
        if Path(executor_repository).resolve() != Path(repository).resolve():
            raise HTTPException(
                status_code=409,
                detail="Executor and workflow service must use the same repository",
            )
        expected_repository = run.repository

        def validate_workspace() -> None:
            current_run = orchestrator.store.validate_lease(run_id, token)
            current_workspace = service.store.require_lease_token(
                workspace.lease_id, workspace.lease_token
            )
            if (
                current_run.status is not RunStatus.ACTIVE
                or current_run.stage is not Stage.IMPLEMENT
                or current_run.repository != expected_repository
                or current_workspace.agent_id != f"dashboard-run:{run_id}"
                or current_workspace.worktree_path != workspace.worktree_path
            ):
                raise WorkflowError("Run or workspace lease changed")

        try:
            _handle_store_error(
                lambda: _handle_workflow_error(
                    lambda: prepare_run(
                        run_id, expected_repository, workspace, validate_workspace
                    )
                )
            )
            prepared = True
            routing = _handle_store_error(
                lambda: orchestrator.run_implementation_attempt(run_id, token)
            )
            run = orchestrator.store.get_run(run_id)
            if run is None:  # pragma: no cover - the service already validated the run
                raise HTTPException(status_code=404, detail="Run not found")
            return {"run": _run_dict(run), "routing": routing.as_dict()}
        finally:
            if prepared:
                release_run(run_id)

    @app.post("/runs/{run_id}/question/answer")
    def answer_task_question(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: TaskQuestionAnswer,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        run = _handle_store_error(
            lambda: orchestrator.answer_operator_question(
                run_id,
                question_id=request.question_id,
                revision=request.revision,
                answer=request.answer,
                authorization_method="X-API-Key",
                operator_role="operator",
            )
        )
        return {
            **_run_dict(run),
            "operator_question": orchestrator.store.operator_question_for_run(run_id),
        }

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
