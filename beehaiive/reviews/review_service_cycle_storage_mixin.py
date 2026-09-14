from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from .constants import REQUIRED_CONCERNS
from .helpers import current_timestamp, require_text
from .reader_status import ReaderStatus
from .review_cycle_status import ReviewCycleStatus
from .review_error import ReviewError

if TYPE_CHECKING:
    from .review_snapshot import ReviewSnapshot
from typing import Any


class ReviewServiceCycleStorageMixin:
    def _start_cycle(
        self: Any,
        pull_request_id: str,
        head_sha: str,
        *,
        expected_cycle_id: str | None,
        github_evidence_json: str | None = None,
    ) -> ReviewSnapshot:
        with self.store.transaction() as connection:
            cycle_id = self._start_cycle_in_transaction(
                connection,
                pull_request_id,
                head_sha,
                expected_cycle_id=expected_cycle_id,
                github_evidence_json=github_evidence_json,
            )
        return self.store.snapshot(cycle_id)

    def _start_cycle_in_transaction(
        self: Any,
        connection: sqlite3.Connection,
        pull_request_id: str,
        head_sha: str,
        *,
        expected_cycle_id: str | None,
        github_evidence_json: str | None = None,
    ) -> str:
        current = self.store.current_cycle_row(connection, pull_request_id)
        current_cycle_id = None if current is None else str(current["cycle_id"])
        if current_cycle_id != expected_cycle_id:
            if (
                current is not None
                and str(current["head_sha"]) == head_sha
                and ReviewCycleStatus(str(current["status"]))
                is not ReviewCycleStatus.FAILED
            ):
                assert current_cycle_id is not None
                return current_cycle_id
            raise ReviewError("Review cycle changed while starting a new cycle")
        if current is not None:
            current_status = ReviewCycleStatus(str(current["status"]))
            if (
                str(current["head_sha"]) == head_sha
                and current_status is not ReviewCycleStatus.FAILED
            ):
                return str(current["cycle_id"])
            connection.execute(
                """
                    UPDATE review_cycles
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE cycle_id = ?
                    """,
                (
                    ReviewCycleStatus.SUPERSEDED.value,
                    "A newer review cycle must authorize merge",
                    current_timestamp(),
                    str(current["cycle_id"]),
                ),
            )
            cycle_number = int(current["cycle_number"]) + 1
        else:
            cycle_number = 1
        cycle_id = str(uuid4())
        timestamp = current_timestamp()
        connection.execute(
            """
                UPDATE review_findings
                SET stale = 1, updated_at = ?
                WHERE pull_request_id = ? AND head_sha != ? AND stale = 0
                """,
            (timestamp, pull_request_id, head_sha),
        )
        connection.execute(
            """
                INSERT INTO review_cycles(
                    cycle_id, pull_request_id, head_sha, cycle_number, status,
                    human_approval, required_action, github_evidence_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
            (
                cycle_id,
                pull_request_id,
                head_sha,
                cycle_number,
                ReviewCycleStatus.ACTIVE.value,
                "Awaiting four specialized review readers",
                github_evidence_json,
                timestamp,
                timestamp,
            ),
        )
        for concern in REQUIRED_CONCERNS:
            connection.execute(
                """
                    INSERT INTO review_readers(
                        cycle_id, concern, status, finding_ids_json, reader, updated_at
                    ) VALUES (?, ?, ?, '[]', 'automated', ?)
                    """,
                (cycle_id, concern.value, ReaderStatus.PENDING.value, timestamp),
            )
        return cycle_id

    def snapshot(self: Any, pull_request_id: str) -> ReviewSnapshot:
        return self.store.current_snapshot(
            require_text(pull_request_id, "pull request id")
        )
