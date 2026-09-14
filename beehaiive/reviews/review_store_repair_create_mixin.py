from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from .constants import MAX_REVIEW_REPAIR_FINDINGS
from .finding_publication_state import FindingPublicationState
from .finding_status import FindingStatus
from .helpers import current_timestamp, require_text
from .review_error import ReviewError
from .review_repair_status import ReviewRepairStatus
from .review_repair_transition_status import ReviewRepairTransitionStatus

if TYPE_CHECKING:
    from .review_repair_attempt import ReviewRepairAttempt
from typing import Any


class ReviewStoreRepairCreateMixin:
    def create_repair_attempt(
        self: Any, cycle_id: str, finding_ids: Iterable[str], actor: str
    ) -> tuple[ReviewRepairAttempt, bool]:
        cycle_id = require_text(cycle_id, "review cycle id")
        actor = require_text(actor, "review actor", 100)
        raw_ids = tuple(finding_ids)
        if not 1 <= len(raw_ids) <= MAX_REVIEW_REPAIR_FINDINGS:
            raise ReviewError(
                f"Select between 1 and {MAX_REVIEW_REPAIR_FINDINGS} findings"
            )
        selected = tuple(
            require_text(finding_id, "finding id", 200) for finding_id in raw_ids
        )
        if len(set(selected)) != len(selected):
            raise ReviewError("Duplicate finding identifiers are not allowed")

        with self.transaction() as connection:
            cycle = self.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            pull_request_id = str(cycle["pull_request_id"])
            existing = connection.execute(
                "SELECT * FROM review_repair_attempts WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if existing is not None:
                attempt = self.repair_attempt_from_row(existing)
                if attempt.finding_ids != selected:
                    raise ReviewError(
                        "A different repair selection already claimed this cycle"
                    )
                return attempt, False

            current = self.current_cycle_row(connection, pull_request_id)
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Findings must be selected from the current cycle")

            readers = self._readers(connection, cycle_id)
            cycle_finding_ids = {
                finding_id for reader in readers for finding_id in reader.finding_ids
            }
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                f"SELECT * FROM review_findings WHERE finding_id IN ({placeholders})",
                selected,
            ).fetchall()
            findings = {str(row["finding_id"]): row for row in rows}
            if len(findings) != len(selected):
                raise ReviewError("Selection contains an unknown review finding")
            for finding_id in selected:
                finding = findings[finding_id]
                if (
                    finding_id not in cycle_finding_ids
                    or str(finding["pull_request_id"]) != pull_request_id
                ):
                    raise ReviewError("Findings must belong to the current cycle")
                if bool(finding["stale"]) or str(finding["head_sha"] or "") != str(
                    cycle["head_sha"]
                ):
                    raise ReviewError("Stale findings cannot be selected")
                if str(finding["status"]) != FindingStatus.OPEN.value:
                    raise ReviewError("Only open findings can be selected")
                if (
                    str(finding["publication_state"])
                    != FindingPublicationState.PUBLISHED.value
                    or finding["duplicate_target"] is not None
                ):
                    raise ReviewError(
                        "Only published, non-duplicate findings can be selected"
                    )

            timestamp = current_timestamp()
            attempt_id = str(uuid4())
            connection.execute(
                """
                    INSERT INTO review_repair_attempts(
                        attempt_id, cycle_id, pull_request_id, head_sha,
                        finding_ids_json, actor, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    attempt_id,
                    cycle_id,
                    pull_request_id,
                    str(cycle["head_sha"]),
                    json.dumps(selected, separators=(",", ":")),
                    actor,
                    ReviewRepairStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_repair_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            assert row is not None
            return self.repair_attempt_from_row(row), True

    def repair_attempt(self: Any, attempt_id: str) -> ReviewRepairAttempt:
        attempt_id = require_text(attempt_id, "repair attempt id", 100)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM review_repair_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise ReviewError(f"Unknown repair attempt: {attempt_id}")
        return self.repair_attempt_from_row(row)

    def pending_repair_attempts(self: Any) -> tuple[ReviewRepairAttempt, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                    SELECT * FROM review_repair_attempts
                    WHERE status IN (?, ?, ?)
                       OR (status = ? AND review_transition_status IN (?, ?))
                    """,
                (
                    ReviewRepairStatus.QUEUED.value,
                    ReviewRepairStatus.RUNNING.value,
                    ReviewRepairStatus.PUSHING.value,
                    ReviewRepairStatus.SUCCEEDED.value,
                    ReviewRepairTransitionStatus.PENDING.value,
                    ReviewRepairTransitionStatus.RETRY_REQUIRED.value,
                ),
            ).fetchall()
        return tuple(self.repair_attempt_from_row(row) for row in rows)

    def claim_repair_attempt(self: Any, attempt_id: str) -> bool:
        with self.transaction() as connection:
            updated = connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET status = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = ? AND cancellation_requested = 0
                    """,
                (
                    ReviewRepairStatus.RUNNING.value,
                    current_timestamp(),
                    require_text(attempt_id, "repair attempt id", 100),
                    ReviewRepairStatus.QUEUED.value,
                ),
            )
            return updated.rowcount == 1

    def attach_repair_lease(self: Any, attempt_id: str, lease_id: str) -> bool:
        with self.transaction() as connection:
            updated = connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET lease_id = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = ? AND lease_id IS NULL
                        AND cancellation_requested = 0
                    """,
                (
                    require_text(lease_id, "workspace lease id", 100),
                    current_timestamp(),
                    require_text(attempt_id, "repair attempt id", 100),
                    ReviewRepairStatus.RUNNING.value,
                ),
            )
            return updated.rowcount == 1
