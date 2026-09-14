from threading import Thread
from types import SimpleNamespace

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
    WorkerCapacityError,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    LeaseStatus,
    WorkflowError,
    WorkspaceLease,
)
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_recovery_defers_runs_at_capacity_and_filters_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = [
        SimpleNamespace(
            run_id=run_id,
            project_id=project_id,
            repository=f"owner/{run_id}",
            title=run_id,
        )
        for run_id, project_id in (
            ("run-1", "allowed"),
            ("run-2", "allowed"),
            ("run-3", "outside"),
        )
    ]

    class RecoveryStore:
        def __init__(self) -> None:
            self.runs = runs

        def active_agent_sessions(self) -> tuple[SimpleNamespace, ...]:
            return tuple(self.runs)

        def get_agent_session(self, run_id: str) -> dict[str, str]:
            return {"task": run_id}

    class RecoveryOrchestrator:
        def __init__(self) -> None:
            self.store = RecoveryStore()

        def claim(
            self,
            project_id: str,
            repository: str,
            owner_id: str,
            *,
            expected_run_id: str,
            agent_session: tuple[str, str],
        ) -> SimpleNamespace:
            del project_id, repository, owner_id, agent_session
            return SimpleNamespace(
                run_id=expected_run_id, lease_token=f"lease-{expected_run_id}"
            )

    class RecoveryWorkflow:
        def cleanup_dashboard_run_workspaces(self, run_id: str) -> None:
            del run_id

    orchestrator = RecoveryOrchestrator()
    manager = AgentWorkerManager(
        orchestrator,
        SimpleNamespace(_secret_values=()),
        RecoveryWorkflow(),
    )
    manager.set_max_concurrent_workers(1)
    started: list[str] = []
    capacity_race = True

    def start(run: SimpleNamespace) -> None:
        nonlocal capacity_race
        started.append(run.run_id)
        if run.run_id == "run-1" and capacity_race:
            capacity_race = False
            raise WorkerCapacityError("slot was claimed concurrently")
        manager._threads[run.run_id] = Thread(target=lambda: None)

    monkeypatch.setattr(manager, "start", start)
    assert manager.recover({"allowed"}) == ("run-2",)
    assert started == ["run-1", "run-2"]
    assert [run.run_id for run in orchestrator.store.runs] == [
        "run-1",
        "run-2",
        "run-3",
    ]

    orchestrator.store.runs = [runs[1], runs[0]]
    assert manager.recover({"allowed"}) == ()
    assert len(started) == 2
    manager._threads.clear()
    orchestrator.store.runs = [runs[0]]
    assert manager.recover({"allowed"}) == ("run-1",)
    assert started == ["run-1", "run-2", "run-1"]


def test_worker_start_requires_workflow_service() -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    manager = AgentWorkerManager(orchestrator, executor)

    with pytest.raises(StoreError, match="Workflow service is required"):
        manager.start(run)
    with pytest.raises(WorkflowError, match="Workflow service is required"):
        manager._workspace_validator(
            run.run_id,
            WorkspaceLease(
                "lease",
                "dashboard-run:test",
                "codex/test",
                ".",
                LeaseStatus.ACTIVE,
                "created",
                "updated",
                "token",
            ),
        )
    assert manager.recover() == ()

    assert not executor._active_attempts
    store.close()
    routing_store.close()
