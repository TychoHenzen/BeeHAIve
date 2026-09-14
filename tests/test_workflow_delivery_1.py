from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    DeterministicCheckRunner,
    GitDeliveryStatus,
    WorkflowRole,
    WorkflowStore,
)
from tests.support.workflow.fixture_check import FixtureCheck as FixtureCheck
from tests.support.workflow.helpers import (
    commit_repository_change,
    git_command,
    make_delivery_service,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_commit_and_push_records_exact_commit_for_handoff_and_remote(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
    base_head = git_command(repository, "rev-parse", "HEAD")
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
        git_command(
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
    assert git_command(repository, "rev-parse", "HEAD") == base_head
    assert git_command(repository, "status", "--porcelain") == ""
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()
    reloaded = WorkflowStore(tmp_path / "workflow.sqlite3")
    persisted = reloaded.latest_gate(lease.lease_id, "git_delivery")
    assert persisted is not None and persisted.allowed
    assert {check.name: check.evidence for check in persisted.checks}[
        "commit_sha"
    ] == result.commit_sha
    reloaded.close()


def test_commit_and_push_quality_gate_blocks_before_staging(tmp_path: Path) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    service.checks = DeterministicCheckRunner(
        [FixtureCheck("coverage", passed=False, evidence="Coverage is 89%")]
    )
    worktree = tmp_path / "quality-blocked-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:quality-blocked", "codex/quality-blocked", worktree
    )
    (worktree / "change.txt").write_text("unverified change\n", encoding="utf-8")
    base_head = service.worktrees.head(worktree)
    status_before = git_command(
        worktree, "status", "--porcelain", "--untracked-files=all"
    )

    result = service.commit_and_push(
        lease.lease_id,
        lease.lease_token,
        "dashboard-run:quality-blocked",
        "owner/api",
        "deliver unverified change",
        lambda: None,
    )

    assert result.status is GitDeliveryStatus.BLOCKED
    assert "coverage" in result.evidence
    assert service.worktrees.head(worktree) == base_head
    assert (
        git_command(worktree, "status", "--porcelain", "--untracked-files=all")
        == status_before
    )
    gate = store.latest_gate(lease.lease_id, "git_delivery")
    assert gate is not None and gate.allowed is False
    coverage = next(check for check in gate.checks if check.name == "coverage")
    assert coverage.status == "failed"
    assert coverage.required is True
    store.close()


def test_delivery_commits_changes_added_after_a_successful_push(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
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
    remote_head = git_command(
        repository,
        "ls-remote",
        "--exit-code",
        "origin",
        f"refs/heads/{lease.branch}",
    ).split()[0]
    assert remote_head == second.commit_sha

    local_head = commit_repository_change(
        worktree, "local.txt", "existing local commit\n"
    )
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
        git_command(
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
