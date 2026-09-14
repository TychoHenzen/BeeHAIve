from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import uuid4

from .handoff_status import HandoffStatus
from .helpers import checks_to_json, current_timestamp
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .check_result import CheckResult
    from .handoff_record import HandoffRecord
    from .workflow_role import WorkflowRole
from typing import Any


class WorkflowStoreHandoffMixin:
    def record_handoff(
        self: Any,
        lease_id: str,
        source_role: WorkflowRole,
        target_role: WorkflowRole,
        commit_sha: str,
        source_state: str,
        status: HandoffStatus,
        checks: Sequence[CheckResult],
        constitution_rules: Sequence[str],
        required_action: str | None,
    ) -> HandoffRecord:
        handoff_id = str(uuid4())
        timestamp = current_timestamp()
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT lease_id, status FROM workflow_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(lease["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {str(lease['status'])}")
            open_handoff = connection.execute(
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
            if open_handoff is not None:
                raise WorkflowError("An unresolved handoff already exists")
            connection.execute(
                """
                    INSERT INTO workflow_handoffs(
                        handoff_id, lease_id, source_role, target_role, commit_sha,
                        source_state, status, checks_json, constitution_json,
                        required_action, approval_actor, approval_note,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                (
                    handoff_id,
                    lease_id,
                    source_role.value,
                    target_role.value,
                    commit_sha,
                    source_state,
                    status.value,
                    checks_to_json(checks),
                    json.dumps(list(constitution_rules)),
                    required_action,
                    timestamp,
                    timestamp,
                ),
            )
        handoff = self.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError("Handoff was not persisted")
        return handoff

    def get_handoff(self: Any, handoff_id: str) -> HandoffRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
        return None if row is None else self._handoff_from_row(row)

    def latest_handoff(self: Any, lease_id: str) -> HandoffRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                    SELECT * FROM workflow_handoffs
                    WHERE lease_id = ?
                    ORDER BY created_at DESC, handoff_id DESC
                    LIMIT 1
                    """,
                (lease_id,),
            ).fetchone()
        return None if row is None else self._handoff_from_row(row)

    def update_handoff(
        self: Any,
        handoff_id: str,
        status: HandoffStatus,
        required_action: str | None,
        checks: Sequence[CheckResult] | None = None,
        approval_actor: str | None = None,
        approval_note: str | None = None,
        expected_status: HandoffStatus | None = None,
    ) -> HandoffRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown handoff: {handoff_id}")
            where = "WHERE handoff_id = ?"
            parameters: list[object] = [handoff_id]
            if expected_status is not None:
                where += " AND status = ?"
                parameters.append(expected_status.value)
            parameters = [
                status.value,
                required_action,
                checks_to_json(checks)
                if checks is not None
                else str(row["checks_json"]),
                approval_actor if approval_actor is not None else row["approval_actor"],
                approval_note if approval_note is not None else row["approval_note"],
                current_timestamp(),
                *parameters,
            ]
            updated = connection.execute(
                """
                    UPDATE workflow_handoffs
                    SET status = ?, required_action = ?, checks_json = ?,
                        approval_actor = ?, approval_note = ?, updated_at = ?
                    """
                + where,
                parameters,
            )
            if updated.rowcount != 1:
                raise WorkflowError("Handoff transition conflict")
        handoff = self.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError("Handoff disappeared")
        return handoff
