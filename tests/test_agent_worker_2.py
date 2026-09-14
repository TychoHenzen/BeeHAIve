import time
from pathlib import Path
from threading import Event, Thread

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
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    CheckResult,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_blocks_model_execution_on_required_gate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="must not execute"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class FailingSuite:
        def run(self, workspace: Path) -> tuple[CheckResult, ...]:
            assert workspace.is_dir()
            return (
                CheckResult(
                    "coverage",
                    False,
                    "Coverage is below 90 percent",
                    status="failed",
                    category="tests",
                ),
            )

    attempted = Event()

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        attempted.set()
        raise AssertionError("The model ran after a required gate failed")

    monkeypatch.setattr(workflow_service, "checks", FailingSuite())
    monkeypatch.setattr(orchestrator, "run_implementation_attempt", fail_if_started)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        while run.run_id in manager._threads:
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not stop after the gate failure")
            time.sleep(0.01)

        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        assert '"quality_gate"' in (failed.last_error or "")
        assert "coverage" in (failed.last_error or "")
        assert not attempted.is_set()
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None
        gate = workflow_store.latest_gate(lease.lease_id, "model_call")
        assert gate is not None and gate.allowed is False
        assert gate.checks[0].status == "failed"
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_manager_enforces_shared_concurrency_limit(
    tmp_path: Path,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        assert manager.active_worker_count == 0
        assert manager.has_capacity()
        manager.set_max_concurrent_workers(1)
        manager._threads["occupied"] = Thread(target=lambda: None)
        assert manager.active_worker_count == 1
        assert not manager.has_capacity()
        with pytest.raises(StoreError, match="Maximum concurrent"):
            manager.start(run)
        for invalid_maximum in (True, 0, 1.5):
            with pytest.raises(StoreError, match="positive integer"):
                manager.set_max_concurrent_workers(invalid_maximum)
    finally:
        manager._threads.clear()
        workflow_store.close()
        store.close()
        routing_store.close()
