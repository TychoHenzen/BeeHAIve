from pathlib import Path
from types import SimpleNamespace

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.models import (
    RunStatus,
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    LeaseStatus,
    WorkflowError,
)


def test_worker_manager_commit_and_push_rejects_invalid_run_context() -> None:
    repository = Path.cwd().resolve()

    class FakeStore:
        def __init__(self, run) -> None:
            self.run = run

        def get_run(self, run_id: str):
            if self.run is None or self.run.run_id != run_id:
                return None
            return self.run

    class FakeWorkflowService:
        def __init__(self, lease) -> None:
            self.lease = lease
            self.worktrees = SimpleNamespace(repository=repository)

        def cleanup_dashboard_run_workspaces(self):
            return ()

        def workspace_for_run(self, _run_id: str):
            return self.lease

        def commit_and_push(self, *_arguments):
            raise AssertionError("Git delivery must not run for invalid state")

    def run_state(
        status=RunStatus.ACTIVE,
        lease_token="run-token",
        lease_expires_at="2099-01-01T00:00:00+00:00",
    ):
        return SimpleNamespace(
            run_id="run-40",
            project_id="project-1",
            repository="owner/api",
            pbi_number=40,
            title="Commit and push",
            status=status,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    def lease_state(status=LeaseStatus.ACTIVE):
        return SimpleNamespace(
            lease_id="lease-40",
            lease_token="workspace-token",
            status=status,
        )

    cases = (
        (run_state(), lease_state(), False, repository, "Workflow service is required"),
        (None, lease_state(), True, repository, "Unknown run"),
        (
            run_state("cancelled"),
            lease_state(),
            True,
            repository,
            "not eligible for Git delivery",
        ),
        (
            run_state(lease_token=None),
            lease_state(),
            True,
            repository,
            "active run lease",
        ),
        (
            run_state(lease_expires_at="2000-01-01T00:00:00+00:00"),
            lease_state(),
            True,
            repository,
            "active run lease",
        ),
        (run_state(), None, True, repository, "leased dashboard worktree"),
        (
            run_state(),
            lease_state(LeaseStatus.STOPPED),
            True,
            repository,
            "active workspace lease",
        ),
        (
            run_state(RunStatus.FAILED),
            lease_state(),
            True,
            repository,
            "retained worktree",
        ),
        (
            run_state(),
            lease_state(),
            True,
            repository / "other",
            "same repository",
        ),
    )
    for run, lease, use_service, executor_repository, error in cases:
        store = FakeStore(run)
        service = FakeWorkflowService(lease) if use_service else None
        manager = AgentWorkerManager(
            SimpleNamespace(store=store),
            SimpleNamespace(repository=executor_repository),
            service,
        )
        with pytest.raises(StoreError, match=error):
            manager.commit_and_push("run-40")


def test_worker_manager_revalidates_run_before_git_delivery() -> None:
    repository = Path.cwd().resolve()
    run = SimpleNamespace(
        run_id="run-40",
        project_id="project-1",
        repository="owner/api",
        pbi_number=40,
        title="Commit and push",
        status=RunStatus.ACTIVE,
        lease_token="run-token",
        lease_expires_at="2099-01-01T00:00:00+00:00",
    )

    class FakeStore:
        current = run

        def get_run(self, _run_id: str):
            return self.current

    store = FakeStore()

    class FakeWorkflowService:
        worktrees = SimpleNamespace(repository=repository)

        def cleanup_dashboard_run_workspaces(self):
            return ()

        def workspace_for_run(self, _run_id: str):
            return SimpleNamespace(
                lease_id="lease-40",
                lease_token="workspace-token",
                status=LeaseStatus.ACTIVE,
            )

        def commit_and_push(self, *_arguments):
            store.current = SimpleNamespace(**(vars(run) | {"pbi_number": 41}))
            _arguments[-1]()

    manager = AgentWorkerManager(
        SimpleNamespace(store=store),
        SimpleNamespace(repository=repository),
        FakeWorkflowService(),
    )
    with pytest.raises(WorkflowError, match="Dashboard run lease changed"):
        manager.commit_and_push(run.run_id)
