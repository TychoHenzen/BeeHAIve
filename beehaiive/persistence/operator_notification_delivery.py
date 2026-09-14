from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from beehaiive.contract_types.validation import _redact_text as _redact_text

from .errors import StoreError
from .helpers.lease_helpers import _now as _now

MAX_OPERATOR_NOTIFICATION_ATTEMPTS = 3


class OperatorNotificationDeliveryMixin:
    def recover_interrupted_operator_notifications(self: Any) -> int:
        # ponytail: one API process per SQLite database; use a cross-process
        # lease coordinator if multiple API processes share this database.
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE operator_questions
                SET notification_status = 'pending',
                    notification_lease_token = NULL,
                    notification_lease_expires_at = NULL, updated_at = ?
                WHERE notification_status = 'sending'
                """,
                (_now(),),
            )
            return updated.rowcount

    def pending_operator_notification_ids(
        self: Any, limit: int = 25, run_id: str | None = None
    ) -> tuple[str, ...]:
        if limit < 1:
            raise StoreError("Notification batch limit must be positive")
        now = _now()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT question_id FROM operator_questions
                WHERE status = 'pending'
                  AND (notification_status IN ('pending', 'not_configured')
                    OR (notification_status = 'sending'
                        AND notification_lease_expires_at <= ?))
                  AND (? IS NULL OR run_id = ?)
                ORDER BY created_at, question_id LIMIT ?
                """,
                (now, run_id, run_id, limit),
            ).fetchall()
        return tuple(str(row["question_id"]) for row in rows)

    def mark_operator_notification_not_configured(
        self: Any, question_id: str, reason: str
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id, project_id, repository_name, pbi_number, revision,
                       status AS question_status,
                       notification_status, notification_lease_expires_at
                FROM operator_questions WHERE question_id = ?
                """,
                (question_id,),
            ).fetchone()
            if (
                row is None
                or row["question_status"] != "pending"
                or row["notification_status"] in {"delivered", "failed", "cancelled"}
            ):
                return False
            now = _now()
            if row["notification_status"] == "sending" and (
                row["notification_lease_expires_at"] is None
                or str(row["notification_lease_expires_at"]) > now
            ):
                return False
            run = self._run_for_id(connection, str(row["run_id"]))
            if run is None:
                return False
            safe_reason = _redact_text(reason, 500)
            connection.execute(
                """
                UPDATE operator_questions
                SET notification_status = 'not_configured',
                    notification_last_error = ?, notification_lease_token = NULL,
                    notification_lease_expires_at = NULL, updated_at = ?
                WHERE question_id = ?
                """,
                (safe_reason, now, question_id),
            )
            self._record_event(
                connection,
                run.project_id,
                run.repository,
                run.pbi_number,
                run.run_id,
                "operator_notification_not_configured",
                run.stage,
                run.stage,
                {
                    "question_id": question_id,
                    "revision": int(row["revision"]),
                    "reason": safe_reason,
                },
            )
            return True

    def claim_operator_notification(
        self: Any, question_id: str
    ) -> dict[str, object] | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id, project_id, repository_name, pbi_number, revision,
                       status AS question_status,
                       notification_status, notification_attempts,
                       notification_lease_expires_at
                FROM operator_questions WHERE question_id = ?
                """,
                (question_id,),
            ).fetchone()
            if row is None:
                return None
            if row["question_status"] != "pending":
                return None
            now = _now()
            status = str(row["notification_status"])
            lease_expires_at = row["notification_lease_expires_at"]
            if status == "sending" and (
                lease_expires_at is None or str(lease_expires_at) > now
            ):
                return None
            if status not in {"pending", "not_configured", "sending"}:
                return None
            attempts = int(row["notification_attempts"])
            if attempts >= MAX_OPERATOR_NOTIFICATION_ATTEMPTS:
                connection.execute(
                    """
                    UPDATE operator_questions
                    SET notification_status = 'failed',
                        notification_last_error = 'Maximum attempts reached',
                        notification_lease_token = NULL,
                        notification_lease_expires_at = NULL, updated_at = ?
                    WHERE question_id = ?
                    """,
                    (now, question_id),
                )
                run = self._run_for_id(connection, str(row["run_id"]))
                if run is not None:
                    self._record_event(
                        connection,
                        run.project_id,
                        run.repository,
                        run.pbi_number,
                        run.run_id,
                        "operator_notification_attempt_limit_reached",
                        run.stage,
                        run.stage,
                        {
                            "question_id": question_id,
                            "revision": int(row["revision"]),
                            "attempts": attempts,
                        },
                    )
                return None
            attempt = attempts + 1
            lease_token = str(uuid4())
            lease_expires = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            connection.execute(
                """
                UPDATE operator_questions
                SET notification_status = 'sending', notification_attempts = ?,
                    notification_last_attempt_at = ?,
                    notification_last_status_code = NULL,
                    notification_last_error = NULL, notification_lease_token = ?,
                    notification_lease_expires_at = ?, updated_at = ?
                WHERE question_id = ? AND notification_attempts = ?
                """,
                (attempt, now, lease_token, lease_expires, now, question_id, attempts),
            )
            run = self._run_for_id(connection, str(row["run_id"]))
            if run is None:
                raise StoreError("Notification run is unavailable")
            self._record_event(
                connection,
                run.project_id,
                run.repository,
                run.pbi_number,
                run.run_id,
                "operator_notification_attempt_started",
                run.stage,
                run.stage,
                {
                    "question_id": question_id,
                    "revision": int(row["revision"]),
                    "attempt": attempt,
                },
            )
            question = self._operator_question_by_id(connection, question_id)
            if question is None:
                raise StoreError("Claimed notification question could not be read")
            return {
                "question": question,
                "lease_token": lease_token,
                "attempt": attempt,
            }

    def complete_operator_notification_attempt(
        self: Any,
        question_id: str,
        lease_token: str,
        *,
        status_code: int | None,
        delivered: bool,
        retryable: bool,
        error: str | None,
    ) -> str | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id, revision, notification_status, notification_attempts,
                       notification_lease_token
                FROM operator_questions WHERE question_id = ?
                """,
                (question_id,),
            ).fetchone()
            if (
                row is None
                or row["notification_status"] != "sending"
                or row["notification_lease_token"] != lease_token
            ):
                return None
            attempts = int(row["notification_attempts"])
            status = (
                "delivered"
                if delivered
                else "pending"
                if retryable and attempts < MAX_OPERATOR_NOTIFICATION_ATTEMPTS
                else "failed"
            )
            now = _now()
            safe_error = (
                None
                if delivered
                else _redact_text(error or "Webhook delivery failed", 500)
            )
            connection.execute(
                """
                UPDATE operator_questions
                SET notification_status = ?, notification_last_status_code = ?,
                    notification_last_error = ?,
                    notification_delivered_at = CASE WHEN ? = 'delivered'
                        THEN ? ELSE notification_delivered_at END,
                    notification_lease_token = NULL,
                    notification_lease_expires_at = NULL, updated_at = ?
                WHERE question_id = ? AND notification_lease_token = ?
                """,
                (
                    status,
                    status_code,
                    safe_error,
                    status,
                    now,
                    now,
                    question_id,
                    lease_token,
                ),
            )
            run = self._run_for_id(connection, str(row["run_id"]))
            if run is None:
                raise StoreError("Notification run is unavailable")
            self._record_event(
                connection,
                run.project_id,
                run.repository,
                run.pbi_number,
                run.run_id,
                "operator_notification_attempt_result",
                run.stage,
                run.stage,
                {
                    "question_id": question_id,
                    "revision": int(row["revision"]),
                    "attempt": attempts,
                    "status": status,
                    "status_code": status_code,
                    "error": safe_error,
                },
            )
            return status


__all__ = ["OperatorNotificationDeliveryMixin"]
