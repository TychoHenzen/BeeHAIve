from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .helpers import current_timestamp, require_text
from .review_error import ReviewError
from .review_repair_status import ReviewRepairStatus
from .review_repair_transition_status import ReviewRepairTransitionStatus

if TYPE_CHECKING:
    from .review_repair_attempt import ReviewRepairAttempt
from typing import Any


class ReviewStoreRepairTransitionMixin:
    def begin_repair_push(
        self: Any,
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: Mapping[str, object],
    ) -> bool:
        evidence_json = json.dumps(dict(push_evidence), separators=(",", ":"))
        if len(evidence_json) > 4_000:
            raise ReviewError("Repair push evidence exceeds the storage limit")
        with self.transaction() as connection:
            updated = connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET status = ?, commit_sha = ?,
                        push_evidence_json = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = ? AND lease_id = ?
                        AND cancellation_requested = 0
                    """,
                (
                    ReviewRepairStatus.PUSHING.value,
                    require_text(commit_sha, "repair commit sha", 100),
                    evidence_json,
                    current_timestamp(),
                    require_text(attempt_id, "repair attempt id", 100),
                    ReviewRepairStatus.RUNNING.value,
                    require_text(lease_id, "workspace lease id", 100),
                ),
            )
            return updated.rowcount == 1

    def request_repair_cancellation(self: Any, attempt_id: str) -> ReviewRepairAttempt:
        attempt_id = require_text(attempt_id, "repair attempt id", 100)
        with self.transaction() as connection:
            updated = connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET status = CASE WHEN status = ? THEN ? ELSE status END,
                        cancellation_requested = 1, updated_at = ?
                    WHERE attempt_id = ? AND status IN (?, ?)
                    """,
                (
                    ReviewRepairStatus.QUEUED.value,
                    ReviewRepairStatus.CANCELLED.value,
                    current_timestamp(),
                    attempt_id,
                    ReviewRepairStatus.QUEUED.value,
                    ReviewRepairStatus.RUNNING.value,
                ),
            )
            if updated.rowcount == 0:
                row = connection.execute(
                    "SELECT attempt_id FROM review_repair_attempts "
                    "WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    raise ReviewError(f"Unknown repair attempt: {attempt_id}")
        return self.repair_attempt(attempt_id)

    def finish_repair_attempt(
        self: Any,
        attempt_id: str,
        status: ReviewRepairStatus,
        *,
        commit_sha: str | None = None,
        push_evidence: Mapping[str, object] | None = None,
        result: str | None = None,
        required_action: str | None = None,
    ) -> ReviewRepairAttempt:
        if status not in {
            ReviewRepairStatus.SUCCEEDED,
            ReviewRepairStatus.FAILED,
            ReviewRepairStatus.CANCELLED,
            ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
        }:
            raise ReviewError("Repair attempt must finish in a terminal state")
        evidence_json = (
            None
            if push_evidence is None
            else json.dumps(dict(push_evidence), separators=(",", ":"))
        )
        if evidence_json is not None and len(evidence_json) > 4_000:
            raise ReviewError("Repair push evidence exceeds the storage limit")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM review_repair_attempts WHERE attempt_id = ?",
                (require_text(attempt_id, "repair attempt id", 100),),
            ).fetchone()
            if row is None:
                raise ReviewError(f"Unknown repair attempt: {attempt_id}")
            if str(row["status"]) in {
                ReviewRepairStatus.SUCCEEDED.value,
                ReviewRepairStatus.FAILED.value,
                ReviewRepairStatus.CANCELLED.value,
                ReviewRepairStatus.HUMAN_ACTION_REQUIRED.value,
            }:
                return self.repair_attempt(attempt_id)
            connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET status = ?, commit_sha = ?, push_evidence_json = ?, result = ?,
                        required_action = ?, updated_at = ?,
                        review_transition_status = ?, review_transition_cycle_id = NULL,
                        review_transition_required_action = NULL
                    WHERE attempt_id = ?
                    """,
                (
                    status.value,
                    commit_sha,
                    evidence_json,
                    None if result is None else result[:4_000],
                    None if required_action is None else required_action[:1_000],
                    current_timestamp(),
                    (
                        ReviewRepairTransitionStatus.PENDING.value
                        if status is ReviewRepairStatus.SUCCEEDED
                        else ReviewRepairTransitionStatus.NOT_REQUIRED.value
                    ),
                    attempt_id,
                ),
            )
        return self.repair_attempt(attempt_id)

    def fail_repair_transition(
        self: Any,
        attempt_id: str,
        status: ReviewRepairTransitionStatus,
        required_action: str,
    ) -> ReviewRepairAttempt:
        if status not in {
            ReviewRepairTransitionStatus.RETRY_REQUIRED,
            ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED,
        }:
            raise ReviewError("Repair transition failure status is invalid")
        with self.transaction() as connection:
            connection.execute(
                """
                    UPDATE review_repair_attempts
                    SET review_transition_status = ?,
                        review_transition_required_action = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = ?
                        AND review_transition_cycle_id IS NULL
                        AND review_transition_status IN (?, ?)
                    """,
                (
                    status.value,
                    require_text(required_action, "repair transition action", 1_000),
                    current_timestamp(),
                    require_text(attempt_id, "repair attempt id", 100),
                    ReviewRepairStatus.SUCCEEDED.value,
                    ReviewRepairTransitionStatus.PENDING.value,
                    ReviewRepairTransitionStatus.RETRY_REQUIRED.value,
                ),
            )
        return self.repair_attempt(attempt_id)
