from pathlib import Path
from types import SimpleNamespace

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.models import (
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    RoutingStatus,
)
from beehaiive.workflow import (
    GitDeliveryResult,
    GitDeliveryStatus,
    LeaseStatus,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_manager_reopens_routing_when_handoff_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    run = RunState(
        "persist-run",
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

    class PersistenceStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            self.failure = (run_id, error, lease_token)

    class PersistenceOrchestrator:
        def __init__(self) -> None:
            self.store = PersistenceStore()
            self.recovery: tuple[str, str] | None = None

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token

        def run_implementation_attempt(self, run_id: str, lease_token: str):
            del run_id, lease_token
            (worktree / "worker-output.txt").write_text(
                "successful work\n", encoding="utf-8"
            )
            return SimpleNamespace(
                state=SimpleNamespace(
                    status=RoutingStatus.RESOLVED,
                    required_action=None,
                ),
                attempt=SimpleNamespace(outcome=AttemptOutcome.SUCCESS),
                decision=SimpleNamespace(failure_context=""),
                execution_result="completed result",
                task_result=TaskResult(TaskOutcome.PASS, {}),
            )

        def handoff(self, *args, **kwargs) -> None:
            del args, kwargs
            raise RuntimeError("database unavailable")

        def recover_routing_problem(self, run_id: str, reason: str) -> None:
            self.recovery = (run_id, reason)

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    orchestrator = PersistenceOrchestrator()
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/persist-run",
        tmp_path / "persist-run-worktree",
    )
    worktree = Path(lease.worktree_path)
    manager._workspace_leases[run.run_id] = lease
    monkeypatch.setattr(
        manager,
        "commit_and_push",
        lambda _run_id: GitDeliveryResult(
            GitDeliveryStatus.PUSHED,
            lease.lease_id,
            lease.branch,
            "a" * 40,
            "push verified",
        ),
    )

    try:
        manager._run(run.run_id, run.lease_token or "")

        assert orchestrator.recovery is not None
        assert orchestrator.recovery[0] == run.run_id
        assert "database unavailable" in orchestrator.recovery[1]
        assert orchestrator.store.failure == (
            run.run_id,
            "Agent worker failed: database unavailable",
            "lease-1",
        )
        retained = workflow_service.workspace_for_run(run.run_id)
        assert retained is not None
        assert retained.status is LeaseStatus.RETAINED
        assert (worktree / "worker-output.txt").read_text(encoding="utf-8") == (
            "successful work\n"
        )
    finally:
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            workflow_service.discard_workspace(remaining.lease_id, "test cleanup")
        workflow_store.close()
