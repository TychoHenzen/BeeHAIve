from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitWorktreeManager,
    LeaseStatus,
    WorkflowError,
    WorkflowStore,
)
from tests.support.workflow.helpers import git_command, make_repository, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_workflow_store_migrates_legacy_lease_liveness_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-leases.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE workflow_leases(
            lease_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            branch TEXT NOT NULL, worktree_path TEXT NOT NULL,
            status TEXT NOT NULL, stop_reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_leases(
            lease_id, agent_id, branch, worktree_path, status,
            stop_reason, created_at, updated_at
        ) VALUES ('legacy', 'agent', 'branch', 'path', 'active', NULL, 'now', 'now')
        """
    )
    connection.commit()
    connection.close()

    store = WorkflowStore(database)

    try:
        lease = store.get_lease("legacy")
        assert lease is not None
        assert lease.lease_token
        assert lease.expires_at
    finally:
        store.close()


def test_worktree_path_identity_preserves_internal_whitespace(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "worktree  with  spaces"

    lease = manager.acquire("agent", "codex/spaces", worktree)

    assert lease.worktree_path == str(worktree.resolve())
    released = manager.release(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED
    store.close()


def test_worktree_gitdir_uses_the_host_path(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "host-path-worktree"

    lease = manager.acquire("agent", "codex/host-path", worktree)

    marker = (worktree / ".git").read_text(encoding="utf-8").strip()
    expected = (repository / ".git" / "worktrees" / worktree.name).resolve()
    assert marker == f"gitdir: {expected.as_posix()}"
    assert manager.clean(worktree)
    manager.release(lease.lease_id)
    store.close()


def test_successful_run_workspace_is_retained_until_explicit_release(
    tmp_path: Path,
) -> None:
    service, store, _repository_path = make_service(tmp_path)
    worktree = tmp_path / "retained-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:run-retained", "codex/retained", worktree
    )
    (worktree / "change.txt").write_text("uncommitted\n", encoding="utf-8")

    retained = service.retain_workspace(lease.lease_id, lease.lease_token)

    assert retained.status is LeaseStatus.RETAINED
    assert retained.expires_at is None
    with store._transaction() as connection:
        connection.execute(
            "UPDATE workflow_leases SET expires_at = ? WHERE lease_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), lease.lease_id),
        )
    recovered = service.workspace_for_run("run-retained")
    assert recovered is not None
    assert recovered.status is LeaseStatus.RETAINED
    assert recovered.expires_at is not None
    assert (worktree / "change.txt").read_text(encoding="utf-8") == "uncommitted\n"

    with pytest.raises(WorkflowError, match="uncommitted"):
        service.release_workspace(lease.lease_id)
    with pytest.raises(WorkflowError, match="branch or worktree"):
        service.acquire_workspace("other-run", lease.branch, tmp_path / "other")

    git_command(worktree, "add", "change.txt")
    git_command(worktree, "commit", "-m", "consume retained worktree")
    released = service.release_workspace(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED
    assert not worktree.exists()
    store.close()


def test_restart_cleanup_removes_only_unfinished_dashboard_workspaces(
    tmp_path: Path,
) -> None:
    service, store, _repository_path = make_service(tmp_path)
    active_path = tmp_path / "active-worktree"
    retained_path = tmp_path / "retained-worktree"
    active = service.acquire_workspace(
        "dashboard-run:run-active", "codex/active-run", active_path
    )
    retained = service.acquire_workspace(
        "dashboard-run:run-success", "codex/success-run", retained_path
    )
    (retained_path / "change.txt").write_text("keep\n", encoding="utf-8")
    service.retain_workspace(retained.lease_id, retained.lease_token)

    cleaned = service.cleanup_dashboard_run_workspaces()

    assert len(cleaned) == 1
    assert cleaned[0].lease_id == active.lease_id
    assert cleaned[0].status is LeaseStatus.RELEASED
    assert not active_path.exists()
    still_retained = service.workspace_for_run("run-success")
    assert still_retained is not None
    assert still_retained.status is LeaseStatus.RETAINED
    assert (retained_path / "change.txt").read_text(encoding="utf-8") == "keep\n"
    store.close()


def test_restart_cleanup_preserves_dirty_and_uninspectable_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _repository_path = make_service(tmp_path)
    clean_path = tmp_path / "clean-worktree"
    dirty_path = tmp_path / "dirty-worktree"
    unknown_path = tmp_path / "unknown-worktree"
    clean_lease = service.acquire_workspace(
        "dashboard-run:run-clean", "codex/clean", clean_path
    )
    dirty_lease = service.acquire_workspace(
        "dashboard-run:run-dirty", "codex/dirty", dirty_path
    )
    unknown_lease = service.acquire_workspace(
        "dashboard-run:run-unknown", "codex/unknown", unknown_path
    )
    dirty_file = dirty_path / "keep.txt"
    dirty_file.write_text("preserve\n", encoding="utf-8")
    original_clean = service.worktrees.clean

    def clean_or_fail(path: Path) -> bool:
        if Path(path).resolve() == unknown_path.resolve():
            raise WorkflowError("Git status unavailable")
        return original_clean(path)

    monkeypatch.setattr(service.worktrees, "clean", clean_or_fail)
    cleaned = service.cleanup_dashboard_run_workspaces()
    results = {lease.lease_id: lease for lease in cleaned}

    assert results[clean_lease.lease_id].status is LeaseStatus.RELEASED
    assert not clean_path.exists()
    assert results[dirty_lease.lease_id].status is LeaseStatus.STOPPED
    assert dirty_file.read_text(encoding="utf-8") == "preserve\n"
    assert results[unknown_lease.lease_id].status is LeaseStatus.STOPPED
    assert unknown_path.is_dir()
    store.close()


def test_restart_cleanup_can_target_one_dashboard_run(tmp_path: Path) -> None:
    service, store, _repository_path = make_service(tmp_path)
    target = service.acquire_workspace(
        "dashboard-run:run-target",
        "codex/run-target",
        tmp_path / "target-worktree",
    )
    other = service.acquire_workspace(
        "dashboard-run:run-other",
        "codex/run-other",
        tmp_path / "other-worktree",
    )

    cleaned = service.cleanup_dashboard_run_workspaces("run-target")

    assert [lease.lease_id for lease in cleaned] == [target.lease_id]
    assert cleaned[0].status is LeaseStatus.RELEASED
    assert service.store.get_lease(other.lease_id) == other
    assert Path(other.worktree_path).is_dir()
    service.cleanup_dashboard_run_workspaces()
    store.close()
