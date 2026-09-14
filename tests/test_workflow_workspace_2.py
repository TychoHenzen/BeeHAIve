from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from beehaiive.workflow import (
    GitWorktreeManager,
    HandoffStatus,
    LeaseStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowStore,
    _lease_is_expired,
)
from tests.support.workflow.helpers import make_repository, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_retained_lease_validates_state_and_can_be_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _repository_path = make_service(tmp_path)
    lease = service.acquire_workspace(
        "dashboard-run:retained",
        "codex/retained",
        tmp_path / "retained-worktree",
    )

    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.retain_lease("missing-lease", "token")
    with pytest.raises(WorkflowError, match="Lease token is invalid"):
        store.retain_lease(lease.lease_id, "wrong-token")
    retained = service.retain_workspace(lease.lease_id, lease.lease_token)
    with pytest.raises(WorkflowError, match="Workspace lease is retained"):
        store.retain_lease(lease.lease_id, lease.lease_token)
    with pytest.raises(WorkflowError, match="cannot be renewed"):
        store._set_lease_status(lease.lease_id, LeaseStatus.ACTIVE, None)

    discarded = service.discard_workspace(lease.lease_id, "discard retained workspace")
    assert retained.status is LeaseStatus.RETAINED
    assert discarded.status is LeaseStatus.RELEASED
    assert not Path(lease.worktree_path).exists()

    lost = service.acquire_workspace(
        "dashboard-run:lost",
        "codex/lost",
        tmp_path / "lost-worktree",
    )
    original_get_lease = store.get_lease
    monkeypatch.setattr(store, "get_lease", lambda _lease_id: None)
    with pytest.raises(WorkflowError, match="Workspace lease disappeared"):
        store.retain_lease(lost.lease_id, lost.lease_token)
    monkeypatch.setattr(store, "get_lease", original_get_lease)
    lost_retained = store.get_lease(lost.lease_id)
    assert lost_retained is not None
    assert lost_retained.status is LeaseStatus.RETAINED
    service.discard_workspace(lost.lease_id, "discard test workspace")
    store.close()


def test_worktree_leases_prevent_concurrent_duplicate_writes(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    barrier = Barrier(2)

    def acquire(index: int) -> object:
        barrier.wait()
        try:
            return manager.acquire(
                f"agent-{index}",
                "codex/shared",
                tmp_path / f"worktree-{index}",
            )
        except WorkflowError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, (1, 2)))
    leases = [result for result in results if not isinstance(result, WorkflowError)]
    errors = [result for result in results if isinstance(result, WorkflowError)]
    assert len(leases) == 1
    assert len(errors) == 1
    lease = leases[0]
    assert lease.status is LeaseStatus.ACTIVE
    assert manager.clean(lease.worktree_path) is True
    released = manager.release(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED

    bad_base_path = tmp_path / "bad-base"
    with pytest.raises(WorkflowError):
        manager.acquire("agent-bad", "codex/bad", bad_base_path, "missing-ref")
    bad_lease = store.acquire_lease("agent-bad", "codex/bad", str(bad_base_path))
    assert store.release_lease(bad_lease.lease_id).status is LeaseStatus.RELEASED

    with pytest.raises(WorkflowError, match="Unknown workspace"):
        manager.release("missing")
    with pytest.raises(WorkflowError):
        manager.head(tmp_path / "missing-worktree")
    with pytest.raises(WorkflowError):
        manager.clean(tmp_path / "missing-worktree")
    store.close()


def test_expired_leases_are_fenced_renewed_and_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WorkflowError, match="TTL"):
        WorkflowStore(lease_ttl_seconds=0)
    assert _lease_is_expired(None) is True
    assert _lease_is_expired("not-a-timestamp") is True
    repository = make_repository(tmp_path)
    store = WorkflowStore(tmp_path / "leases.sqlite3")
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "expired-worktree"
    lease = manager.acquire("agent", "codex/expired", worktree)

    with pytest.raises(WorkflowError, match="invalid"):
        store.renew_lease(lease.lease_id, "wrong-token")
    renewed = store.renew_lease(lease.lease_id, lease.lease_token)
    assert renewed.expires_at != lease.expires_at
    with store._transaction() as connection:
        connection.execute(
            "UPDATE workflow_leases SET expires_at = ? WHERE lease_id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                lease.lease_id,
            ),
        )

    replacement = manager.acquire("replacement", "codex/expired", worktree)

    assert store.get_lease(lease.lease_id).status is LeaseStatus.STOPPED  # type: ignore[union-attr]
    assert replacement.status is LeaseStatus.ACTIVE
    assert replacement.lease_token != lease.lease_token
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.require_lease_token("missing", None)
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.require_lease_token(lease.lease_id, lease.lease_token)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.ensure_release_allowed("missing")
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.ensure_release_allowed(lease.lease_id)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.renew_lease("missing", None)
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.renew_lease(lease.lease_id, lease.lease_token)
    disposable = store.acquire_lease("disposable", "codex/disposable", "path")
    original_get_lease = store.get_lease
    monkeypatch.setattr(store, "get_lease", lambda _lease_id: None)
    with pytest.raises(WorkflowError, match="disappeared"):
        store.ensure_release_allowed(disposable.lease_id)
    with pytest.raises(WorkflowError, match="disappeared"):
        store.renew_lease(disposable.lease_id, disposable.lease_token)
    monkeypatch.setattr(store, "get_lease", original_get_lease)
    released = manager.release(replacement.lease_id)
    assert released.status is LeaseStatus.RELEASED
    store.close()


def test_stopped_lease_cleanup_is_idempotent(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    store = WorkflowStore(tmp_path / "stopped.sqlite3")
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "stopped-worktree"
    lease = manager.acquire("agent", "codex/stopped", worktree)

    stopped = store.stop_lease(lease.lease_id, "operator stop")
    assert stopped.status is LeaseStatus.STOPPED
    assert store.release_lease(lease.lease_id).status is LeaseStatus.STOPPED
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.record_handoff(
            lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    released = manager.release(lease.lease_id)
    repeated = manager.release(lease.lease_id)
    assert store.release_lease(lease.lease_id).status is LeaseStatus.RELEASED

    assert released.status is LeaseStatus.RELEASED
    assert repeated.status is LeaseStatus.RELEASED
    assert not worktree.exists()
    manager._delete_reclaimed_branch("codex/missing")
    with pytest.raises(WorkflowError, match="checked-out branch"):
        manager._delete_reclaimed_branch("master")
    store.close()


def test_git_timeout_is_reported_and_failed_acquisition_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(tmp_path)
    store = WorkflowStore()
    with pytest.raises(WorkflowError, match="Git timeout"):
        GitWorktreeManager(repository, store, git_timeout_seconds=0)
    manager = GitWorktreeManager(repository, store, git_timeout_seconds=0.25)

    def timeout(*arguments: object, **kwargs: object) -> object:
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired("git", 0.25)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(WorkflowError, match="timed out"):
        manager.head(tmp_path / "missing")
    with pytest.raises(WorkflowError, match="timed out"):
        manager.acquire("agent", "codex/timeout", tmp_path / "timeout-worktree")
    leases = store.get_lease("missing")
    assert leases is None
    store.close()
