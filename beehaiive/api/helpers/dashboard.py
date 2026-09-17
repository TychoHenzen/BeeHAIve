import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from fastapi import HTTPException

from beehaiive import Orchestrator
from beehaiive.agent import (
    DEFAULT_DEMO_TASK,
    DEMO_TASK_NAME,
    MAX_AGENT_OUTPUT_LENGTH,
    AgentWorkerManager,
    format_worker_exception,
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
from beehaiive.api.models import (
    DashboardGraphRollbackRequest as DashboardGraphRollbackRequest,
)
from beehaiive.api.models import (
    DashboardGraphSafetyRequest as DashboardGraphSafetyRequest,
)
from beehaiive.api.models import (
    DashboardIdeaCaptureRequest as DashboardIdeaCaptureRequest,
)
from beehaiive.api.models import DashboardRequeueRequest as DashboardRequeueRequest
from beehaiive.api.models import DashboardRetryRequest as DashboardRetryRequest
from beehaiive.api.models import DashboardStartRequest as DashboardStartRequest
from beehaiive.api.models import DashboardStopRequest as DashboardStopRequest
from beehaiive.api.models import (
    DashboardSynchronizeRequest as DashboardSynchronizeRequest,
)
from beehaiive.autonomous import ADVISOR_STEP, AUTONOMOUS_STEPS
from beehaiive.dashboard import build_dashboard_state
from beehaiive.dashboard.values import mapping, safe_dashboard_value
from beehaiive.dashboard.views import queue_for_skill
from beehaiive.graph import GraphDefinition
from beehaiive.graph_safety import GraphSafetyService, graph_definition_hash
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
    "requeue": "orchestrator",
    "commit_push": "agent_worker",
    "deliver": "agent_worker",
    "graph_evaluate": "graph_safety",
    "graph_review": "graph_safety",
    "graph_activate": "graph_safety",
    "graph_rollback": "graph_safety",
    "capture_idea": "autonomous_service",
}
DASHBOARD_ACTION_READBACK = {
    action: "action,state" for action in DASHBOARD_ACTION_OWNERS
}


def _workflow_skill_ids() -> list[str]:
    roots = [Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"]
    configured = os.environ.get("BEEHAIIVE_DOD_GUARD_SKILLS", "").strip()
    if configured:
        roots.append(Path(configured))
    known = {
        f"skill/{Path(step.skill_path).parent.name}"
        for step in (*AUTONOMOUS_STEPS, ADVISOR_STEP)
        if Path(step.skill_path).is_file()
    }
    known.update(
        f"skill/{skill_file.parent.name}"
        for root in roots
        if root.is_dir()
        for skill_file in root.glob("*/SKILL.md")
        if skill_file.is_file()
    )
    return sorted(known)


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
    graph_safety_service: GraphSafetyService | None = None,
    workflow_id: str | None = None,
    autonomous_service: object | None = None,
) -> dict[str, object]:
    orchestrator.synchronize(project_id)
    state = orchestrator.store.project_state(project_id, event_limit)
    actions = orchestrator.store.actions_for_project(project_id)
    dashboard = build_dashboard_state(state, actions, archived)
    dashboard["archived"] = archived
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
    dashboard["graph"] = _dashboard_graph_state(
        orchestrator, workflow_id, graph_safety_service
    )
    dashboard["workflow_ids"] = list(orchestrator.store.graph_workflow_ids())
    dashboard["workflow_skill_ids"] = _workflow_skill_ids()
    status_for_work_item = getattr(autonomous_service, "status_for_work_item", None)
    if callable(status_for_work_item):
        repositories = cast(list[dict[str, object]], dashboard["repositories"])
        for repository in repositories:
            repository_name = repository.get("name")
            if not isinstance(repository_name, str):
                continue
            for pbi in cast(list[dict[str, object]], repository["pbis"]):
                number = pbi.get("number")
                if type(number) is not int:
                    continue
                live = status_for_work_item(project_id, repository_name, number)
                if not isinstance(live, Mapping):
                    continue
                live_values = cast(Mapping[str, object], live)
                pbi["autonomous_status"] = live_values.get("status")
                pbi["autonomous_current_step"] = live_values.get("current_step")
                pbi["autonomous_handoffs"] = live_values.get("handoffs", [])
                pbi["autonomous_process"] = live_values.get("process")
                if live_values.get("current_step"):
                    pbi["workflow_queue"] = queue_for_skill(
                        live_values.get("current_step"), live_values.get("status")
                    )
                live_events = live_values.get("session_events", [])
                if (
                    (isinstance(live_events, list) and live_events)
                    or live_values.get("current_step")
                    or live_values.get("process")
                ):
                    pbi["agent_session"] = {
                        "worker_id": "autonomous",
                        "task": (
                            "Running skill: "
                            f"{live_values.get('current_step', 'unknown')}"
                        ),
                        "state": "active",
                        "events": live_events,
                        "activity_state": live_values.get("status"),
                        "started_at": live_values.get("started_at"),
                        "step_started_at": live_values.get("step_started_at"),
                        "last_output_at": live_values.get("last_output_at"),
                        "last_output": live_values.get("last_output"),
                        "process": live_values.get("process"),
                    }
    if workflow_service is not None:
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
    dashboard["queues"] = _dashboard_queue_view(dashboard)
    return cast(dict[str, object], safe_dashboard_value(dashboard, secret_values))


def _dashboard_queue_view(
    dashboard: Mapping[str, object],
) -> list[dict[str, object]]:
    queue_values = {
        str(queue["id"]): dict(queue)
        for queue in cast(Sequence[Mapping[str, object]], dashboard.get("queues", []))
        if isinstance(queue.get("id"), str)
    }
    items: dict[str, list[dict[str, object]]] = {
        queue_id: [] for queue_id in queue_values
    }
    for repository in cast(
        Sequence[Mapping[str, object]], dashboard.get("repositories", [])
    ):
        for pbi in cast(Sequence[Mapping[str, object]], repository.get("pbis", [])):
            queue = mapping(pbi.get("workflow_queue"))
            queue_id = queue.get("id")
            if not isinstance(queue_id, str) or queue_id not in items:
                continue
            items[queue_id].append(
                {
                    "repository": repository.get("name"),
                    "pbi_number": pbi.get("number"),
                    "title": pbi.get("title"),
                    "reason": queue.get("reason"),
                    "evidence": dict(mapping(queue.get("evidence"))),
                    "next_skill": queue.get("next_skill"),
                }
            )
    return [
        {
            **queue_values[queue_id],
            "count": len(items[queue_id]),
            "items": items[queue_id],
        }
        for queue_id in queue_values
    ]


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
    graph_safety_service: GraphSafetyService | None = None,
    workflow_id: str | None = None,
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
            graph_safety_service,
            workflow_id,
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


def _dashboard_graph_state(
    orchestrator: Orchestrator,
    workflow_id: str | None,
    graph_safety_service: GraphSafetyService | None,
) -> dict[str, object]:
    if not workflow_id:
        return {
            "workflow_id": None,
            "definitions": [],
            "active": None,
            "empty": True,
            "message": "Enter a workflow ID to view graph state.",
        }
    service = graph_safety_service or GraphSafetyService(orchestrator.store)
    store = service.store
    if store is None:
        return {
            "workflow_id": workflow_id,
            "definitions": [],
            "active": None,
            "empty": True,
            "message": "Graph state is unavailable.",
        }
    definitions = store.graph_definitions_for(workflow_id)
    active = store.active_graph_version(workflow_id)
    selected_definitions = list(definitions[-100:])
    active_revision = active.get("revision") if active is not None else None
    if active_revision is not None and not any(
        definition.revision == active_revision for definition in selected_definitions
    ):
        active_definition = next(
            (
                definition
                for definition in definitions
                if definition.revision == active_revision
            ),
            None,
        )
        if active_definition is not None:
            selected_definitions = [active_definition, *list(definitions[-99:])]
            selected_definitions.sort(key=lambda definition: definition.revision)
    entries: list[dict[str, object]] = []
    for definition in selected_definitions:
        definition_hash = graph_definition_hash(definition)
        evidence = store.graph_safety_evidence_for(
            definition.workflow_id, definition.revision
        )
        review = store.graph_safety_review_for(
            definition.workflow_id, definition.revision
        )
        entry = definition.as_dict()
        entry.update(
            {
                "definition_hash": definition_hash,
                "safety_evidence": evidence,
                "review": review,
                "active": bool(
                    active is not None and active.get("revision") == definition.revision
                ),
            }
        )
        entries.append(entry)
    return {
        "workflow_id": workflow_id,
        "definitions": entries,
        "active": dict(active) if active is not None else None,
        "empty": not entries,
        "message": "No graph definitions available." if not entries else "",
    }


def _redact_graph_value(
    value: object, secret_values: tuple[str, ...], depth: int = 0
) -> object:
    if depth > 12:
        raise StoreError("Graph input is nested too deeply")
    if isinstance(value, str):
        return redact_worker_text(
            value, secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            redact_worker_text(
                str(key), secret_values, max_length=MAX_AGENT_OUTPUT_LENGTH
            ): _redact_graph_value(item, secret_values, depth + 1)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        sequence = cast(Sequence[object], value)
        return [
            _redact_graph_value(item, secret_values, depth + 1) for item in sequence
        ]
    return value


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
            f"Agent worker failed to start: {format_worker_exception(exc)}",
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


DashboardActionHandler = Callable[..., dict[str, object]]


def _execute_capture_idea(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
    autonomous_service: object | None = None,
    action_id: str | None = None,
) -> dict[str, object]:
    del orchestrator, agent_worker, secret_values
    if not isinstance(request, DashboardIdeaCaptureRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    capture = getattr(autonomous_service, "capture_idea", None)
    if not callable(capture) or action_id is None:
        raise StoreError("Idea capture service is not configured")
    return cast(dict[str, object], capture(project_id, request.idea, action_id))


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
    if request.pbi_number is None:
        run = agent_worker.claim(
            project_id,
            request.repository,
            request.worker_id or "dashboard-operator",
            task=task,
        )
    else:
        run = agent_worker.claim(
            project_id,
            request.repository,
            request.worker_id or "dashboard-operator",
            task=task,
            expected_pbi_number=request.pbi_number,
        )
    if run is None:
        raise StoreError("No claimable PBI is available for this repository")
    try:
        agent_worker.start(run)
    except Exception as exc:
        failure = redact_worker_text(
            f"Agent worker failed to start: {format_worker_exception(exc)}",
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


def _execute_requeue(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    del agent_worker, secret_values
    if not isinstance(request, DashboardRequeueRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    actions = orchestrator.store.actions_for_project(project_id)
    blocked = any(
        action.get("kind") == "autonomous_start"
        and action.get("repository") == request.repository
        and action.get("pbi_number") == request.pbi_number
        and action.get("run_id") == request.run_id
        and action.get("status") == "failed"
        for action in actions
    )
    if not blocked:
        raise StoreError("Only a blocked autonomous run can be made claimable")
    return {
        "pbi": orchestrator.store.set_pbi_claimable(
            project_id, request.repository, request.pbi_number
        )
    }


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


def _execute_graph_safety(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
    graph_safety_service: GraphSafetyService | None = None,
) -> dict[str, object]:
    del project_id, agent_worker
    if not isinstance(request, DashboardGraphSafetyRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    service = graph_safety_service or GraphSafetyService(orchestrator.store)
    candidate_payload = _redact_graph_value(request.candidate, secret_values)
    if not isinstance(candidate_payload, Mapping):
        raise StoreError("Graph candidate must be an object")
    candidate = GraphDefinition.from_dict(cast(Mapping[str, object], candidate_payload))
    if candidate.workflow_id != request.workflow_id:
        raise StoreError("Graph workflow ID does not match the selected workflow")
    baseline = (
        GraphDefinition.from_dict(
            cast(
                Mapping[str, object],
                _redact_graph_value(request.baseline, secret_values),
            )
        )
        if request.baseline is not None
        else None
    )
    fixtures = cast(
        Mapping[str, object], _redact_graph_value(request.fixtures, secret_values)
    )
    baseline_fixtures = cast(
        Mapping[str, object],
        _redact_graph_value(request.baseline_fixtures, secret_values),
    )
    evaluation = service.evaluate(
        candidate,
        fixtures,
        baseline=baseline,
        baseline_fixtures=baseline_fixtures,
    )
    result: dict[str, object] = {"graph": evaluation.as_dict()}
    if request.action == "graph_review":
        result["review"] = service.review(evaluation, "operator").as_dict()
    elif request.action == "graph_activate":
        result["activation"] = service.activate(evaluation, "operator").as_dict()
    return result


def _execute_graph_rollback(
    orchestrator: Orchestrator,
    project_id: str,
    request: DashboardActionRequest,
    agent_worker: AgentWorkerManager | None,
    secret_values: tuple[str, ...],
    graph_safety_service: GraphSafetyService | None = None,
) -> dict[str, object]:
    del project_id, agent_worker, secret_values
    if not isinstance(request, DashboardGraphRollbackRequest):
        raise StoreError(f"No dashboard handler for action: {request.action}")
    activation = (
        graph_safety_service or GraphSafetyService(orchestrator.store)
    ).rollback(request.workflow_id, request.revision, "operator")
    return {"graph": {"activation": activation.as_dict()}}


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
    "requeue": _execute_requeue,
    "commit_push": _execute_delivery,
    "deliver": _execute_delivery,
    "graph_evaluate": _execute_graph_safety,
    "graph_review": _execute_graph_safety,
    "graph_activate": _execute_graph_safety,
    "graph_rollback": _execute_graph_rollback,
    "capture_idea": _execute_capture_idea,
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
    graph_safety_service: GraphSafetyService | None = None,
    autonomous_service: object | None = None,
    action_id: str | None = None,
) -> dict[str, object]:
    if isinstance(request, DashboardGraphSafetyRequest):
        return _execute_graph_safety(
            orchestrator,
            project_id,
            request,
            agent_worker,
            secret_values,
            graph_safety_service,
        )
    if isinstance(request, DashboardGraphRollbackRequest):
        return _execute_graph_rollback(
            orchestrator,
            project_id,
            request,
            agent_worker,
            secret_values,
            graph_safety_service,
        )
    handler = DASHBOARD_ACTION_DISPATCH.get(request.action)
    if handler is None:
        raise StoreError(f"Unsupported dashboard action: {request.action}")
    if request.action == "capture_idea":
        return handler(
            orchestrator,
            project_id,
            request,
            agent_worker,
            secret_values,
            autonomous_service,
            action_id,
        )
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
