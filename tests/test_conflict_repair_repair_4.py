from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    Constitution,
    GitWorktreeManager,
    WorkflowError,
    WorkflowService,
    WorkflowStore,
)
from tests.support.conflict_repair.fixture_check import FixtureCheck as FixtureCheck
from tests.support.conflict_repair.helpers import (
    git_command,
    make_conflict_repository,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_worktree_manager_proves_exact_refs_and_merge_results(tmp_path: Path) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    remote = tmp_path / "remote.git"
    git_command(tmp_path, "init", "--bare", str(remote))
    git_command(repository, "remote", "add", "origin", str(remote))
    git_command(repository, "push", "origin", "--all")
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    manager = GitWorktreeManager(repository, store)
    source_ref = "refs/beehaiive/tests/source"
    try:
        assert (
            manager.fetch_exact_branch("feature", source_head, source_ref) == source_ref
        )
        with pytest.raises(WorkflowError, match="changed"):
            manager.fetch_exact_branch("feature", "different-head", source_ref)
        with pytest.raises(WorkflowError, match="remote ref"):
            manager.fetch_exact_branch("missing", source_head, source_ref)
        manager.remove_ref(source_ref)
        manager.remove_ref("")

        git_command(repository, "remote", "remove", "origin")
        with pytest.raises(WorkflowError, match="not available"):
            manager.fetch_exact_branch("feature", "missing-local-head", source_ref)
        assert (
            manager.fetch_exact_branch("feature", source_head, source_ref) == source_ref
        )

        worktree = tmp_path / "manager-worktree"
        lease = manager.acquire(
            "manager-test", "codex/manager-test", worktree, source_head
        )
        assert (
            manager.integrate_target(worktree, source_head, source_head).conflicted
            is False
        )
        assert manager.contains_commit(worktree, source_head)
        assert not manager.contains_commit(worktree, target_head)
        with pytest.raises(WorkflowError, match="expected head"):
            manager.integrate_target(worktree, "wrong-head", source_head)
        with pytest.raises(WorkflowError, match="missing-target"):
            manager.integrate_target(worktree, source_head, "missing-target")
        manager.release(lease.lease_id)

        workflow = WorkflowService(
            store,
            repository,
            Constitution.load(CONSTITUTION_PATH),
            [FixtureCheck()],
            worktrees=manager,
        )
        with pytest.raises(WorkflowError, match="Unknown workspace lease"):
            workflow.discard_workspace("missing-lease", "test")
    finally:
        manager.remove_ref(source_ref)
        store.close()


def test_worktree_manager_fails_closed_when_remote_inventory_fails(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "not-a-repository"
    repository.mkdir()
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    manager = GitWorktreeManager(repository, store)
    try:
        with pytest.raises(WorkflowError, match="not a git repository"):
            manager.fetch_exact_branch(
                "feature", "head", "refs/beehaiive/tests/missing"
            )
        with pytest.raises(WorkflowError, match="not a git repository"):
            manager.contains_commit(repository, "head")
    finally:
        store.close()
