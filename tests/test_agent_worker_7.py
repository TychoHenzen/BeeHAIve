import sqlite3
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.models import (
    RunStatus,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    RoutingStatus,
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    Constitution,
    LeaseStatus,
    WorkflowService,
    WorkflowStore,
    WorkspaceLease,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)
from tests.support.agent.passing_workflow_check import (
    PassingWorkflowCheck as PassingWorkflowCheck,
)


def test_worker_manager_shutdown_preserves_workspace_when_worker_stays_alive(
    tmp_path: Path,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    manager = AgentWorkerManager(service, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/stuck-worker",
        tmp_path / "stuck-worker-worktree",
    )
    output = Path(lease.worktree_path) / "worker-output.txt"
    output.write_text("keep this output\n", encoding="utf-8")

    class StuckThread:
        def __init__(self) -> None:
            self.joins: list[float | None] = []

        def join(self, timeout=None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    thread = StuckThread()
    manager._threads[run.run_id] = thread  # type: ignore[assignment]
    manager._workspace_leases[run.run_id] = lease
    try:
        with pytest.raises(StoreError, match="did not stop before shutdown"):
            manager.shutdown()
        assert thread.joins == [5, 1]
        still_active = store.get_run(run.run_id)
        assert still_active is not None
        assert still_active.status is RunStatus.ACTIVE
        current_lease = workflow_service.workspace_for_run(run.run_id)
        assert current_lease is not None
        assert current_lease.status is LeaseStatus.ACTIVE
        assert output.read_text(encoding="utf-8") == "keep this output\n"
    finally:
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            workflow_service.discard_workspace(remaining.lease_id, "test cleanup")
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_manager_persists_model_failure(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(
            AttemptOutcome.FAILURE,
            failure_context="token=worker-secret",
        ),
        repository,
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 3
        current = store.get_run(run.run_id)
        while (
            current is not None
            and current.status is RunStatus.ACTIVE
            or run.run_id in manager._threads
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not finish")
            time.sleep(0.01)
            current = store.get_run(run.run_id)
        assert current is not None
        assert current.status is RunStatus.FAILED
        assert "token=[redacted]" in (current.last_error or "")
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        manager.shutdown()
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_fails_closed_when_workspace_heartbeat_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_store = WorkflowStore(tmp_path / "workflow.db", lease_ttl_seconds=1)
    workflow_service = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        [PassingWorkflowCheck()],
    )
    renewal_attempted = Event()

    def fail_renewal(_lease_id: str, _lease_token: str | None) -> WorkspaceLease:
        renewal_attempted.set()
        raise sqlite3.OperationalError("database is locked")

    def wait_for_renewal(_run_id: str, _lease_token: str):
        assert renewal_attempted.wait(3)
        return SimpleNamespace(
            state=SimpleNamespace(status=RoutingStatus.RESOLVED, required_action=None),
            attempt=SimpleNamespace(outcome=AttemptOutcome.FAILURE, failure_context=""),
            decision=SimpleNamespace(failure_context=""),
            execution_result=None,
        )

    monkeypatch.setattr(workflow_store, "renew_lease", fail_renewal)
    monkeypatch.setattr(orchestrator, "run_implementation_attempt", wait_for_renewal)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        while run.run_id in manager._threads:
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not stop after lease loss")
            time.sleep(0.01)

        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        assert "workspace lease was lost" in (failed.last_error or "")
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()
