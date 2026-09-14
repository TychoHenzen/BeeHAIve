from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    HandoffStatus,
    LeaseStatus,
    WorkflowError,
)
from tests.support.workflow.helpers import commit_repository_change, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_clarification_stop_and_release_states(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path)
    clarification_worktree = tmp_path / "clarification-worktree"
    lease = service.acquire_workspace(
        "writer", "codex/clarification", clarification_worktree
    )
    commit_sha = commit_repository_change(
        clarification_worktree, "clarify.txt", "clarify\n"
    )
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
