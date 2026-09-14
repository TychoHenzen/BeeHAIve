from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    GitDeliveryStatus,
    WorkflowError,
)
from tests.support.workflow.helpers import git_command, make_delivery_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_delivery_rejects_unknown_lease_agent_and_empty_message(
    tmp_path: Path,
) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "invalid-input-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:invalid-input", "codex/invalid-input", worktree
    )
    (worktree / "change.txt").write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")

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
    assert git_command(worktree, "status", "--porcelain") == original_status
    service.discard_workspace(lease.lease_id, "test cleanup")
    store.close()


def test_delivery_revalidates_the_lease_after_run_validation(tmp_path: Path) -> None:
    service, store, _repository_path, _remote = make_delivery_service(tmp_path)
    worktree = tmp_path / "revalidation-worktree"
    lease = service.acquire_workspace(
        "dashboard-run:revalidation", "codex/revalidation", worktree
    )
    change = worktree / "change.txt"
    change.write_text("leave untouched\n", encoding="utf-8")
    original_status = git_command(worktree, "status", "--porcelain")

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
    assert git_command(worktree, "status", "--porcelain") == original_status
    store.close()
