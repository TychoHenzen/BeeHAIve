from __future__ import annotations

from typing import Any
from uuid import uuid4

from beehaiive.models import RunState, RunStatus, Stage

from .constants import MAX_AGENT_DIAGNOSTIC_LENGTH as MAX_AGENT_DIAGNOSTIC_LENGTH
from .errors import StoreError
from .helpers.claimability import (
    _task_claimability_for_run as _task_claimability_for_run,
)
from .helpers.lease_helpers import _now as _now


class RunFailureMixin:
    def fail(self: Any, run_id: str, error: str, lease_token: str) -> RunState:
        failed, _ = self.fail_with_transition(run_id, error, lease_token)
        return failed

    def fail_with_transition(
        self: Any,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
        route_failure: bool = False,
    ) -> tuple[RunState, bool]:
        if not error.strip():
            raise StoreError("A failure reason is required")
        if input_tokens < 0 or output_tokens < 0:
            raise StoreError("Token usage must not be negative")
        if recursive_spawn_depth < 0:
            raise StoreError("Recursive spawn depth must not be negative")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is RunStatus.COMPLETED:
                raise StoreError("A completed run cannot fail")
            if row.status is RunStatus.FAILED:
                return row, False
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (error, _now(), run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (error, row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                {"error": error},
            )
            if route_failure and row.stage is Stage.IMPLEMENT:
                connection.execute(
                    """
                    INSERT INTO routing_failure_outbox(
                        transition_id, run_id, error, input_tokens, output_tokens,
                        recursive_spawn_depth, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        error,
                        input_tokens,
                        output_tokens,
                        recursive_spawn_depth,
                        _now(),
                    ),
                )
            return self._run_for_id(connection, run_id) or row, True

    def fail_agent_run(
        self: Any,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        claimable: bool | None = None,
    ) -> RunState:
        """Persist a worker failure and release its lease for a later retry."""

        normalized_error = error.strip()
        if not normalized_error:
            raise StoreError("A failure reason is required")
        normalized_error = normalized_error[:MAX_AGENT_DIAGNOSTIC_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status is RunStatus.COMPLETED:
                raise StoreError("A completed run cannot fail")
            if row.status is RunStatus.FAILED:
                return row
            self._require_lease(row, lease_token)
            task_paused, answered_question = _task_claimability_for_run(
                connection, run_id
            )
            requested_claimable = (
                row.stage is not Stage.PULL_REQUEST if claimable is None else claimable
            )
            effective_claimable = (
                requested_claimable or answered_question
            ) and not task_paused
            now = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, last_result = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_error, now, run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(effective_claimable),
                    None if answered_question else normalized_error,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            details: dict[str, object] = {"error": normalized_error}
            if answered_question:
                details["superseded_by_answer"] = True
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                details,
            )
            return self._run_for_id(connection, run_id) or row

    def fail_agent_run_after_lease_loss(
        self: Any, run_id: str, error: str, *, expected_lease_token: str
    ) -> RunState:
        """Record a worker failure even when its lease can no longer be used."""

        normalized_error = error.strip()
        if not normalized_error:
            raise StoreError("A failure reason is required")
        if not expected_lease_token.strip():
            raise StoreError("A run lease token is required")
        normalized_error = normalized_error[:MAX_AGENT_DIAGNOSTIC_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return row
            task_paused, answered_question = _task_claimability_for_run(
                connection, run_id
            )
            effective_claimable = (
                row.stage is not Stage.PULL_REQUEST and not task_paused
            )
            now = _now()
            updated = connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, last_result = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'active' AND lease_token = ?
                """,
                (normalized_error, now, run_id, expected_lease_token),
            )
            if updated.rowcount == 0:
                return self._run_for_id(connection, run_id) or row
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(effective_claimable),
                    None if answered_question else normalized_error,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            details: dict[str, object] = {
                "error": normalized_error,
                "lease_lost": True,
            }
            if answered_question:
                details["superseded_by_answer"] = True
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                details,
            )
            return self._run_for_id(connection, run_id) or row

    def stop(self: Any, run_id: str, reason: str = "Stopped by operator") -> RunState:
        """Stop a run from an authenticated operator action."""

        if not reason.strip():
            raise StoreError("A stop reason is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return row
            now = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (reason, now, run_id),
            )
            connection.execute(
                """
                UPDATE operator_questions
                SET status = 'closed',
                    notification_status = CASE
                        WHEN notification_status IN (
                            'pending', 'not_configured', 'sending'
                        )
                        THEN 'cancelled' ELSE notification_status END,
                    notification_lease_token = NULL,
                    notification_lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (reason, row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "stopped",
                row.stage,
                row.stage,
                {"reason": reason},
            )
            return self._run_for_id(connection, run_id) or row


__all__ = ["RunFailureMixin"]
