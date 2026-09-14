from pathlib import Path
from types import SimpleNamespace

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
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


def test_workspace_validator_rejects_another_run_lease(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        "dashboard-run:another-run",
        "codex/another-run",
        tmp_path / "another-run-worktree",
    )

    try:
        validate = manager._workspace_validator(run.run_id, lease)
        with pytest.raises(WorkflowError, match="lease changed"):
            validate()
    finally:
        workflow_service.discard_workspace(lease.lease_id, "test complete")
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_rolls_back_when_thread_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class FailingThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(
        "beehaiive.agent_parts.worker_capacity_mixin.Thread", FailingThread
    )
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            manager.start(run)
        assert run.run_id not in manager._threads
        assert run.run_id not in executor._active_attempts
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_start_reports_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)

    def fail_repository_files() -> tuple[Path, ...]:
        raise RuntimeError("workspace validation failed")

    original_discard = workflow_service.discard_workspace

    def fail_cleanup(_lease_id: str, _reason: str):
        raise WorkflowError("workspace cleanup unavailable")

    monkeypatch.setattr(executor, "_repository_files", fail_repository_files)
    monkeypatch.setattr(workflow_service, "discard_workspace", fail_cleanup)
    try:
        with pytest.raises(StoreError, match="workspace cleanup failed"):
            manager.start(run)
    finally:
        monkeypatch.setattr(workflow_service, "discard_workspace", original_discard)
        lease = workflow_service.workspace_for_run(run.run_id)
        if lease is not None and lease.status is not LeaseStatus.RELEASED:
            original_discard(lease.lease_id, "test cleanup")
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_shutdown_attempts_all_cancellations_after_failure() -> None:
    class FailingExecutor:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel(self, run_id: str) -> None:
            self.cancelled.append(run_id)
            if run_id == "run-1":
                raise RuntimeError("taskkill failed")

    class FakeThread:
        def __init__(self) -> None:
            self.joins: list[float | None] = []

        def join(self, timeout=None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return False

    executor = FailingExecutor()
    manager = AgentWorkerManager(SimpleNamespace(), executor)
    threads = {run_id: FakeThread() for run_id in ("run-1", "run-2")}
    manager._threads.update(threads)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="taskkill failed"):
        manager.shutdown()

    assert executor.cancelled == ["run-1", "run-2"]
    assert all(thread.joins == [5] for thread in threads.values())
