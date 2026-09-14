from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.models import (
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    LeaseStatus,
    WorkflowError,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_fails_when_workspace_heartbeat_thread_stays_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/stuck-heartbeat",
        tmp_path / "stuck-heartbeat-worktree",
    )
    manager._workspace_leases[run.run_id] = lease
    manager._workspace_validators[run.run_id] = lambda: None
    heartbeat_threads = []

    class StuckThread:
        def __init__(self, target, name, daemon) -> None:
            del target, name, daemon
            self.joins: list[float | None] = []
            heartbeat_threads.append(self)

        def start(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr("beehaiive.agent_parts.worker_run_mixin.Thread", StuckThread)
    try:
        manager._run(run.run_id, run.lease_token or "")

        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        assert "heartbeat did not stop" in (failed.last_error or "")
        assert len(heartbeat_threads) == 1
        assert len(heartbeat_threads[0].joins) == 2
        released = workflow_service.workspace_for_run(run.run_id)
        assert released is not None and released.status is LeaseStatus.RELEASED
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_reports_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/cleanup-failure",
        tmp_path / "cleanup-failure-worktree",
    )
    manager._workspace_leases[run.run_id] = lease
    manager._workspace_validators[run.run_id] = lambda: None
    original_discard = workflow_service.discard_workspace

    def fail_discard(_lease_id: str, _reason: str):
        raise WorkflowError("cleanup unavailable")

    monkeypatch.setattr(workflow_service, "discard_workspace", fail_discard)
    try:
        with pytest.raises(StoreError, match="Worker workspace cleanup failed"):
            manager._run(run.run_id, run.lease_token or "")
        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
    finally:
        monkeypatch.setattr(workflow_service, "discard_workspace", original_discard)
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            original_discard(remaining.lease_id, "test cleanup")
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_manager_records_unexpected_worker_exception() -> None:
    run = RunState(
        "run-1",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class StubStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            self.failure = (run_id, error, lease_token)

    class FailingOrchestrator:
        def __init__(self) -> None:
            self.store = StubStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            raise RuntimeError("worker exploded")

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = FailingOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.failure == (
        run.run_id,
        "Agent worker failed: worker exploded",
        "lease-1",
    )
