from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitDeliveryStatus,
    HandoffStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowStore,
)
from tests.support.workflow.fixture_check import FixtureCheck as FixtureCheck
from tests.support.workflow.helpers import (
    commit_repository_change,
    git_command,
    make_delivery_service,
    make_service,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_delivery_rejects_remote_credentials_and_branch_mismatch(
    tmp_path: Path,
) -> None:
    service, store, repository, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "remote-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:remote-run", "codex/remote", worktree
    )
    (worktree / "change.txt").write_text("keep unstaged\n", encoding="utf-8")
    git_command(
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
    git_command(repository, "remote", "set-url", "--push", "origin", str(_remote))
    git_command(worktree, "checkout", "-b", "codex/wrong-branch")
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


def test_handoffs_store_checks_commits_and_approval_states(tmp_path: Path) -> None:
    approval_check = FixtureCheck("tests")
    service, store, repository = make_service(tmp_path, [approval_check])
    worktree = tmp_path / "writer-worktree"
    lease = service.acquire_workspace("writer-1", "codex/writer-1", worktree)
    gate = service.before_model_call(lease.lease_id)
    assert gate.allowed is True
    commit_sha = commit_repository_change(worktree, "change.txt", "change\n")

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
    approval_sha = commit_repository_change(
        approval_worktree, "approval.txt", "approval\n"
    )
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
