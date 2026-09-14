from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitDeliveryStatus,
    LeaseStatus,
    WorkflowError,
)
from tests.support.workflow.helpers import (
    git_command,
    make_delivery_service,
    make_repository,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_delivery_rejects_a_worktree_specific_push_url_before_staging(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "worktree-pushurl"
    lease = service.acquire_workspace(
        "dashboard-run:worktree-pushurl", "codex/worktree-pushurl", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")
    unapproved_remote = tmp_path / "unapproved" / "owner" / "api.git"
    unapproved_remote.parent.mkdir(parents=True)
    subprocess.run(
        ("git", "init", "--bare", str(unapproved_remote)),
        check=True,
        capture_output=True,
    )
    git_command(repository, "config", "extensions.worktreeConfig", "true")
    git_command(
        worktree,
        "config",
        "--worktree",
        "remote.origin.pushurl",
        str(unapproved_remote),
    )
    assert git_command(
        worktree, "remote", "get-url", "--push", "--all", "origin"
    ) == str(unapproved_remote)

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:worktree-pushurl",
        "owner/api",
        "must not stage",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.BLOCKED
    assert "configured push remote" in result.evidence
    assert git_command(worktree, "status", "--porcelain") == original_status
    assert (
        service.worktrees.run_git(
            "ls-remote",
            "--exit-code",
            str(unapproved_remote),
            "refs/heads/codex/worktree-pushurl",
        ).returncode
        != 0
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_rejects_a_worktree_path_outside_its_repository(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "foreign-path-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:foreign-path", "codex/foreign-path", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_repository = make_repository(foreign_root)
    foreign_worktree = tmp_path / "foreign-path"
    git_command(
        foreign_repository,
        "worktree",
        "add",
        "--detach",
        str(foreign_worktree),
        "master",
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE workflow_leases SET worktree_path = ? WHERE lease_id = ?",
            (str(foreign_worktree.resolve()), lease.lease_id),
        )

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:foreign-path",
        "owner/api",
        "must not stage",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.BLOCKED
    assert "different repository" in result.evidence
    assert git_command(worktree, "status", "--porcelain") == original_status
    store.close()


def test_delivery_checks_run_and_workspace_tokens_before_git_mutations(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "fenced-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:fenced-run", "codex/fenced", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")

    def reject_run() -> None:
        raise WorkflowError("Dashboard run lease changed")

    with pytest.raises(WorkflowError, match="Lease token is invalid"):
        service.commit_and_push(
            lease.lease_id,
            "wrong-token",
            "dashboard-run:fenced-run",
            "owner/api",
            "fenced commit",
            lambda: None,
        )
    with pytest.raises(WorkflowError, match="run lease changed"):
        service.commit_and_push(
            lease.lease_id,
            lease.lease_token,
            "dashboard-run:fenced-run",
            "owner/api",
            "fenced commit",
            reject_run,
        )
    invalid_message = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:fenced-run",
        "owner/api",
        "\x00",
        lambda: None,
    )
    assert invalid_message.status is GitDeliveryStatus.BLOCKED

    assert git_command(worktree, "status", "--porcelain") == original_status
    store.close()


def test_expired_delivery_lease_keeps_the_uncommitted_worktree(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "expired-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:expired-run", "codex/expired", worktree
    )
    change = worktree / "change.txt"
    change.write_text("preserve after expiry\n", encoding="utf-8")
    with store._transaction() as connection:
        connection.execute(
            "UPDATE workflow_leases SET expires_at = ? WHERE lease_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), lease.lease_id),
        )

    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        service.commit_and_push(
            lease.lease_id,
            lease.lease_token,
            "dashboard-run:expired-run",
            "owner/api",
            "expired commit",
            lambda: None,
        )

    assert change.read_text(encoding="utf-8") == "preserve after expiry\n"
    expired = store.get_lease(lease.lease_id)
    assert expired is not None and expired.status is LeaseStatus.STOPPED
    store.close()
