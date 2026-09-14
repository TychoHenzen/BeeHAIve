from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from .handoff_status import HandoffStatus
from .helpers import current_timestamp, lease_expiry, require_path, require_text
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .workspace_lease import WorkspaceLease
from typing import Any


class WorkflowStoreLeaseWriteMixin:
    def renew_lease(
        self: Any, lease_id: str, lease_token: str | None
    ) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
            if not lease_token or row["lease_token"] != lease_token:
                raise WorkflowError("Lease token is invalid")
            connection.execute(
                """
                    UPDATE workflow_leases
                    SET expires_at = ?, updated_at = ?
                    WHERE lease_id = ? AND status = ? AND lease_token = ?
                    """,
                (
                    lease_expiry(self._lease_ttl_seconds),
                    current_timestamp(),
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                    lease_token,
                ),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def retain_lease(
        self: Any, lease_id: str, lease_token: str | None
    ) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
            if not lease_token or row["lease_token"] != lease_token:
                raise WorkflowError("Lease token is invalid")
            connection.execute(
                """
                    UPDATE workflow_leases
                    SET status = ?, expires_at = NULL, updated_at = ?
                    WHERE lease_id = ? AND status = ? AND lease_token = ?
                    """,
                (
                    LeaseStatus.RETAINED.value,
                    current_timestamp(),
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                    lease_token,
                ),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def acquire_lease(
        self: Any, agent_id: str, branch: str, worktree_path: str
    ) -> WorkspaceLease:
        agent_id = require_text(agent_id, "agent id")
        branch = require_text(branch, "branch")
        worktree_path = require_path(worktree_path, "worktree path")
        timestamp = current_timestamp()
        lease_id = str(uuid4())
        lease_token = str(uuid4())
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                        INSERT INTO workflow_leases(
                            lease_id, agent_id, branch, worktree_path, lease_token,
                            expires_at, status, stop_reason, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                        """,
                    (
                        lease_id,
                        agent_id,
                        branch,
                        worktree_path,
                        lease_token,
                        lease_expiry(self._lease_ttl_seconds),
                        LeaseStatus.ACTIVE.value,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowError(
                "An active agent already owns this branch or worktree"
            ) from exc
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease was not persisted")
        return lease

    def release_lease(
        self: Any, lease_id: str, *, allow_stopped: bool = False
    ) -> WorkspaceLease:
        return self._set_lease_status(
            lease_id, LeaseStatus.RELEASED, None, allow_stopped=allow_stopped
        )

    def stop_lease(self: Any, lease_id: str, reason: str) -> WorkspaceLease:
        return self._set_lease_status(
            lease_id, LeaseStatus.STOPPED, require_text(reason, "stop reason")
        )

    def _set_lease_status(
        self: Any,
        lease_id: str,
        status: LeaseStatus,
        reason: str | None,
        *,
        allow_stopped: bool = False,
    ) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            current_status = LeaseStatus(str(row["status"]))
            if current_status is LeaseStatus.RELEASED:
                return self._lease_from_row(row)
            if current_status is LeaseStatus.STOPPED:
                if not allow_stopped:
                    return self._lease_from_row(row)
            elif current_status is LeaseStatus.RETAINED:
                if status not in {LeaseStatus.RELEASED, LeaseStatus.STOPPED}:
                    raise WorkflowError("A retained workspace cannot be renewed")
            else:
                assert current_status is LeaseStatus.ACTIVE
            if status is LeaseStatus.RELEASED:
                pending = connection.execute(
                    """
                        SELECT 1 FROM workflow_handoffs
                        WHERE lease_id = ? AND status IN (?, ?)
                        LIMIT 1
                        """,
                    (
                        lease_id,
                        HandoffStatus.AWAITING_APPROVAL.value,
                        HandoffStatus.AWAITING_CLARIFICATION.value,
                    ),
                ).fetchone()
                if pending is not None:
                    raise WorkflowError(
                        "Cannot release a lease with an unresolved handoff"
                    )
            timestamp = current_timestamp()
            if status is LeaseStatus.STOPPED:
                connection.execute(
                    """
                        UPDATE workflow_handoffs
                        SET status = ?, required_action = ?, updated_at = ?
                        WHERE lease_id = ? AND status NOT IN (?, ?)
                        """,
                    (
                        HandoffStatus.STOPPED.value,
                        reason,
                        timestamp,
                        lease_id,
                        HandoffStatus.ACCEPTED.value,
                        HandoffStatus.STOPPED.value,
                    ),
                )
            connection.execute(
                """
                    UPDATE workflow_leases
                    SET status = ?, stop_reason = ?, updated_at = ?
                    WHERE lease_id = ?
                    """,
                (status.value, reason, timestamp, lease_id),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease
