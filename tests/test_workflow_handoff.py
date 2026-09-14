from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    CheckResult,
    HandoffStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowStore,
    _checks_from_json,
)
from tests.support.workflow.helpers import commit_repository_change, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


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


def test_workflow_rejects_non_topological_role_handoffs(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path)
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
    service, store, repository = make_service(tmp_path)
    worktree = tmp_path / "cas-worktree"
    lease = service.acquire_workspace("writer", "codex/cas", worktree)
    commit_sha = commit_repository_change(worktree, "cas.txt", "cas\n")
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
