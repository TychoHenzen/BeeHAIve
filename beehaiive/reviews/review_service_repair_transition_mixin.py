from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .helpers import current_timestamp, require_text
from .review_error import ReviewError
from .review_repair_status import ReviewRepairStatus
from .review_repair_transition_status import ReviewRepairTransitionStatus

if TYPE_CHECKING:
    from .pull_request_target import PullRequestTarget
    from .review_repair_attempt import ReviewRepairAttempt
from typing import Any


class ReviewServiceRepairTransitionMixin:
    def start_repair_followup_cycle(
        self: Any, attempt_id: str, target: PullRequestTarget
    ) -> ReviewRepairAttempt:
        attempt_id = require_text(attempt_id, "repair attempt id", 100)
        initial_attempt = self.store.repair_attempt(attempt_id)
        target = self._validate_provider_target(initial_attempt.pull_request_id, target)
        if target.head_sha != initial_attempt.commit_sha:
            raise ReviewError("Review head does not match the pushed repair")

        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM review_repair_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ReviewError(f"Unknown repair attempt: {attempt_id}")
            attempt = self.store.repair_attempt_from_row(row)
            if attempt.status is not ReviewRepairStatus.SUCCEEDED:
                raise ReviewError("Only a successful repair can start a review cycle")
            if (
                attempt.review_transition_status
                is ReviewRepairTransitionStatus.COMPLETED
            ):
                return attempt
            if attempt.review_transition_status not in {
                ReviewRepairTransitionStatus.PENDING,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
            }:
                return attempt
            cycle_id, action = self._resolve_repair_followup_cycle(
                connection, attempt, target
            )
            if action is not None:
                return self._require_repair_human_action(connection, attempt_id, action)
            assert cycle_id is not None
            return self._complete_repair_transition(connection, attempt_id, cycle_id)

    def _resolve_repair_followup_cycle(
        self: Any,
        connection: sqlite3.Connection,
        attempt: ReviewRepairAttempt,
        target: PullRequestTarget,
    ) -> tuple[str | None, str | None]:
        commit_sha = attempt.commit_sha
        evidence = attempt.push_evidence
        if (
            commit_sha is None
            or commit_sha == attempt.head_sha
            or evidence is None
            or evidence.get("expected_head") != attempt.head_sha
            or evidence.get("pushed_head") != commit_sha
            or target.head_sha != commit_sha
        ):
            return None, (
                "Verify the repair commit and current pull-request head before "
                "starting a fresh review cycle."
            )

        current = self.store.current_cycle_row(connection, attempt.pull_request_id)
        if current is None:
            return None, (
                "The repair's review cycle is unavailable. Verify the pull-request "
                "head and start a fresh review cycle manually."
            )
        current_cycle_id = str(current["cycle_id"])
        if current_cycle_id != attempt.cycle_id:
            if str(current["head_sha"]) != commit_sha:
                return None, (
                    "A newer review cycle superseded this repair. Verify the "
                    "pull-request head and start a fresh review cycle manually."
                )
            return current_cycle_id, None
        return (
            self._start_cycle_in_transaction(
                connection,
                attempt.pull_request_id,
                commit_sha,
                expected_cycle_id=attempt.cycle_id,
                github_evidence_json=target.evidence_json,
            ),
            None,
        )

    def _complete_repair_transition(
        self: Any, connection: sqlite3.Connection, attempt_id: str, cycle_id: str
    ) -> ReviewRepairAttempt:
        connection.execute(
            """
                UPDATE review_repair_attempts
                SET review_transition_status = ?, review_transition_cycle_id = ?,
                    review_transition_required_action = NULL, updated_at = ?
                WHERE attempt_id = ? AND review_transition_cycle_id IS NULL
                """,
            (
                ReviewRepairTransitionStatus.COMPLETED.value,
                cycle_id,
                current_timestamp(),
                attempt_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM review_repair_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert updated is not None
        return self.store.repair_attempt_from_row(updated)

    def _require_repair_human_action(
        self: Any,
        connection: sqlite3.Connection,
        attempt_id: str,
        action: str,
    ) -> ReviewRepairAttempt:
        connection.execute(
            """
                UPDATE review_repair_attempts
                SET review_transition_status = ?,
                    review_transition_required_action = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
            (
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED.value,
                action[:1_000],
                current_timestamp(),
                attempt_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM review_repair_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert updated is not None
        return self.store.repair_attempt_from_row(updated)
