import sys
import time
from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
    CodexExecModelExecutor,
)
from beehaiive.models import (
    RunStatus,
)
from beehaiive.workflow import (
    LeaseStatus,
    WorkflowError,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for


def test_dashboard_worker_shutdown_kills_child_and_preserves_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    script = tmp_path / "slow_writer.py"
    script.write_text(
        "import pathlib, time\n"
        "pathlib.Path('worker-output.txt').write_text('started\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    class SlowWorkspaceExecutor(CodexExecModelExecutor):
        def _workspace_command_for_execution(
            self, prompt: str, model: str, worktree: Path
        ) -> list[str]:
            return [sys.executable, str(script), "--cd", str(worktree), prompt]

    executor = SlowWorkspaceExecutor(
        repository, executable=sys.executable, repository_name="owner/api"
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        lease = workflow_service.workspace_for_run(run.run_id)
        while (
            lease is not None
            and not Path(lease.worktree_path, "worker-output.txt").exists()
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("The child did not write inside its worktree")
            time.sleep(0.01)
            lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None

        def fail_clean(_worktree: Path) -> bool:
            raise WorkflowError("Git status unavailable")

        def fail_retain(_lease_id: str, _lease_token: str | None) -> None:
            raise WorkflowError("Lease changed")

        monkeypatch.setattr(workflow_service.worktrees, "clean", fail_clean)
        monkeypatch.setattr(workflow_service, "retain_workspace", fail_retain)

        manager.shutdown()

        stopped = state_store.get_run(run.run_id)
        assert stopped is not None and stopped.status is RunStatus.FAILED
        preserved = workflow_service.workspace_for_run(run.run_id)
        assert preserved is not None and preserved.status is LeaseStatus.STOPPED
        assert (
            Path(lease.worktree_path, "worker-output.txt").read_text(encoding="utf-8")
            == "started\n"
        )
        assert not executor._processes
        workflow_service.discard_workspace(lease.lease_id, "test cleanup")
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()
