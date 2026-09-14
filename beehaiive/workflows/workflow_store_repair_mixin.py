from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from .helpers import (
    checks_to_json,
    current_timestamp,
    optional_text,
    require_path,
    require_text,
)
from .repair_status import RepairStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .check_result import CheckResult
    from .repair_record import RepairRecord
from typing import Any


class WorkflowStoreRepairMixin:
    def begin_repair(
        self: Any,
        repair_id: str,
        pull_request_id: str,
        repository: str,
        pull_request_number: int,
        source_branch: str,
        target_branch: str,
        expected_head: str,
        target_head: str,
        repair_branch: str,
        worktree_path: str,
        evidence: Mapping[str, object],
    ) -> RepairRecord:
        values = (
            require_text(repair_id, "repair id", 200),
            require_text(pull_request_id, "pull-request id", 300),
            require_text(repository, "repository", 300),
            pull_request_number,
            optional_text(source_branch),
            optional_text(target_branch),
            optional_text(expected_head, 200),
            optional_text(target_head, 200),
            require_text(repair_branch, "repair branch", 400),
            require_path(worktree_path, "repair worktree", 1_000),
            RepairStatus.RUNNING.value,
            checks_to_json(()),
            json.dumps(dict(evidence), sort_keys=True),
            current_timestamp(),
            current_timestamp(),
        )
        if pull_request_number <= 0:
            raise WorkflowError("Pull-request number must be positive")
        with self._transaction() as connection:
            existing = connection.execute(
                """
                    SELECT * FROM workflow_repairs
                    WHERE pull_request_id = ? AND expected_head = ?
                    """,
                (values[1], values[6]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                        INSERT INTO workflow_repairs(
                            repair_id, pull_request_id, repository, pull_request_number,
                            source_branch, target_branch, expected_head, target_head,
                            repair_branch, worktree_path, lease_id, status,
                            repaired_head,
                            checks_json, evidence_json, required_action, created_at,
                            updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL,
                            ?, ?
                        )
                        """,
                    values,
                )
            else:
                return self._repair_from_row(existing)
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?",
                (values[0],),
            ).fetchone()
        if row is None:
            raise WorkflowError("Repair record was not persisted")
        return self._repair_from_row(row)

    def attach_repair_workspace(
        self: Any, repair_id: str, lease_id: str, worktree_path: str
    ) -> RepairRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown repair: {repair_id}")
            current_lease = row["lease_id"]
            if current_lease is not None and current_lease != lease_id:
                raise WorkflowError("Repair already has a different workspace lease")
            connection.execute(
                """
                    UPDATE workflow_repairs
                    SET lease_id = ?, worktree_path = ?, updated_at = ?
                    WHERE repair_id = ? AND status = ?
                    """,
                (
                    lease_id,
                    require_path(worktree_path, "repair worktree", 1_000),
                    current_timestamp(),
                    repair_id,
                    RepairStatus.RUNNING.value,
                ),
            )
        record = self.get_repair(repair_id)
        if record is None:
            raise WorkflowError("Repair record disappeared")
        return record

    def finish_repair(
        self: Any,
        repair_id: str,
        status: RepairStatus,
        required_action: str | None,
        evidence: Mapping[str, object],
        checks: Sequence[CheckResult] = (),
        repaired_head: str | None = None,
    ) -> RepairRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown repair: {repair_id}")
            current = RepairStatus(str(row["status"]))
            if current is not RepairStatus.RUNNING:
                return self._repair_from_row(row)
            connection.execute(
                """
                    UPDATE workflow_repairs
                    SET status = ?, repaired_head = ?, checks_json = ?,
                        evidence_json = ?, required_action = ?, updated_at = ?
                    WHERE repair_id = ? AND status = ?
                    """,
                (
                    status.value,
                    repaired_head,
                    checks_to_json(checks),
                    json.dumps(dict(evidence), sort_keys=True),
                    required_action,
                    current_timestamp(),
                    repair_id,
                    RepairStatus.RUNNING.value,
                ),
            )
        record = self.get_repair(repair_id)
        if record is None:
            raise WorkflowError("Repair record disappeared")
        return record

    def get_repair(self: Any, repair_id: str) -> RepairRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
        return None if row is None else self._repair_from_row(row)

    def get_repair_for_identity(
        self: Any, pull_request_id: str, expected_head: str
    ) -> RepairRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                    SELECT * FROM workflow_repairs
                    WHERE pull_request_id = ? AND expected_head = ?
                    """,
                (pull_request_id, expected_head),
            ).fetchone()
        return None if row is None else self._repair_from_row(row)
