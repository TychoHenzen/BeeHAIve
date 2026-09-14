from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from .handoff_record import HandoffRecord
from .handoff_status import HandoffStatus
from .helpers import (
    checks_from_json,
    current_timestamp,
    enum_role,
    enum_status,
    lease_is_expired,
)
from .lease_status import LeaseStatus
from .repair_record import RepairRecord
from .repair_status import RepairStatus
from .workflow_error import WorkflowError
from .workspace_lease import WorkspaceLease


class WorkflowStoreRecordMixin:
    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> WorkspaceLease:
        return WorkspaceLease(
            lease_id=str(row["lease_id"]),
            agent_id=str(row["agent_id"]),
            branch=str(row["branch"]),
            worktree_path=str(row["worktree_path"]),
            status=LeaseStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            lease_token=row["lease_token"],
            expires_at=row["expires_at"],
            stop_reason=row["stop_reason"],
        )

    def _expire_active_leases(
        self: Any, connection: sqlite3.Connection
    ) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT * FROM workflow_leases WHERE status = ?",
            (LeaseStatus.ACTIVE.value,),
        ).fetchall()
        expired_ids: list[str] = []
        for row in rows:
            if not lease_is_expired(row["expires_at"]):
                continue
            lease_id = str(row["lease_id"])
            timestamp = current_timestamp()
            connection.execute(
                """
                    UPDATE workflow_handoffs
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE lease_id = ? AND status NOT IN (?, ?)
                    """,
                (
                    HandoffStatus.STOPPED.value,
                    "Lease expired and requires operator recovery",
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
                    WHERE lease_id = ? AND status = ?
                    """,
                (
                    LeaseStatus.STOPPED.value,
                    "Lease expired and requires operator recovery",
                    timestamp,
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                    UPDATE workflow_repairs
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE lease_id = ? AND status = ?
                    """,
                (
                    RepairStatus.AWAITING_CLARIFICATION.value,
                    "Lease expired and requires operator recovery",
                    timestamp,
                    lease_id,
                    RepairStatus.RUNNING.value,
                ),
            )
            expired_ids.append(lease_id)
        return tuple(expired_ids)

    @staticmethod
    def _handoff_from_row(row: sqlite3.Row) -> HandoffRecord:
        return HandoffRecord(
            handoff_id=str(row["handoff_id"]),
            lease_id=str(row["lease_id"]),
            source_role=enum_role(row["source_role"]),
            target_role=enum_role(row["target_role"]),
            commit_sha=str(row["commit_sha"]),
            source_state=str(row["source_state"]),
            status=enum_status(row["status"]),
            checks=checks_from_json(row["checks_json"]),
            constitution_rules=tuple(
                cast(list[str], json.loads(str(row["constitution_json"])))
            ),
            required_action=row["required_action"],
            approval_actor=row["approval_actor"],
            approval_note=row["approval_note"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _repair_from_row(row: sqlite3.Row) -> RepairRecord:
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError as exc:
            raise WorkflowError("Stored repair evidence is invalid") from exc
        if not isinstance(evidence, dict):
            raise WorkflowError("Stored repair evidence is invalid")
        try:
            status = RepairStatus(str(row["status"]))
        except ValueError as exc:
            raise WorkflowError("Stored repair status is invalid") from exc
        return RepairRecord(
            repair_id=str(row["repair_id"]),
            pull_request_id=str(row["pull_request_id"]),
            repository=str(row["repository"]),
            pull_request_number=int(row["pull_request_number"]),
            source_branch=str(row["source_branch"]),
            target_branch=str(row["target_branch"]),
            expected_head=str(row["expected_head"]),
            target_head=str(row["target_head"]),
            repair_branch=str(row["repair_branch"]),
            worktree_path=str(row["worktree_path"]),
            lease_id=row["lease_id"],
            status=status,
            repaired_head=row["repaired_head"],
            checks=checks_from_json(row["checks_json"]),
            evidence=cast(Mapping[str, object], evidence),
            required_action=row["required_action"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
