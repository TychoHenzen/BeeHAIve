from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from beehaiive.workflow import (
    CheckResult,
    CommandCheck,
    Constitution,
    DeterministicCheckRunner,
    GateResult,
    GitDeliveryStatus,
    GitWorktreeManager,
    HandoffStatus,
    LeaseStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowService,
    WorkflowStore,
    _checks_from_json,
    _lease_is_expired,
)
from main import _production_workflow_service, create_app

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


@dataclass
class FixtureCheck:
    name: str
    passed: bool = True
    evidence: str = "fixture passed"
    calls: int = 0

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.exists()
        self.calls += 1
        return CheckResult(self.name, self.passed, self.evidence)


class RaisingCheck:
    name = "raising"

    def run(self, workspace: Path) -> CheckResult:
        del workspace
        raise RuntimeError("fixture exploded")


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Workflow Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository


def _commit(worktree: Path, name: str, content: str) -> str:
    (worktree / name).write_text(content, encoding="utf-8")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-m", f"add {name}")
    return _git(worktree, "rev-parse", "HEAD")


def _service(
    tmp_path: Path,
    checks: list[FixtureCheck] | None = None,
) -> tuple[WorkflowService, WorkflowStore, Path]:
    repository = _repository(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        checks or [FixtureCheck("tests")],
    )
    return service, store, repository


def _delivery_service(
    tmp_path: Path,
    *,
    identity: bool = True,
    push_remote: Path | None = None,
) -> tuple[WorkflowService, WorkflowStore, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = _repository(tmp_path)
    remote = tmp_path / "owner" / "api.git"
    remote.parent.mkdir()
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "master")
    if push_remote is not None:
        push_remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ("git", "init", "--bare", str(push_remote)),
            check=True,
            capture_output=True,
        )
        _git(repository, "remote", "set-url", "--push", "origin", str(push_remote))
    if not identity:
        _git(repository, "config", "--unset", "user.name")
        _git(repository, "config", "--unset", "user.email")
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck("tests")],
    )
    return service, store, repository, remote


def test_commit_and_push_records_exact_commit_for_handoff_and_remote(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = _delivery_service(tmp_path)
    base_head = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "leased-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:delivery-run", "codex/delivery", worktree
    )
    (worktree / "change.txt").write_text("leased change\n", encoding="utf-8")

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:delivery-run",
        "owner/api",
        "deliver leased change",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.PUSHED
    assert result.commit_sha == service.worktrees.head(worktree)
    assert service.worktrees.clean(worktree)
    assert (
        _git(
            repository,
            "ls-remote",
            "--exit-code",
            "origin",
            "refs/heads/codex/delivery",
        ).split()[0]
        == result.commit_sha
    )
    repeated = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:delivery-run",
        "owner/api",
        "ignored after push",
        lambda: None,
    )
    assert repeated.status is GitDeliveryStatus.PUSHED
    assert repeated.commit_sha == result.commit_sha
    gate = store.latest_gate(lease.lease_id, "git_delivery")
    assert gate is not None and gate.allowed
    assert {check.name: check.evidence for check in gate.checks}[
        "commit_sha"
    ] == result.commit_sha

    handoff = service.handoff(
        lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.REVIEWER,
        result.commit_sha or "",
        "implementation complete",
    )
    assert handoff.commit_sha == result.commit_sha
    assert _git(repository, "rev-parse", "HEAD") == base_head
    assert _git(repository, "status", "--porcelain") == ""
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()
    reloaded = WorkflowStore(tmp_path / "workflow.sqlite3")
    persisted = reloaded.latest_gate(lease.lease_id, "git_delivery")
    assert persisted is not None and persisted.allowed
    assert {check.name: check.evidence for check in persisted.checks}[
        "commit_sha"
    ] == result.commit_sha
    reloaded.close()


def test_delivery_commits_changes_added_after_a_successful_push(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "post-push-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:post-push", "codex/post-push", worktree
    )
    change = worktree / "change.txt"
    change.write_text("first version\n", encoding="utf-8")
    first = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:post-push",
        "owner/api",
        "deliver first version",
        lambda: None,
    )
    assert first.status is GitDeliveryStatus.PUSHED

    change.write_text("second version\n", encoding="utf-8")
    second = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:post-push",
        "owner/api",
        "deliver second version",
        lambda: None,
    )

    assert second.status is GitDeliveryStatus.PUSHED
    assert second.commit_sha != first.commit_sha
    assert second.commit_sha == service.worktrees.head(worktree)
    assert service.worktrees.clean(worktree)
    remote_head = _git(
        repository,
        "ls-remote",
        "--exit-code",
        "origin",
        f"refs/heads/{lease.branch}",
    ).split()[0]
    assert remote_head == second.commit_sha

    local_head = _commit(worktree, "local.txt", "existing local commit\n")
    needs_retry = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:post-push",
        "owner/api",
        "deliver existing local commit",
        lambda: None,
    )
    assert needs_retry.status is GitDeliveryStatus.BLOCKED
    assert needs_retry.commit_sha == local_head
    retried = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:post-push",
        "owner/api",
        "retry existing local commit",
        lambda: None,
    )
    assert retried.status is GitDeliveryStatus.PUSHED
    assert retried.commit_sha == local_head
    assert (
        _git(
            repository,
            "ls-remote",
            "--exit-code",
            "origin",
            f"refs/heads/{lease.branch}",
        ).split()[0]
        == local_head
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_uses_the_configured_push_url_for_push_and_verification(
    tmp_path: Path,
) -> None:
    push_remote = tmp_path / "push" / "owner" / "api.git"
    service, store, _repository_path, fetch_remote = _delivery_service(
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
        == _git(
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
    service, store, repository, _remote = _delivery_service(tmp_path)
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
    assert _git(repository, "ls-remote", "origin", "refs/heads/codex/noop") == ""
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
    service, store, repository, remote = _delivery_service(tmp_path)
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
        _git(
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
    service, store, repository, remote = _delivery_service(tmp_path)
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
            _git(worktree, "switch", "-c", "codex/unexpected")
        elif failure_point == "nothing_to_stage" and validation_calls == 2:
            change.unlink()
        elif failure_point == "dirty_before_push" and validation_calls == 4:
            late_change.write_text("preserve this later change\n", encoding="utf-8")
        elif (
            failure_point == "remote_mismatch_before_push" and validation_calls == 4
        ) or (failure_point == "remote_mismatch_after_push" and validation_calls == 6):
            assert alternate_remote is not None
            _git(
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

    remote_row = _git(
        repository, "ls-remote", str(remote), f"refs/heads/{lease.branch}"
    )
    remote_sha = remote_row.split()[0] if remote_row else None
    if failure_point in {"remote_mismatch_after_push", "unverifiable_remote_push"}:
        assert result.commit_sha is not None
        assert remote_sha == result.commit_sha
    else:
        assert remote_sha is None

    if failure_point in {"wrong_branch_before_staging", "stage_failure"}:
        assert _git(worktree, "status", "--porcelain") == "?? change.txt"
    elif failure_point == "wrong_branch_after_staging" or failure_point in {
        "diff_failure",
        "commit_failure",
    }:
        assert _git(worktree, "diff", "--cached", "--name-only") == "change.txt"
    elif failure_point == "nothing_to_stage":
        assert _git(worktree, "status", "--porcelain") == ""
    elif failure_point == "missing_commit_sha":
        assert _git(worktree, "rev-parse", "HEAD") != _git(
            repository, "rev-parse", "HEAD"
        )
        assert result.commit_sha is None
    elif failure_point in {"dirty_after_commit", "dirty_before_push"}:
        assert result.commit_sha == _git(worktree, "rev-parse", "HEAD")
        assert late_change.read_text(encoding="utf-8") == (
            "preserve this later change\n"
        )


def test_delivery_rejects_missing_host_identity_before_staging(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = _delivery_service(
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


def test_delivery_rejects_unknown_lease_agent_and_empty_message(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "invalid-input-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:invalid-input", "codex/invalid-input", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")

    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        service.commit_and_push(
            "missing-lease",
            lease.lease_token,
            "dashboard-run:invalid-input",
            "owner/api",
            "unused",
            lambda: None,
        )
    with pytest.raises(WorkflowError, match="does not belong to this run"):
        service.commit_and_push(
            lease.lease_id,
            lease.lease_token,
            "dashboard-run:other-run",
            "owner/api",
            "unused",
            lambda: None,
        )
    invalid_message = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:invalid-input",
        "owner/api",
        "   ",
        lambda: None,
    )

    assert invalid_message.status is GitDeliveryStatus.BLOCKED
    assert "valid commit message" in invalid_message.evidence
    assert _git(worktree, "status", "--porcelain") == original_status
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_revalidates_the_lease_after_run_validation(tmp_path: Path) -> None:
    service, store, _repository_path, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "revalidation-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:revalidation", "codex/revalidation", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")

    def update_lease(field: str, value: str) -> None:
        with store._transaction() as connection:
            connection.execute(
                f"UPDATE workflow_leases SET {field} = ? WHERE lease_id = ?",
                (value, lease.lease_id),
            )

    for field, value, message in (
        ("lease_token", "replaced-token", "Lease token is invalid"),
        ("agent_id", "dashboard-run:other", "does not belong to this run"),
    ):
        changed = False

        def change_after_validation(field: str = field, value: str = value) -> None:
            nonlocal changed
            if not changed:
                update_lease(field, value)
                changed = True

        with pytest.raises(WorkflowError, match=message):
            service.commit_and_push(
                lease.lease_id,
                lease.lease_token,
                "dashboard-run:revalidation",
                "owner/api",
                "must not stage",
                change_after_validation,
            )
        update_lease(field, getattr(lease, field))

    stopped = False

    def stop_after_validation() -> None:
        nonlocal stopped
        if not stopped:
            store.stop_lease(lease.lease_id, "test lease change")
            stopped = True

    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        service.commit_and_push(
            lease.lease_id,
            lease.lease_token,
            "dashboard-run:revalidation",
            "owner/api",
            "must not stage",
            stop_after_validation,
        )
    assert _git(worktree, "status", "--porcelain") == original_status
    store.close()


def test_delivery_blocks_when_git_state_probes_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, repository, remote = _delivery_service(tmp_path)
    worktree = tmp_path / "probe-failure-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:probe-failure", "codex/probe-failure", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")
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
    _git(repository, "remote", "remove", "origin")
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
    _git(repository, "remote", "add", "origin", str(remote))
    assert _git(worktree, "status", "--porcelain") == original_status
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_rejects_stored_path_and_detached_head_before_staging(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "fenced-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:fenced-run", "codex/fenced", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")

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
    assert _git(worktree, "status", "--porcelain") == original_status

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

    _git(worktree, "switch", "--detach", "HEAD")
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
    assert _git(worktree, "status", "--porcelain") == original_status
    _git(worktree, "switch", lease.branch)
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_rejects_a_worktree_specific_push_url_before_staging(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "worktree-pushurl"
    lease = service.acquire_workspace(
        "dashboard-run:worktree-pushurl", "codex/worktree-pushurl", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")
    unapproved_remote = tmp_path / "unapproved" / "owner" / "api.git"
    unapproved_remote.parent.mkdir(parents=True)
    subprocess.run(
        ("git", "init", "--bare", str(unapproved_remote)),
        check=True,
        capture_output=True,
    )
    _git(repository, "config", "extensions.worktreeConfig", "true")
    _git(
        worktree,
        "config",
        "--worktree",
        "remote.origin.pushurl",
        str(unapproved_remote),
    )
    assert _git(worktree, "remote", "get-url", "--push", "--all", "origin") == str(
        unapproved_remote
    )

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
    assert _git(worktree, "status", "--porcelain") == original_status
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
    service, store, _repository_path, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "foreign-path-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:foreign-path", "codex/foreign-path", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_repository = _repository(foreign_root)
    foreign_worktree = tmp_path / "foreign-path"
    _git(
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
    assert _git(worktree, "status", "--porcelain") == original_status
    store.close()


def test_delivery_checks_run_and_workspace_tokens_before_git_mutations(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "fenced-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:fenced-run", "codex/fenced", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = _git(worktree, "status", "--porcelain")

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

    assert _git(worktree, "status", "--porcelain") == original_status
    store.close()


def test_expired_delivery_lease_keeps_the_uncommitted_worktree(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = _delivery_service(tmp_path)
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


def test_delivery_rejects_remote_credentials_and_branch_mismatch(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = _delivery_service(tmp_path)
    worktree = tmp_path / "remote-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:remote-run", "codex/remote", worktree
    )
    (worktree / "change.txt").write_text("keep unstaged\n", encoding="utf-8")
    _git(
        repository,
        "remote",
        "set-url",
        "--push",
        "origin",
        "https://operator:secret@example.invalid/owner/api.git",
    )
    original_status = service.worktrees._git(
        "-C", str(worktree), "status", "--porcelain"
    )

    remote_mismatch = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:remote-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )

    assert remote_mismatch.status is GitDeliveryStatus.BLOCKED
    assert "secret" not in remote_mismatch.evidence
    gate = store.latest_gate(lease.lease_id, "git_delivery")
    assert gate is not None
    assert all("secret" not in check.evidence for check in gate.checks)
    _git(repository, "remote", "set-url", "--push", "origin", str(_remote))
    _git(worktree, "checkout", "-b", "codex/wrong-branch")
    wrong_branch = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:remote-run",
        "owner/api",
        "must not stage",
        lambda: None,
    )
    assert wrong_branch.status is GitDeliveryStatus.BLOCKED
    assert (
        service.worktrees._git("-C", str(worktree), "status", "--porcelain")
        == original_status
    )
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_production_workflow_composition_loads_persistent_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEHAIIVE_WORKFLOW_DB", str(tmp_path / "production.db"))
    monkeypatch.setenv("BEEHAIIVE_WORKFLOW_REPOSITORY", str(tmp_path))

    service = _production_workflow_service()

    try:
        assert service.constitution.rules_for(WorkflowRole.WRITER)
        assert service.worktrees.repository == tmp_path.resolve()
    finally:
        service.store.close()


def test_constitution_and_check_runner_validate_evidence(tmp_path: Path) -> None:
    constitution = Constitution.load(CONSTITUTION_PATH)
    writer_rules = constitution.rules_for(WorkflowRole.WRITER)
    assert writer_rules == constitution.rules_for("writer")
    assert len(writer_rules) == 6
    with pytest.raises(TypeError):
        constitution.sections["project"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        constitution.roles[WorkflowRole.WRITER] = ()  # type: ignore[index]
    with pytest.raises(WorkflowError, match="Unknown workflow role"):
        constitution.rules_for("unknown")

    passing = FixtureCheck("pass")
    empty = FixtureCheck("empty", evidence=" ")

    class MismatchCheck:
        name = "mismatch"

        def run(self, workspace: Path) -> CheckResult:
            del workspace
            return CheckResult("other", True, "wrong name")

    results = DeterministicCheckRunner(
        [passing, empty, MismatchCheck(), RaisingCheck()]
    ).run(tmp_path)
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[2].evidence.startswith("Check failed to run")
    assert results[3].evidence.startswith("Check failed to run")

    with pytest.raises(WorkflowError, match="At least one"):
        DeterministicCheckRunner([])
    with pytest.raises(WorkflowError, match="needs a name"):
        DeterministicCheckRunner([FixtureCheck(" ")])
    with pytest.raises(WorkflowError, match="unique"):
        DeterministicCheckRunner([FixtureCheck("same"), FixtureCheck("same")])

    command_ok = CommandCheck("command", (sys.executable, "-c", "print('ok')"))
    command_fail = CommandCheck(
        "command-fail", (sys.executable, "-c", "import sys; sys.exit(3)")
    )
    assert command_ok.run(tmp_path).passed is True
    assert command_fail.run(tmp_path).passed is False
    with pytest.raises(WorkflowError, match="no command"):
        CommandCheck("empty", ()).run(tmp_path)


def test_constitution_rejects_invalid_documents(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(tmp_path / "missing.json")
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(invalid_json)

    cases = [
        ("not-object", [], "JSON object"),
        ("bad-version", {"version": 0}, "version"),
        ("bad-sections", {"version": 1, "sections": []}, "sections"),
        (
            "empty-rules",
            {"version": 1, "sections": {"project": []}},
            "non-empty",
        ),
        (
            "non-string-rule",
            {"version": 1, "sections": {"project": [1]}},
            "strings",
        ),
        (
            "empty-rule",
            {"version": 1, "sections": {"project": [""]}},
            "required",
        ),
        (
            "long-rule",
            {"version": 1, "sections": {"project": ["x" * 1_001]}},
            "at most",
        ),
        (
            "bad-roles",
            {"version": 1, "sections": {"project": ["rule"]}, "roles": []},
            "roles",
        ),
        (
            "unknown-role",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"unknown": ["project"]},
            },
            "Unknown workflow role",
        ),
        (
            "unknown-section",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"writer": ["engineering"]},
            },
            "unknown section",
        ),
    ]
    for name, document, message in cases:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(WorkflowError, match=message):
            Constitution.load(path)

    missing_role = tmp_path / "missing-role.json"
    missing_role.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"planner": ["project"]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="No constitution rules"):
        Constitution.load(missing_role).rules_for("writer")

    blank_section = tmp_path / "blank-section.json"
    blank_section.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"": ["rule"]},
                "roles": {"planner": [""]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="section names"):
        Constitution.load(blank_section)


def test_workflow_store_rejects_corrupt_evidence_and_unknown_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore(tmp_path / "corrupt.sqlite3")
    with pytest.raises(WorkflowError, match="required"):
        store.acquire_lease("", "branch", "path")
    with pytest.raises(WorkflowError, match="at most"):
        store.acquire_lease("x" * 401, "branch", "path")
    with pytest.raises(WorkflowError, match="required"):
        store.acquire_lease("agent", "branch", " ")
    with pytest.raises(WorkflowError, match="at most"):
        store.acquire_lease("agent", "branch-long", "x" * 1_001)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.release_lease("missing")
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.record_handoff(
            "missing",
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    with pytest.raises(WorkflowError, match="Unknown handoff"):
        store.update_handoff("missing", HandoffStatus.BLOCKED, "reason")
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.record_gate("missing", GateResult("model_call", False, (), "reason"))

    lease = store.acquire_lease("agent", "branch", "path")
    monkeypatch.setattr(store, "get_lease", lambda _lease_id: None)
    with pytest.raises(WorkflowError, match="not persisted"):
        store.acquire_lease("agent-2", "branch-2", "path-2")
    with pytest.raises(WorkflowError, match="disappeared"):
        store.release_lease(lease.lease_id)
    store.close()

    persistence_store = WorkflowStore(tmp_path / "persistence.sqlite3")
    persistence_lease = persistence_store.acquire_lease("agent", "branch", "path")
    persistence_handoff = persistence_store.record_handoff(
        persistence_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.REVIEWER,
        "sha",
        "state",
        HandoffStatus.ACCEPTED,
        (),
        (),
        None,
    )
    monkeypatch.setattr(persistence_store, "get_handoff", lambda _handoff_id: None)
    with pytest.raises(WorkflowError, match="not persisted"):
        persistence_store.record_handoff(
            persistence_lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    with pytest.raises(WorkflowError, match="disappeared"):
        persistence_store.update_handoff(
            persistence_handoff.handoff_id, HandoffStatus.BLOCKED, "reason"
        )
    persistence_store.close()


def test_workflow_store_rejects_corrupt_handoff_json(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "evidence.sqlite3")
    lease = store.acquire_lease("agent", "branch", "path")
    handoff = store.record_handoff(
        lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.REVIEWER,
        "sha",
        "state",
        HandoffStatus.ACCEPTED,
        (CheckResult("check", True, "ok"),),
        ("rule",),
        None,
    )
    corruptions = [
        ("checks_json", "{"),
        ("checks_json", "{}"),
        ("checks_json", "[1]"),
        ("checks_json", '[{"name": "check"}]'),
        ("status", "unknown"),
    ]
    with pytest.raises(WorkflowError, match="Stored verification evidence"):
        _checks_from_json(None)
    for column, value in corruptions:
        with store._transaction() as connection:
            connection.execute(
                f"UPDATE workflow_handoffs SET {column} = ? WHERE handoff_id = ?",
                (value, handoff.handoff_id),
            )
        with pytest.raises(WorkflowError):
            store.get_handoff(handoff.handoff_id)
    store.close()


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


def test_workflow_manager_and_service_report_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore()
    with pytest.raises(WorkflowError, match="does not exist"):
        GitWorktreeManager(tmp_path / "missing-repository", store)
    service_root = tmp_path / "service"
    service_root.mkdir()
    service, service_store, _ = _service(service_root, [FixtureCheck("check")])
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        service.before_model_call("missing")
    lease = service.acquire_workspace(
        "agent", "codex/service", tmp_path / "service-worktree"
    )
    with pytest.raises(WorkflowError, match="at most"):
        service.handoff(
            lease.lease_id,
            "writer",
            "reviewer",
            "sha",
            "x" * 1_001,
        )
    original_head = service.worktrees.head

    def broken_head(_worktree: str | Path) -> str:
        raise WorkflowError("head unavailable")

    monkeypatch.setattr(service.worktrees, "head", broken_head)
    commit_failure = service.handoff(
        lease.lease_id, "writer", "reviewer", "sha", "state"
    )
    assert commit_failure.status is HandoffStatus.BLOCKED
    monkeypatch.setattr(service.worktrees, "head", original_head)

    def broken_clean(_worktree: str | Path) -> bool:
        raise WorkflowError("clean check unavailable")

    monkeypatch.setattr(service.worktrees, "clean", broken_clean)
    clean_failure = service.handoff(
        lease.lease_id, "writer", "reviewer", "sha", "state"
    )
    assert clean_failure.status is HandoffStatus.BLOCKED
    service_store.close()
    store.close()


def test_workflow_rejects_non_topological_role_handoffs(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    worktree = tmp_path / "role-worktree"
    lease = service.acquire_workspace("writer", "codex/role", worktree)

    with pytest.raises(WorkflowError, match="not permitted"):
        service.handoff(
            lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.PLANNER,
            "sha",
            "state",
        )
    assert store.latest_handoff(lease.lease_id) is None
    service.release_workspace(lease.lease_id)
    store.close()


def test_handoff_updates_use_compare_and_set_status(tmp_path: Path) -> None:
    service, store, repository = _service(tmp_path)
    worktree = tmp_path / "cas-worktree"
    lease = service.acquire_workspace("writer", "codex/cas", worktree)
    commit_sha = _commit(worktree, "cas.txt", "cas\n")
    waiting = service.handoff(
        lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        commit_sha,
        "ready",
    )

    stopped = store.update_handoff(
        waiting.handoff_id,
        HandoffStatus.STOPPED,
        "operator stop",
        expected_status=HandoffStatus.AWAITING_APPROVAL,
    )
    assert stopped.status is HandoffStatus.STOPPED
    with pytest.raises(WorkflowError, match="transition conflict"):
        store.update_handoff(
            waiting.handoff_id,
            HandoffStatus.ACCEPTED,
            None,
            expected_status=HandoffStatus.AWAITING_APPROVAL,
        )
    manager = service.worktrees
    manager._cleanup_stopped_worktree(str(worktree))
    store.release_lease(lease.lease_id, allow_stopped=True)
    store.close()
    assert repository.exists()


def test_worktree_path_identity_preserves_internal_whitespace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "worktree  with  spaces"

    lease = manager.acquire("agent", "codex/spaces", worktree)

    assert lease.worktree_path == str(worktree.resolve())
    released = manager.release(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED
    store.close()


def test_successful_run_workspace_is_retained_until_explicit_release(
    tmp_path: Path,
) -> None:
    service, store, _repository_path = _service(tmp_path)
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

    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-m", "consume retained worktree")
    released = service.release_workspace(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED
    assert not worktree.exists()
    store.close()


def test_restart_cleanup_removes_only_unfinished_dashboard_workspaces(
    tmp_path: Path,
) -> None:
    service, store, _repository_path = _service(tmp_path)
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
    service, store, _repository_path = _service(tmp_path)
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


def test_retained_lease_validates_state_and_can_be_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _repository_path = _service(tmp_path)
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
    repository = _repository(tmp_path)
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
    repository = _repository(tmp_path)
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
    repository = _repository(tmp_path)
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
    repository = _repository(tmp_path)
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


def test_handoffs_store_checks_commits_and_approval_states(tmp_path: Path) -> None:
    approval_check = FixtureCheck("tests")
    service, store, repository = _service(tmp_path, [approval_check])
    worktree = tmp_path / "writer-worktree"
    lease = service.acquire_workspace("writer-1", "codex/writer-1", worktree)
    gate = service.before_model_call(lease.lease_id)
    assert gate.allowed is True
    commit_sha = _commit(worktree, "change.txt", "change\n")

    accepted = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        commit_sha,
        "implementation complete",
    )
    assert accepted.status is HandoffStatus.ACCEPTED
    assert accepted.commit_sha == commit_sha
    assert accepted.constitution_rules
    assert {check.name for check in accepted.checks} == {
        "tests",
        "source_state",
        "constitution",
        "commit",
        "working_tree",
    }
    with pytest.raises(WorkflowError, match="cannot be paused"):
        service.request_clarification(accepted.handoff_id, "not now")
    reloaded = WorkflowStore(tmp_path / "workflow.sqlite3")
    assert reloaded.get_handoff(accepted.handoff_id) == accepted
    reloaded.close()

    approval_worktree = tmp_path / "approval-worktree"
    approval_lease = service.acquire_workspace(
        "writer-2", "codex/writer-2", approval_worktree
    )
    approval_sha = _commit(approval_worktree, "approval.txt", "approval\n")
    waiting = service.handoff(
        approval_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        approval_sha,
        "needs operator",
        approval_required=False,
    )
    assert waiting.status is HandoffStatus.AWAITING_APPROVAL
    with pytest.raises(WorkflowError, match="unresolved handoff"):
        store.release_lease(approval_lease.lease_id)
    approval_check.passed = False
    blocked_approval = service.approve_handoff(waiting.handoff_id, "operator-1")
    assert blocked_approval.status is HandoffStatus.BLOCKED
    approval_check.passed = True
    waiting = service.handoff(
        approval_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        approval_sha,
        "needs operator again",
        approval_required=True,
    )
    approved = service.approve_handoff(waiting.handoff_id, "operator-1", "reviewed")
    assert approved.status is HandoffStatus.ACCEPTED
    assert approved.approval_actor == "operator-1"
    assert approved.approval_note == "reviewed"

    service.release_workspace(lease.lease_id)
    service.release_workspace(approval_lease.lease_id)
    assert repository.exists()
    store.close()


def test_failed_and_missing_evidence_pause_workflow(tmp_path: Path) -> None:
    failing_check = FixtureCheck("tests", passed=False, evidence="tests failed")
    service, store, _ = _service(tmp_path, [failing_check])
    worktree = tmp_path / "blocked-worktree"
    lease = service.acquire_workspace("writer", "codex/blocked", worktree)
    gate = service.before_model_call(lease.lease_id)
    assert gate.allowed is False
    assert gate.required_action is not None

    blocked = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        "",
        "",
    )
    assert blocked.status is HandoffStatus.BLOCKED
    assert "tests" in (blocked.required_action or "")
    assert any(check.name == "commit" and not check.passed for check in blocked.checks)
    with pytest.raises(WorkflowError, match="awaiting approval"):
        service.approve_handoff(blocked.handoff_id, "operator")

    dirty_sha = _commit(worktree, "dirty.txt", "committed\n")
    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    dirty = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        dirty_sha,
        "writer state",
    )
    assert dirty.status is HandoffStatus.BLOCKED
    assert any(
        check.name == "working_tree" and not check.passed for check in dirty.checks
    )
    store.close()


def test_clarification_stop_and_release_states(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    clarification_worktree = tmp_path / "clarification-worktree"
    lease = service.acquire_workspace(
        "writer", "codex/clarification", clarification_worktree
    )
    commit_sha = _commit(clarification_worktree, "clarify.txt", "clarify\n")
    waiting = service.handoff(
        lease.lease_id,
        "writer",
        "operator",
        commit_sha,
        "waiting",
        approval_required=True,
    )
    clarified = service.request_clarification(waiting.handoff_id, "Which scope?")
    assert clarified.status is HandoffStatus.AWAITING_CLARIFICATION
    answered = service.answer_clarification(waiting.handoff_id, "The API only")
    assert answered.status is HandoffStatus.BLOCKED
    assert "API only" in (answered.required_action or "")
    with pytest.raises(WorkflowError, match="not awaiting clarification"):
        service.answer_clarification(waiting.handoff_id, "again")
    with pytest.raises(WorkflowError, match="awaiting approval"):
        service.approve_handoff(waiting.handoff_id, "operator")

    stopped = service.stop(lease.lease_id, "Operator paused the run")
    assert stopped.status is LeaseStatus.STOPPED
    assert service.get_handoff(waiting.handoff_id).status is HandoffStatus.STOPPED
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        service.before_model_call(lease.lease_id)

    release_worktree = tmp_path / "release-worktree"
    release_lease = service.acquire_workspace(
        "writer-2", "codex/release", release_worktree
    )
    assert (
        service.release_workspace(release_lease.lease_id).status is LeaseStatus.RELEASED
    )
    assert (
        service.release_workspace(release_lease.lease_id).status is LeaseStatus.RELEASED
    )
    with pytest.raises(WorkflowError, match="Unknown handoff"):
        service.get_handoff("missing")
    store.close()


def test_workflow_api_requires_server_configured_operator(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor=WorkflowRole.WRITER,
        )
    )

    response = client.post(
        "/workflow/handoffs/missing/approve",
        json={"note": "caller cannot choose the actor"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 403
    store.close()


def test_workflow_api_rejects_missing_or_invalid_operator_configuration(
    tmp_path: Path,
) -> None:
    for actor in (None, "not-a-role"):
        service_root = tmp_path / (actor or "missing")
        service_root.mkdir()
        service, store, _ = _service(service_root)
        client = TestClient(
            create_app(
                workflow_service=service,
                api_key="test-key",
                workflow_actor=actor,
            )
        )
        response = client.post(
            "/workflow/handoffs/missing/approve",
            json={"note": "operator decision"},
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 503
        store.close()


def test_workflow_api_exposes_gates_and_operator_controls(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    auth = {"X-API-Key": "test-key"}
    no_auth = client.post("/workflow/model-calls", json={"lease_id": "missing"})
    assert no_auth.status_code == 401

    worktree = tmp_path / "api-worktree"
    acquired = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-api",
            "branch": "codex/api-workflow",
            "worktree": str(worktree),
        },
        headers=auth,
    )
    assert acquired.status_code == 200
    lease_id = acquired.json()["lease_id"]
    lease_auth = {
        **auth,
        "X-Workflow-Lease-Token": acquired.json()["lease_token"],
    }
    invalid_token = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=auth,
    )
    assert invalid_token.status_code == 409
    renewed = client.post(f"/workflow/workspaces/{lease_id}/renew", headers=lease_auth)
    assert renewed.status_code == 200
    invalid_role = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "not-a-role",
            "target_role": "operator",
        },
        headers=lease_auth,
    )
    assert invalid_role.status_code == 422
    assert client.post(
        "/workflow/model-calls", json={"lease_id": lease_id}, headers=lease_auth
    ).json()["allowed"]
    commit_sha = _commit(worktree, "api.txt", "api\n")

    waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert waiting.status_code == 200
    handoff_id = waiting.json()["handoff_id"]
    pending_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert pending_gate.status_code == 200
    assert pending_gate.json()["allowed"] is False
    assert "approval" in pending_gate.json()["required_action"].lower()
    duplicate_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "duplicate",
        },
        headers=lease_auth,
    )
    assert duplicate_waiting.status_code == 409
    unresolved_release = client.post(
        f"/workflow/workspaces/{lease_id}/release", headers=lease_auth
    )
    assert unresolved_release.status_code == 409
    assert worktree.exists()
    assert (
        client.get(f"/workflow/handoffs/{handoff_id}", headers=auth).status_code == 200
    )
    clarified = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify",
        json={"question": "Confirm the scope"},
        headers=lease_auth,
    )
    answered = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify/answer",
        json={"answer": "The API only"},
        headers=lease_auth,
    )
    assert clarified.status_code == 200
    assert answered.status_code == 200
    blocked_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert blocked_gate.status_code == 200
    assert blocked_gate.json()["allowed"] is False
    second_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready again",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert second_waiting.status_code == 200
    second_handoff_id = second_waiting.json()["handoff_id"]
    approved = client.post(
        f"/workflow/handoffs/{second_handoff_id}/approve",
        json={"actor": "writer", "note": "go"},
        headers=lease_auth,
    )
    assert approved.status_code == 200
    assert approved.json()["approval_actor"] == "operator"
    unknown = client.get("/workflow/handoffs/missing", headers=auth)
    assert unknown.status_code == 409

    release_worktree = tmp_path / "api-release"
    release = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-release",
            "branch": "codex/api-release",
            "worktree": str(release_worktree),
        },
        headers=auth,
    )
    release_id = release.json()["lease_id"]
    release_auth = {
        **auth,
        "X-Workflow-Lease-Token": release.json()["lease_token"],
    }
    released = client.post(
        f"/workflow/workspaces/{release_id}/release", headers=release_auth
    )
    assert released.status_code == 200

    stop_worktree = tmp_path / "api-stop"
    stop = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-stop",
            "branch": "codex/api-stop",
            "worktree": str(stop_worktree),
        },
        headers=auth,
    )
    stop_id = stop.json()["lease_id"]
    stop_auth = {
        **auth,
        "X-Workflow-Lease-Token": stop.json()["lease_token"],
    }
    stopped = client.post(
        f"/workflow/workspaces/{stop_id}/stop",
        json={"reason": "operator stop"},
        headers=stop_auth,
    )
    assert stopped.status_code == 200
    cleaned = client.post(f"/workflow/workspaces/{stop_id}/release", headers=stop_auth)
    assert cleaned.status_code == 200
    assert not stop_worktree.exists()
    missing_service = TestClient(create_app(api_key="test-key"))
    unavailable = missing_service.post(
        "/workflow/model-calls", json={"lease_id": "missing"}, headers=auth
    )
    assert unavailable.status_code == 503
    store.close()
