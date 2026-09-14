from __future__ import annotations

from typing import TYPE_CHECKING

from .handoff_status import HandoffStatus
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .workspace_lease import WorkspaceLease
from typing import Any


class WorkflowStoreLeaseReadMixin:
    def get_lease(self: Any, lease_id: str) -> WorkspaceLease | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return None if row is None else self._lease_from_row(row)

    def get_lease_for_agent(self: Any, agent_id: str) -> WorkspaceLease | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                    SELECT * FROM workflow_leases
                    WHERE agent_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                (agent_id,),
            ).fetchone()
        return None if row is None else self._lease_from_row(row)

    def dashboard_run_leases(self: Any) -> tuple[WorkspaceLease, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                    SELECT * FROM workflow_leases
                    WHERE agent_id GLOB 'dashboard-run:*'
                    AND status IN (?, ?)
                    ORDER BY created_at
                    """,
                (LeaseStatus.ACTIVE.value, LeaseStatus.STOPPED.value),
            ).fetchall()
        return tuple(self._lease_from_row(row) for row in rows)

    def reclaim_expired(self: Any) -> tuple[WorkspaceLease, ...]:
        with self._transaction(reclaim_expired=False) as connection:
            expired_ids = self._expire_active_leases(connection)
            if not expired_ids:
                return ()
            placeholders = ", ".join("?" for _ in expired_ids)
            rows = connection.execute(
                f"SELECT * FROM workflow_leases WHERE lease_id IN ({placeholders})",
                expired_ids,
            ).fetchall()
            return tuple(self._lease_from_row(row) for row in rows)

    def require_lease_token(
        self: Any,
        lease_id: str,
        lease_token: str | None,
        *,
        allow_stopped: bool = False,
    ) -> WorkspaceLease:
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is not LeaseStatus.ACTIVE and not (
            allow_stopped and lease.status is LeaseStatus.STOPPED
        ):
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        if not lease_token or lease.lease_token != lease_token:
            raise WorkflowError("Lease token is invalid")
        return lease

    def ensure_release_allowed(self: Any, lease_id: str) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
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
                raise WorkflowError("Cannot release a lease with an unresolved handoff")
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease
