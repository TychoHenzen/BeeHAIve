from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from .constants import REQUIRED_CONCERNS
from .finding_status import FindingStatus
from .helpers import claim_is_active, current_timestamp
from .reader_status import ReaderStatus
from .review_cycle_status import ReviewCycleStatus
from .review_error import ReviewError
from .review_repair_transition_status import ReviewRepairTransitionStatus
from .review_snapshot import ReviewSnapshot

if TYPE_CHECKING:
    from .reader_result import ReaderResult
    from .review_concern import ReviewConcern
    from .review_finding import ReviewFinding
from typing import Any


class ReviewStoreCycleMixin:
    def cycle_row(
        self: Any, connection: sqlite3.Connection, cycle_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM review_cycles WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()

    def current_cycle_row(
        self: Any, connection: sqlite3.Connection, pull_request_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
                SELECT * FROM review_cycles
                WHERE pull_request_id = ?
                ORDER BY cycle_number DESC
                LIMIT 1
                """,
            (pull_request_id,),
        ).fetchone()

    def current_cycle_id(self: Any, pull_request_id: str) -> str | None:
        with self._lock:
            row = self.current_cycle_row(self._connection, pull_request_id)
        return None if row is None else str(row["cycle_id"])

    def claim_reader(self: Any, cycle_id: str, concern: ReviewConcern) -> str | None:
        with self.transaction() as connection:
            cycle = self.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            current = self.current_cycle_row(connection, str(cycle["pull_request_id"]))
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Reader result belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting reader results")
            reader = connection.execute(
                """
                    SELECT status, claim_token, claim_expires_at
                    FROM review_readers
                    WHERE cycle_id = ? AND concern = ?
                    """,
                (cycle_id, concern.value),
            ).fetchone()
            if reader is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            if ReaderStatus(str(reader["status"])) is not ReaderStatus.PENDING:
                return None
            if claim_is_active(reader["claim_expires_at"]):
                return None
            claim_token = str(uuid4())
            claim_expires_at = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
            connection.execute(
                """
                    UPDATE review_readers
                    SET claim_token = ?, claim_expires_at = ?, updated_at = ?
                    WHERE cycle_id = ? AND concern = ? AND status = ?
                    """,
                (
                    claim_token,
                    claim_expires_at,
                    current_timestamp(),
                    cycle_id,
                    concern.value,
                    ReaderStatus.PENDING.value,
                ),
            )
            return claim_token

    def _readers(
        self: Any, connection: sqlite3.Connection, cycle_id: str
    ) -> tuple[ReaderResult, ...]:
        rows = connection.execute(
            "SELECT * FROM review_readers WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchall()
        readers = tuple(self._reader_from_row(row) for row in rows)
        return tuple(
            sorted(readers, key=lambda reader: REQUIRED_CONCERNS.index(reader.concern))
        )

    def _findings(
        self: Any, connection: sqlite3.Connection, pull_request_id: str
    ) -> tuple[ReviewFinding, ...]:
        rows = connection.execute(
            """
                SELECT * FROM review_findings
                WHERE pull_request_id = ?
                ORDER BY created_at, finding_id
                """,
            (pull_request_id,),
        ).fetchall()
        return tuple(self._finding_from_row(row) for row in rows)

    def snapshot(self: Any, cycle_id: str) -> ReviewSnapshot:
        with self._lock:
            return self.snapshot_in_connection(self._connection, cycle_id)

    def snapshot_in_connection(
        self: Any, connection: sqlite3.Connection, cycle_id: str
    ) -> ReviewSnapshot:
        cycle_row = self.cycle_row(connection, cycle_id)
        if cycle_row is None:
            raise ReviewError(f"Unknown review cycle: {cycle_id}")
        cycle = self._cycle_from_row(cycle_row)
        readers = self._readers(connection, cycle_id)
        findings = self._findings(connection, cycle.pull_request_id)
        merge_allowed = cycle.status is ReviewCycleStatus.HUMAN_APPROVED or (
            cycle.status is ReviewCycleStatus.PASSED
            and not any(finding.status is FindingStatus.OPEN for finding in findings)
        )
        repair_row = connection.execute(
            """
                SELECT * FROM review_repair_attempts
                WHERE cycle_id = ? OR review_transition_cycle_id = ?
                   OR (pull_request_id = ? AND review_transition_status IN (?, ?, ?))
                ORDER BY updated_at DESC LIMIT 1
                """,
            (
                cycle_id,
                cycle_id,
                cycle.pull_request_id,
                ReviewRepairTransitionStatus.PENDING.value,
                ReviewRepairTransitionStatus.RETRY_REQUIRED.value,
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED.value,
            ),
        ).fetchone()
        repair_attempt = (
            None if repair_row is None else self.repair_attempt_from_row(repair_row)
        )
        return ReviewSnapshot(cycle, readers, findings, merge_allowed, repair_attempt)

    def current_snapshot(self: Any, pull_request_id: str) -> ReviewSnapshot:
        with self._lock:
            row = self.current_cycle_row(self._connection, pull_request_id)
        if row is None:
            raise ReviewError(f"No review cycle exists for {pull_request_id}")
        return self.snapshot(str(row["cycle_id"]))

    def pull_request_id_for_cycle(self: Any, cycle_id: str) -> str:
        with self._lock:
            row = self.cycle_row(self._connection, cycle_id)
        if row is None:
            raise ReviewError(f"Unknown review cycle: {cycle_id}")
        return str(row["pull_request_id"])

    def pull_request_id_for_finding(self: Any, finding_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT pull_request_id FROM review_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            raise ReviewError(f"Unknown review finding: {finding_id}")
        return str(row["pull_request_id"])

    def finding_for_id(self: Any, finding_id: str) -> ReviewFinding:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        if row is None:
            raise ReviewError(f"Unknown review finding: {finding_id}")
        return self._finding_from_row(row)
