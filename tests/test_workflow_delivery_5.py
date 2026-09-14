from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitDeliveryStatus,
)
from tests.support.workflow.helpers import git_command, make_delivery_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_delivery_blocks_when_git_state_probes_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, repository, remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "probe-failure-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:probe-failure", "codex/probe-failure", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")
    original_run_git = service.worktrees.run_git

    def failed_status(*arguments: str) -> subprocess.CompletedProcess[str]:
        if "--untracked-files=all" in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        return original_run_git(*arguments)

    monkeypatch.setattr(service.worktrees, "run_git", failed_status)
    status_result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:probe-failure",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert status_result.status is GitDeliveryStatus.BLOCKED
    assert "status could not be verified" in status_result.evidence

    def missing_head(*arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments[-2:] == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(arguments, 1, "", "")
        return original_run_git(*arguments)

    monkeypatch.setattr(service.worktrees, "run_git", missing_head)
    head_result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:probe-failure",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert head_result.status is GitDeliveryStatus.BLOCKED
    assert "HEAD could not be verified" in head_result.evidence

    def unavailable_prefix(*arguments: str) -> subprocess.CompletedProcess[str]:
        if "--show-prefix" in arguments:
            raise OSError("private Git failure")
        return original_run_git(*arguments)

    monkeypatch.setattr(service.worktrees, "run_git", unavailable_prefix)
    prefix_result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:probe-failure",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert prefix_result.status is GitDeliveryStatus.BLOCKED
    assert "private Git failure" not in prefix_result.evidence

    def missing_common_dir(*arguments: str) -> subprocess.CompletedProcess[str]:
        if "--git-common-dir" in arguments and "-C" in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        return original_run_git(*arguments)

    monkeypatch.setattr(service.worktrees, "run_git", missing_common_dir)
    common_result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:probe-failure",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert common_result.status is GitDeliveryStatus.BLOCKED
    assert "repository could not be verified" in common_result.evidence

    monkeypatch.setattr(service.worktrees, "run_git", original_run_git)
    git_command(repository, "remote", "remove", "origin")
    no_remote = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:probe-failure",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert no_remote.status is GitDeliveryStatus.BLOCKED
    assert "configured push remote" in no_remote.evidence
    git_command(repository, "remote", "add", "origin", str(remote))
    assert git_command(worktree, "status", "--porcelain") == original_status
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_rejects_stored_path_and_detached_head_before_staging(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "fenced-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:fenced-run", "codex/fenced", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")

    def set_lease_path(path: Path) -> None:
        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_leases SET worktree_path = ? WHERE lease_id = ?",
                (str(path.resolve()), lease.lease_id),
            )

    nested = worktree / "nested"
    nested.mkdir()
    set_lease_path(nested)
    wrong_path = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:fenced-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert wrong_path.status is GitDeliveryStatus.BLOCKED
    assert "exact leased worktree" in wrong_path.evidence
    assert git_command(worktree, "status", "--porcelain") == original_status

    set_lease_path(repository)
    repository_path = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:fenced-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert repository_path.status is GitDeliveryStatus.BLOCKED
    assert "not isolated" in repository_path.evidence

    not_a_repository = tmp_path / "not-a-repository"
    not_a_repository.mkdir()
    set_lease_path(not_a_repository)
    invalid_repository = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:fenced-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert invalid_repository.status is GitDeliveryStatus.BLOCKED
    assert "could not be opened" in invalid_repository.evidence
    set_lease_path(worktree)

    git_command(worktree, "switch", "--detach", "HEAD")
    detached = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:fenced-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert detached.status is GitDeliveryStatus.BLOCKED
    assert "Detached HEAD" in detached.evidence
    assert git_command(worktree, "status", "--porcelain") == original_status
    git_command(worktree, "switch", lease.branch)
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()
