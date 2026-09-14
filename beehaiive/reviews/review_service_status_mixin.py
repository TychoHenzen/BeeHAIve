from __future__ import annotations

import sqlite3
from typing import Any

from .helpers import current_timestamp
from .reader_status import ReaderStatus
from .review_cycle_status import ReviewCycleStatus


class ReviewServiceStatusMixin:
    def _set_failed(self: Any, connection: sqlite3.Connection, cycle_id: str) -> None:
        connection.execute(
            """
                UPDATE review_cycles
                SET status = ?, required_action = ?, updated_at = ?
                WHERE cycle_id = ?
                """,
            (
                ReviewCycleStatus.FAILED.value,
                "Writer must resolve findings and start a new review cycle",
                current_timestamp(),
                cycle_id,
            ),
        )

    def _recompute_cycle(
        self: Any, connection: sqlite3.Connection, cycle_id: str
    ) -> None:
        statuses = [
            ReaderStatus(str(row["status"]))
            for row in connection.execute(
                "SELECT status FROM review_readers WHERE cycle_id = ?", (cycle_id,)
            ).fetchall()
        ]
        if any(status is ReaderStatus.FAIL for status in statuses):
            status = ReviewCycleStatus.FAILED
            required_action = (
                "Writer must resolve findings and start a new review cycle"
            )
        elif any(status is ReaderStatus.PENDING for status in statuses):
            status = ReviewCycleStatus.ACTIVE
            required_action = "Awaiting four specialized review readers"
        else:
            status = ReviewCycleStatus.PASSED
            required_action = None
        connection.execute(
            """
                UPDATE review_cycles
                SET status = ?, required_action = ?, updated_at = ?
                WHERE cycle_id = ?
                """,
            (status.value, required_action, current_timestamp(), cycle_id),
        )
