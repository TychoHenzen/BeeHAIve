from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    GitDeliveryStatus,
)
from tests.support.workflow.helpers import git_command, make_delivery_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_delivery_uses_the_configured_push_url_for_push_and_verification(
    tmp_path: Path,
) -> None:
    push_remote = tmp_path / "push" / "owner" / "api.git"
    service, store, _repository_path, fetch_remote = make_delivery_service(
        tmp_path, push_remote=push_remote
    )
    worktree = tmp_path / "pushurl-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:pushurl-run", "codex/pushurl", worktree
    )
    (worktree / "change.txt").write_text("pushurl change\n", encoding="utf-8")

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:pushurl-run",
        "owner/api",
        "deliver to configured push URL",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.PUSHED
    assert (
        result.commit_sha
        == git_command(
            worktree,
            "ls-remote",
            "--exit-code",
            str(push_remote),
            "refs/heads/codex/pushurl",
        ).split()[0]
    )
    assert (
        service.worktrees.run_git(
            "ls-remote", "--exit-code", str(fetch_remote), "refs/heads/codex/pushurl"
        ).returncode
        != 0
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_clean_worktree_is_a_noop_without_creating_remote_branch(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
    lease = service.acquire_workspace(
        "dashboard-run:noop-run", "codex/noop", tmp_path / "noop-worktree"
    )

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:noop-run",
        "owner/api",
        "unused message",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.NO_CHANGES
    assert result.commit_sha is None
    assert git_command(repository, "ls-remote", "origin", "refs/heads/codex/noop") == ""
    repeated = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:noop-run",
        "owner/api",
        "unused message",
        lambda: None,
    )
    assert repeated.status is GitDeliveryStatus.NO_CHANGES
    service.release_workspace(lease.lease_id)
    store.close()


def test_delivery_retry_pushes_the_same_saved_commit_after_remote_recovers(
    tmp_path: Path,
) -> None:
    service, store, repository, remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "retry-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:retry-run", "codex/retry", worktree
    )
    (worktree / "change.txt").write_text("preserve on failure\n", encoding="utf-8")
    unavailable = remote.with_name("api.offline")
    remote.rename(unavailable)

    blocked = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:retry-run",
        "owner/api",
        "save once",
        lambda: None,
    )

    assert blocked.status is GitDeliveryStatus.BLOCKED
    assert blocked.commit_sha == service.worktrees.head(worktree)
    assert service.worktrees.clean(worktree)
    assert "offline" not in blocked.evidence
    blocked_gate = store.latest_gate(lease.lease_id, "git_delivery")
    assert blocked_gate is not None
    assert all(str(remote) not in check.evidence for check in blocked_gate.checks)

    later_change = worktree / "later.txt"
    later_change.write_text("not part of the saved commit\n", encoding="utf-8")
    mismatch = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:retry-run",
        "owner/api",
        "must not replace saved commit",
        lambda: None,
    )
    assert mismatch.status is GitDeliveryStatus.BLOCKED
    assert mismatch.commit_sha == blocked.commit_sha
    assert later_change.is_file()
    later_change.unlink()

    unavailable.rename(remote)
    retried = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:retry-run",
        "owner/api",
        "ignored on retry",
        lambda: None,
    )

    assert retried.status is GitDeliveryStatus.PUSHED
    assert retried.commit_sha == blocked.commit_sha
    assert (
        git_command(
            repository,
            "ls-remote",
            "--exit-code",
            "origin",
            "refs/heads/codex/retry",
        ).split()[0]
        == blocked.commit_sha
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()
