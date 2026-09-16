from collections.abc import Callable
from typing import cast

from fastapi import HTTPException

from beehaiive import Orchestrator
from beehaiive.agent import (
    DEFAULT_DEMO_TASK,
    DEMO_TASK_NAME,
    MAX_AGENT_OUTPUT_LENGTH,
    AgentWorkerManager,
    redact_worker_text,
)
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
from beehaiive.api.models import (
    DashboardSynchronizeRequest as DashboardSynchronizeRequest,
)
from beehaiive.dashboard import build_dashboard_state
from beehaiive.dashboard.values import safe_dashboard_value
from beehaiive.models import RunState, RunStatus
from beehaiive.scheduler import AgentScheduler, SchedulerConfig
from beehaiive.storage import StoreError
from beehaiive.workflow import LeaseStatus, WorkflowService

from .serialization import _public_run_dict as _public_run_dict

DASHBOARD_ACTION_OWNERS = {
    "start": "agent_worker",
    "claim": "agent_worker",
    "synchronize": "orchestrator",
    "sync": "orchestrator",
    "stop": "orchestrator",
    "advance": "orchestrator",
    "approve": "action_log",
    "clarify": "action_log",
    "answer_question": "orchestrator",
    "retry": "agent_worker",
    "commit_push": "agent_worker",
    "deliver": "agent_worker",
}
DASHBOARD_ACTION_READBACK = {
    action: "action,state" for action in DASHBOARD_ACTION_OWNERS
}


def _dashboard_state(
    orchestrator: Orchestrator,
    project_id: str,
    event_limit: int,
    archived: bool = False,
    workflow_service: WorkflowService | None = None,
    scheduler: AgentScheduler | None = None,
    scheduler_config: SchedulerConfig | None = None,
    agent_worker: AgentWorkerManager | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, object]:
    orchestrator.synchronize(project_id)
    state = orchestrator.store.project_state(project_id, event_limit)
    actions = orchestrator.store.actions_for_project(project_id)
    dashboard = build_dashboard_state(state, actions, archived)
    dashboard["supported_actions"] = sorted(DASHBOARD_ACTIONS)
    dashboard["supported_action_owners"] = dict(DASHBOARD_ACTION_OWNERS)
    dashboard["supported_action_readback"] = dict(DASHBOARD_ACTION_READBACK)
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
        return cast(dict[str, object], safe_dashboard_value(dashboard, secret_values))
    repositories = cast(list[dict[str, object]], dashboard["repositories"])
    for repository in repositories:
        for pbi in cast(list[dict[str, object]], repository["pbis"]):
            run_id = pbi.get("run_id")
            if isinstance(run_id, str):
                quality_gates = _dashboard_quality_gate_summary(
                    workflow_service, run_id
                )
                if quality_gates is not None:
                    pbi["quality_gates"] = quality_gates
                delivery = _dashboard_delivery(workflow_service, run_id)
                if delivery is not None:
                    pbi["delivery"] = delivery
    return cast(dict[str, object], safe_dashboard_value(dashboard, secret_values))


def _dashboard_state_or_none(
    orchestrator: Orchestrator,
    project_id: str,
    event_limit: int,
    archived: bool = False,
    workflow_service: WorkflowService | None = None,
    scheduler: AgentScheduler | None = None,
    scheduler_config: SchedulerConfig | None = None,
    agent_worker: AgentWorkerManager | None = None,
    secret_values: tuple[str, ...] = (),
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
            secret_values,
        )
    except Exception:
        return None


def _dashboard_delivery(
    workflow_service: WorkflowService, run_id: str
) -> dict[str, object] | None:
    lease = workflow_service.workspace_for_run(run_id)
    if lease is None:
        return None
    gate = workflow_service.store.latest_gate(lease.lease_id, "git_delivery")
    if gate is None:
        if lease.status is LeaseStatus.RETAINED:
            return {
                "status": "retained",
                "commit_sha": None,
                "branch": lease.branch,
                "evidence": "A retained worktree is available for delivery retry.",
                "retry_available": True,
            }
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


def _dashboard_quality_gates(
    workflow_service: WorkflowService, run_id: str
) -> dict[str, object] | None:
    lease = workflow_service.workspace_for_run(run_id)
    if lease is None:
        return None
    gates: dict[str, object] = {
        name: gate.as_dict()
        for name in ("model_call", "git_delivery")
        if (gate := workflow_service.store.latest_gate(lease.lease_id, name))
        is not None
    }
    return gates or None


def _dashboard_quality_gate_summary(
    workflow_service: WorkflowService, run_id: str
) -> dict[str, object] | None:
    lease = workflow_service.workspace_for_run(run_id)
    if lease is None:
        return None
    gates: dict[str, object] = {}
    for name in ("model_call", "git_delivery"):
        gate = workflow_service.store.latest_gate(lease.lease_id, name)
        if gate is not None:
            gates[name] = {
                "gate": gate.gate,
                "allowed": gate.allowed,
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "status": check.status,
                        "category": check.category,
                        "required": check.required,
                        "exit_code": check.exit_code,
                    }
                    for check in gate.checks
                ],
            }
    return gates or None


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
        or run.lease_token is None
    ):
        raise HTTPException(status_code=403, detail="Run is not authorized")
    try:
        return orchestrator.store.validate_lease(run_id, run.lease_token)
    except StoreError as exc:
        raise HTTPException(status_code=403, detail="Run is not authorized") from exc


def _require_dashboard_operator_question(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    pbi_number: int,
    run_id: str,
    question_id: str,
    revision: int,
) -> None:
    run = orchestrator.store.get_run(run_id)
    if (
        run is None
        or run.project_id != project_id
        or run.repository != repository
        or run.pbi_number != pbi_number
        or run.status is not RunStatus.AWAITING_OPERATOR
    ):
        raise HTTPException(status_code=403, detail="Run is not authorized")
    question = orchestrator.store.operator_question_for_run(run_id)
    if (
        question is None
        or question.get("question_id") != question_id
        or question.get("revision") != revision
        or question.get("status") != "pending"
    ):
        raise HTTPException(status_code=409, detail="Operator question is stale")


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


def _require_dashboard_advance_run(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    pbi_number: int,
    run_id: str,
) -> RunState:
    return _require_active_dashboard_run(
        orchestrator, project_id, repository, pbi_number, run_id
    )


def _require_dashboard_retry_run(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    pbi_number: int,
    run_id: str,
    workflow_service: WorkflowService | None = None,
) -> RunState:
    run = orchestrator.store.get_run(run_id)
    pbi = _dashboard_pbi(orchestrator, project_id, repository, pbi_number)
    if (
        run is None
        or pbi is None
        or run.project_id != project_id
        or run.repository != repository
        or run.pbi_number != pbi_number
        or run.status is not RunStatus.FAILED
        or pbi.get("claimable") is not True
    ):
        raise HTTPException(status_code=409, detail="Run is not retryable")
    if workflow_service is not None:
        lease = workflow_service.workspace_for_run(run_id)
        if lease is not None and lease.status is LeaseStatus.RETAINED:
            raise HTTPException(
                status_code=409,
                detail="The run has a retained worktree; retry delivery instead",
            )
    return run


def _dashboard_worker_task(
    orchestrator: Orchestrator,
    agent_worker: AgentWorkerManager,
    run_id: str | None = None,
) -> str:
    if run_id is not None:
        session = orchestrator.store.get_agent_session(run_id)
        task = None if session is None else session.get("task")
        if isinstance(task, str) and task.strip():
            return task
    task = getattr(getattr(agent_worker, "executor", None), "task", None)
    return task if isinstance(task, str) and task.strip() else DEFAULT_DEMO_TASK


def _start_dashboard_worker(
    orchestrator: Orchestrator,
    project_id: str,
    repository: str,
    worker_id: str | None,
    agent_worker: AgentWorkerManager | None,
    expected_run_id: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, object]:
    if agent_worker is None:
        raise StoreError("Agent worker is not configured")
    task = _dashboard_worker_task(orchestrator, agent_worker, expected_run_id)
    owner_id = worker_id or "dashboard-operator"
    if expected_run_id is None:
        run = agent_worker.claim(project_id, repository, owner_id, task=task)
    else:
        retry = getattr(agent_worker, "retry", None)
        if not callable(retry):
            raise StoreError("Agent worker does not support retries")
        retry_worker = cast(Callable[[str, str, str, str, str], RunState | None], retry)
        run = retry_worker(project_id, repository, owner_id, task, expected_run_id)
    if run is None:
        raise StoreError(
            "No claimable PBI is available for this repository"
            if expected_run_id is None
            else "The failed run is no longer claimable"
        )
    try:
        agent_worker.start(run)
    except Exception as exc:
        failure = redact_worker_text(
            f"Agent worker failed to start: {exc}",
            secret_values,
            max_length=MAX_AGENT_OUTPUT_LENGTH,
        )
        try:
            orchestrator.stop(run.run_id, failure)
        except StoreError as stop_error:
            raise StoreError(
                f"Agent worker failed to start and cleanup failed: {stop_error}"
            ) from exc
        raise StoreError(failure) from exc
    return {
        "run": _public_run_dict(run),
        "worker": {"status": "started", "task": DEMO_TASK_NAME},
    }


DashboardActionHandler = Callable[
    [
        Orchestrator,
        str,
        DashboardActionRequest,
        AgentWorkerManager | None,
        tuple[str, ...],
    ],
    dict[str, object],
]


def _execute_synchronize(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del request, agent_worker, secret_values
    orchestrator.synchronize(project_id, force_refresh=True)
    return {"project_id": project_id, "status": "synchronized"}


def _execute_start(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(request, DashboardStartRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    if request.repository is None:
        orchestrator.synchronize(project_id, force_refresh=True)
        return {"project_id": project_id, "status": "synchronized"}
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
        failure = redact_worker_text(
            f"Agent worker failed to start: {exc}",
            secret_values,
            max_length=MAX_AGENT_OUTPUT_LENGTH,
        )
        try:
            orchestrator.stop(run.run_id, failure)
        except StoreError as stop_error:
            raise StoreError(
                f"Agent worker failed to start and cleanup failed: {stop_error}"
            ) from exc
        raise StoreError(failure) from exc
    return {
        "run": _public_run_dict(run),
        "worker": {"status": "started", "task": DEMO_TASK_NAME},
    }


def _execute_retry(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(request, DashboardRetryRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    return _start_dashboard_worker(
        orchestrator,
        project_id,
        request.repository,
        request.worker_id,
        agent_worker,
        expected_run_id=request.run_id,
        secret_values=secret_values,
    )


def _execute_advance(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del project_id, agent_worker, secret_values
    if not isinstance(request, DashboardAdvanceRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    run = orchestrator.store.get_run(request.run_id)
    if run is None or run.lease_token is None:
        raise StoreError("An active run lease is required for advancement")
    validated = orchestrator.store.validate_lease(request.run_id, run.lease_token)
    advanced = orchestrator.advance(
        request.run_id, request.target, validated.lease_token or run.lease_token
    )
    return {"run": _public_run_dict(advanced)}


def _execute_stop(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del project_id, secret_values
    if not isinstance(request, DashboardStopRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    stopped = orchestrator.stop(request.run_id, request.reason or "Stopped by operator")
    if agent_worker is not None:
        agent_worker.cancel(request.run_id)
    return {"run": _public_run_dict(stopped)}


def _execute_answer_question(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del project_id, agent_worker, secret_values
    if not isinstance(request, DashboardAnswerQuestionRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    run = orchestrator.answer_operator_question(
        request.run_id,
        question_id=request.question_id,
        revision=request.revision,
        answer=request.answer,
        authorization_method="X-API-Key",
        operator_role="operator",
    )
    return {
        "run_id": run.run_id,
        "run_status": run.status.value,
        "question_id": request.question_id,
        "revision": request.revision,
        "question_status": "answered",
    }


def _execute_clarify(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del project_id, agent_worker
    if not isinstance(request, DashboardClarifyRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    if not request.clarification.strip():
        raise StoreError("A clarification message is required")
    clarification = redact_worker_text(
        request.clarification.strip(),
        secret_values,
        max_length=MAX_AGENT_OUTPUT_LENGTH,
    )
    run = orchestrator.record_operator_action(
        request.run_id,
        "clarify",
        {"message": clarification},
    )
    return {
        "message": clarification,
        "run_id": run.run_id,
        "run_status": run.status.value,
    }


def _execute_delivery(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del orchestrator, project_id, secret_values
    if not isinstance(request, DashboardCommitPushRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    if agent_worker is None:
        raise StoreError("Agent worker is not configured")
    return {"delivery": agent_worker.commit_and_push(request.run_id).as_dict()}


def _execute_approve(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del project_id, agent_worker, secret_values
    if not isinstance(request, DashboardApproveRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    run = orchestrator.record_operator_action(request.run_id, "approve", {})
    return {"approved": True, "run_id": run.run_id, "run_status": run.status.value}


DASHBOARD_ACTION_DISPATCH: dict[str, DashboardActionHandler] = {
    "start": _execute_start,
    "claim": _execute_start,
    "synchronize": _execute_synchronize,
    "sync": _execute_synchronize,
    "stop": _execute_stop,
    "advance": _execute_advance,
    "approve": _execute_approve,
    "clarify": _execute_clarify,
    "answer_question": _execute_answer_question,
    "retry": _execute_retry,
    "commit_push": _execute_delivery,
    "deliver": _execute_delivery,
}
if (
    frozenset(DASHBOARD_ACTION_DISPATCH) != frozenset(DASHBOARD_ACTION_OWNERS)
    or frozenset(DASHBOARD_ACTION_DISPATCH) != frozenset(DASHBOARD_ACTION_READBACK)
    or frozenset(DASHBOARD_ACTION_DISPATCH) != DASHBOARD_ACTIONS
):
    raise RuntimeError("Dashboard action dispatch is incomplete")


def _execute_dashboard_action(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, object]:
    handler = DASHBOARD_ACTION_DISPATCH.get(request.action)
    if handler is None:
        raise StoreError(f"Unsupported dashboard action: {request.action}")
    return handler(orchestrator, project_id, request, agent_worker, secret_values)


__all__ = [
    "_dashboard_state",
    "_dashboard_state_or_none",
    "_dashboard_delivery",
    "_dashboard_quality_gates",
    "_dashboard_quality_gate_summary",
    "_dashboard_pbi",
    "_require_active_dashboard_run",
    "_require_dashboard_operator_question",
    "_require_dashboard_delivery_run",
    "_require_dashboard_advance_run",
    "_require_dashboard_retry_run",
    "DASHBOARD_ACTIONS",
    "DASHBOARD_ACTION_OWNERS",
    "DASHBOARD_ACTION_READBACK",
    "DASHBOARD_ACTION_DISPATCH",
    "_execute_dashboard_action",
]
