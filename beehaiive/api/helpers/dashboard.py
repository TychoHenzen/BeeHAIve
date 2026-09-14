from typing import cast

from fastapi import HTTPException

from beehaiive import Orchestrator
from beehaiive.agent import DEFAULT_DEMO_TASK, DEMO_TASK_NAME, AgentWorkerManager
from beehaiive.api.models import DashboardActionRequest as DashboardActionRequest
from beehaiive.api.models import DashboardClarifyRequest as DashboardClarifyRequest
from beehaiive.api.models import (
    DashboardCommitPushRequest as DashboardCommitPushRequest,
)
from beehaiive.api.models import DashboardStartRequest as DashboardStartRequest
from beehaiive.api.models import DashboardStopRequest as DashboardStopRequest
from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import RunState, RunStatus
from beehaiive.provider import ProviderError
from beehaiive.scheduler import AgentScheduler, SchedulerConfig
from beehaiive.storage import StoreError
from beehaiive.workflow import LeaseStatus, WorkflowService

from .serialization import _public_run_dict as _public_run_dict


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
                quality_gates = _dashboard_quality_gate_summary(
                    workflow_service, run_id
                )
                if quality_gates is not None:
                    pbi["quality_gates"] = quality_gates
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


__all__ = [
    "_dashboard_state",
    "_dashboard_state_or_none",
    "_dashboard_delivery",
    "_dashboard_quality_gates",
    "_dashboard_quality_gate_summary",
    "_dashboard_pbi",
    "_require_active_dashboard_run",
    "_require_dashboard_delivery_run",
    "_execute_dashboard_action",
]
