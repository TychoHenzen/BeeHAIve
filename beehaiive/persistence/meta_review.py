from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .constants import MAX_META_REVIEW_SUGGESTIONS as MAX_META_REVIEW_SUGGESTIONS
from .constants import MAX_META_REVIEW_TEXT_LENGTH as MAX_META_REVIEW_TEXT_LENGTH
from .constants import META_REVIEW_LEASE_SECONDS as META_REVIEW_LEASE_SECONDS
from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class MetaReviewMixin:
    def begin_meta_review(
        self: Any,
        review_id: str,
        project_id: str,
        record_limit: int,
        input_token_limit: int,
    ) -> None:
        if not review_id.strip() or not project_id.strip():
            raise StoreError("A meta-review id and project id are required")
        with self._transaction() as connection:
            project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise StoreError(f"Unknown project: {project_id}")
            now = _now()
            stale_before = (
                datetime.now(UTC) - timedelta(seconds=META_REVIEW_LEASE_SECONDS)
            ).isoformat()
            connection.execute(
                """
                UPDATE meta_review_runs
                SET status = 'failed', error = ?, completed_at = ?
                WHERE status = 'running' AND created_at < ?
                """,
                ("Meta-review lease expired", now, stale_before),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO meta_review_runs(
                        review_id, project_id, status, record_limit,
                        input_token_limit, created_at
                    ) VALUES (?, ?, 'running', ?, ?, ?)
                    """,
                    (review_id, project_id, record_limit, input_token_limit, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("A meta-review is already running") from exc

    def finish_meta_review(
        self: Any,
        review_id: str,
        status: str,
        selected_records: int,
        input_tokens: int,
        missing_evidence: list[str],
        error: str | None = None,
    ) -> dict[str, object]:
        if status not in {"completed", "failed"}:
            raise StoreError(f"Invalid meta-review status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE meta_review_runs
                SET status = ?, selected_records = ?, input_tokens = ?,
                    missing_evidence_json = ?, error = ?, completed_at = ?
                WHERE review_id = ? AND status = 'running'
                """,
                (
                    status,
                    selected_records,
                    input_tokens,
                    json.dumps(missing_evidence, sort_keys=True),
                    error[:MAX_META_REVIEW_TEXT_LENGTH] if error else None,
                    _now(),
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM meta_review_runs WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown meta-review: {review_id}")
            return self._meta_review_run_from_row(row)

    def complete_meta_review(
        self: Any,
        review_id: str,
        selected_records: int,
        input_tokens: int,
        missing_evidence: list[str],
        suggestions: list[dict[str, object]],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        with self._transaction() as connection:
            saved = self._save_meta_review_suggestions(
                connection, review_id, suggestions
            )
            now = _now()
            updated = connection.execute(
                """
                UPDATE meta_review_runs
                SET status = 'completed', selected_records = ?, input_tokens = ?,
                    missing_evidence_json = ?, completed_at = ?
                WHERE review_id = ? AND status = 'running'
                """,
                (
                    selected_records,
                    input_tokens,
                    json.dumps(missing_evidence, sort_keys=True),
                    now,
                    review_id,
                ),
            )
            if updated.rowcount != 1:
                raise StoreError("Meta-review is not running")
            row = connection.execute(
                "SELECT * FROM meta_review_runs WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the update
                raise StoreError(f"Unknown meta-review: {review_id}")
            return self._meta_review_run_from_row(row), saved

    def save_meta_review_suggestions(
        self: Any, review_id: str, suggestions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with self._transaction() as connection:
            return self._save_meta_review_suggestions(
                connection, review_id, suggestions
            )

    def _save_meta_review_suggestions(
        self: Any,
        connection: sqlite3.Connection,
        review_id: str,
        suggestions: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        review = connection.execute(
            "SELECT project_id FROM meta_review_runs WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if review is None:
            raise StoreError(f"Unknown meta-review: {review_id}")
        project_id = str(review["project_id"])
        now = _now()
        for suggestion in suggestions:
            connection.execute(
                """
                INSERT INTO meta_review_suggestions(
                    suggestion_id, project_id, suggestion_key,
                    proposed_outcome, rationale, evidence_refs_json,
                    status, created_at, updated_at, last_review_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(project_id, suggestion_key) DO UPDATE SET
                    proposed_outcome = excluded.proposed_outcome,
                    rationale = excluded.rationale,
                    evidence_refs_json = excluded.evidence_refs_json,
                    updated_at = excluded.updated_at,
                    last_review_id = excluded.last_review_id
                """,
                (
                    suggestion["suggestion_id"],
                    project_id,
                    suggestion["suggestion_key"],
                    suggestion["proposed_outcome"],
                    suggestion["rationale"],
                    json.dumps(suggestion["evidence_refs"], sort_keys=True),
                    now,
                    now,
                    review_id,
                ),
            )
        rows = connection.execute(
            """
            SELECT * FROM meta_review_suggestions
            WHERE project_id = ? AND last_review_id = ?
            ORDER BY created_at, suggestion_id
            """,
            (project_id, review_id),
        ).fetchall()
        return [self._meta_review_suggestion_from_row(row) for row in rows]

    def meta_review_suggestions(
        self: Any, project_id: str, status: str | None = None
    ) -> list[dict[str, object]]:
        if not project_id.strip():
            raise StoreError("A project id is required")
        if status is not None and status not in {"pending", "accepted", "rejected"}:
            raise StoreError(f"Invalid suggestion status: {status}")
        with self._lock:
            query = "SELECT * FROM meta_review_suggestions WHERE project_id = ?"
            parameters: list[object] = [project_id]
            if status is not None:
                query += " AND status = ?"
                parameters.append(status)
            query += " ORDER BY created_at, suggestion_id LIMIT ?"
            parameters.append(MAX_META_REVIEW_SUGGESTIONS)
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._meta_review_suggestion_from_row(row) for row in rows]

    def decide_meta_review_suggestion(
        self: Any, project_id: str, suggestion_id: str, status: str
    ) -> dict[str, object]:
        if status not in {"accepted", "rejected"}:
            raise StoreError(f"Invalid suggestion decision: {status}")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM meta_review_suggestions
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (project_id, suggestion_id),
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown meta-review suggestion: {suggestion_id}")
            connection.execute(
                """
                UPDATE meta_review_suggestions
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (status, _now(), project_id, suggestion_id),
            )
            updated = connection.execute(
                """
                SELECT * FROM meta_review_suggestions
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (project_id, suggestion_id),
            ).fetchone()
            if updated is None:  # pragma: no cover - guarded by the update
                raise StoreError(f"Unknown meta-review suggestion: {suggestion_id}")
            return self._meta_review_suggestion_from_row(updated)


__all__ = ["MetaReviewMixin"]
