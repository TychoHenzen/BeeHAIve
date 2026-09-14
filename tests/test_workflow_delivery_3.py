from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitDeliveryStatus,
)
from tests.support.workflow.helpers import git_command, make_delivery_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


@pytest.mark.parametrize(
    ("failure_point", "expected_status", "expected_evidence"),
    [
        (
            "wrong_branch_before_staging",
            GitDeliveryStatus.BLOCKED,
            "current branch does not match",
        ),
        ("stage_failure", GitDeliveryStatus.BLOCKED, "could not be staged"),
        (
            "nothing_to_stage",
            GitDeliveryStatus.NO_CHANGES,
            "no committable changes",
        ),
        (
            "diff_failure",
            GitDeliveryStatus.BLOCKED,
            "Staged changes could not be verified",
        ),
        (
            "wrong_branch_after_staging",
            GitDeliveryStatus.BLOCKED,
            "current branch does not match",
        ),
        (
            "commit_failure",
            GitDeliveryStatus.BLOCKED,
            "could not create the commit",
        ),
        (
            "missing_commit_sha",
            GitDeliveryStatus.BLOCKED,
            "new commit could not be verified",
        ),
        (
            "dirty_after_commit",
            GitDeliveryStatus.BLOCKED,
            "worktree is not clean",
        ),
        (
            "remote_mismatch_before_push",
            GitDeliveryStatus.BLOCKED,
            "configured push remote",
        ),
        (
            "dirty_before_push",
            GitDeliveryStatus.BLOCKED,
            "Push requires the recorded commit",
        ),
        (
            "remote_mismatch_after_push",
            GitDeliveryStatus.BLOCKED,
            "configured push remote",
        ),
        (
            "unverifiable_remote_push",
            GitDeliveryStatus.BLOCKED,
            "Push could not be verified",
        ),
    ],
)
def test_delivery_preserves_work_across_git_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_status: GitDeliveryStatus,
    expected_evidence: str,
) -> None:
    service, store, repository, remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "failure-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:failure-run", "codex/failure", worktree
    )
    change = worktree / "change.txt"
    change.write_text("preserve this change\n", encoding="utf-8")
    late_change = worktree / "late.txt"
    alternate_remote: Path | None = None
    if failure_point in {"remote_mismatch_before_push", "remote_mismatch_after_push"}:
        alternate_remote = tmp_path / "unapproved" / "repository.git"
        alternate_remote.parent.mkdir(parents=True)
        subprocess.run(
            ("git", "init", "--bare", str(alternate_remote)),
            check=True,
            capture_output=True,
        )

    original_run_git = service.worktrees.run_git
    validation_calls = 0
    commit_created = False

    def validate_run() -> None:
        nonlocal validation_calls
        validation_calls += 1
        if (
            failure_point == "wrong_branch_before_staging" and validation_calls == 2
        ) or (failure_point == "wrong_branch_after_staging" and validation_calls == 3):
            git_command(worktree, "switch", "-c", "codex/unexpected")
        elif failure_point == "nothing_to_stage" and validation_calls == 2:
            change.unlink()
        elif failure_point == "dirty_before_push" and validation_calls == 4:
            late_change.write_text("preserve this later change\n", encoding="utf-8")
        elif (
            failure_point == "remote_mismatch_before_push" and validation_calls == 4
        ) or (failure_point == "remote_mismatch_after_push" and validation_calls == 6):
            assert alternate_remote is not None
            git_command(
                repository,
                "remote",
                "set-url",
                "--push",
                "origin",
                str(alternate_remote),
            )

    def intercept_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        nonlocal commit_created
        if failure_point == "stage_failure" and "add" in arguments:
            return subprocess.CompletedProcess(
                arguments, 1, "", "injected stage failure"
            )
        if (
            failure_point == "diff_failure"
            and "diff" in arguments
            and "--cached" in arguments
        ):
            return subprocess.CompletedProcess(
                arguments, 2, "", "injected diff failure"
            )
        if failure_point == "commit_failure" and "commit" in arguments:
            return subprocess.CompletedProcess(
                arguments, 1, "", "injected commit failure"
            )
        if (
            failure_point == "missing_commit_sha"
            and commit_created
            and arguments[-2:] == ("rev-parse", "HEAD")
        ):
            return subprocess.CompletedProcess(
                arguments, 1, "", "injected head failure"
            )
        if failure_point == "unverifiable_remote_push" and "ls-remote" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "", "")

        result = original_run_git(*arguments)
        if "commit" in arguments and "-m" in arguments and result.returncode == 0:
            commit_created = True
            if failure_point == "dirty_after_commit":
                late_change.write_text("preserve this later change\n", encoding="utf-8")
        return result

    monkeypatch.setattr(service.worktrees, "run_git", intercept_git)
    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:failure-run",
        "owner/api",
        "deliver failure-path change",
        validate_run,
    )
    gate = store.latest_gate(lease.lease_id, "git_delivery")
    store.close()

    assert result.status is expected_status
    assert expected_evidence in result.evidence
    assert gate is not None
    assert gate.allowed is (expected_status is GitDeliveryStatus.NO_CHANGES)

    remote_row = git_command(
        repository, "ls-remote", str(remote), f"refs/heads/{lease.branch}"
    )
    remote_sha = remote_row.split()[0] if remote_row else None
    if failure_point in {"remote_mismatch_after_push", "unverifiable_remote_push"}:
        assert result.commit_sha is not None
        assert remote_sha == result.commit_sha
    else:
        assert remote_sha is None

    if failure_point in {"wrong_branch_before_staging", "stage_failure"}:
        assert git_command(worktree, "status", "--porcelain") == "?? change.txt"
    elif failure_point == "wrong_branch_after_staging" or failure_point in {
        "diff_failure",
        "commit_failure",
    }:
        assert git_command(worktree, "diff", "--cached", "--name-only") == "change.txt"
    elif failure_point == "nothing_to_stage":
        assert git_command(worktree, "status", "--porcelain") == ""
    elif failure_point == "missing_commit_sha":
        assert git_command(worktree, "rev-parse", "HEAD") != git_command(
            repository, "rev-parse", "HEAD"
        )
        assert result.commit_sha is None
    elif failure_point in {"dirty_after_commit", "dirty_before_push"}:
        assert result.commit_sha == git_command(worktree, "rev-parse", "HEAD")
        assert late_change.read_text(encoding="utf-8") == (
            "preserve this later change\n"
        )


def test_delivery_rejects_missing_host_identity_before_staging(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = make_delivery_service(
        tmp_path / "identity", identity=False
    )
    service.worktrees.host_git_identity = (None, None)
    worktree = tmp_path / "identity-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:identity-run", "codex/identity", worktree
    )
    (worktree / "change.txt").write_text("keep unstaged\n", encoding="utf-8")
    original_status = service.worktrees._git(
        "-C", str(worktree), "status", "--porcelain"
    )

    missing_identity = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:identity-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )

    assert missing_identity.status is GitDeliveryStatus.BLOCKED
    assert "user.name" in missing_identity.evidence
    assert (
        service.worktrees._git("-C", str(worktree), "status", "--porcelain")
        == original_status
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()
