from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

from .errors import StoreError
from .helpers.lease_helpers import _now

MAX_STATION_ISSUES = 100
MAX_STATION_ATTEMPTS = 50
MAX_STATION_TEXT_LENGTH = 4_000
MAX_STATION_NOTE_LENGTH = 500
_ISSUE_STATUSES = frozenset({"open", "dismissed", "addressed", "resolved"})
_ISSUE_ACTIONS = frozenset({"dismiss", "address", "resolve"})


class StationIssueMixin:
    def ensure_station_issue(
        self: Any,
        issue_id: str,
        project_id: str,
        repository_name: str | None,
        pbi_number: int | None,
        run_id: str | None,
        station_id: str,
        explanation: str,
    ) -> dict[str, object]:
        issue_id = _bounded_identifier(issue_id, "Issue ID", 160)
        project_id = _bounded_identifier(project_id, "Project ID", 500)
        station_id = _bounded_identifier(station_id, "Station ID", 80)
        explanation = _safe_text(explanation, MAX_STATION_TEXT_LENGTH, "Explanation")
        repository_name = _optional_text(repository_name, 500)
        run_id = _optional_text(run_id, 200)
        if pbi_number is not None and (type(pbi_number) is not int or pbi_number <= 0):
            raise StoreError("PBI number must be positive")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM station_issues WHERE issue_id = ? AND project_id = ?",
                (issue_id, project_id),
            ).fetchone()
            now = _now()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO station_issues(
                        issue_id, project_id, repository_name, pbi_number, run_id,
                        station_id, explanation, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        issue_id,
                        project_id,
                        repository_name,
                        pbi_number,
                        run_id,
                        station_id,
                        explanation,
                        now,
                        now,
                    ),
                )
            elif str(row["status"]) not in {"dismissed", "resolved"}:
                connection.execute(
                    """
                    UPDATE station_issues
                    SET repository_name = ?, pbi_number = ?, run_id = ?,
                        station_id = ?, explanation = ?, updated_at = ?
                    WHERE issue_id = ? AND project_id = ?
                    """,
                    (
                        repository_name,
                        pbi_number,
                        run_id,
                        station_id,
                        explanation,
                        now,
                        issue_id,
                        project_id,
                    ),
                )
            return self._station_issue_for_connection(connection, issue_id, project_id)

    def station_issues_for_project(
        self: Any, project_id: str, *, limit: int = MAX_STATION_ISSUES
    ) -> list[dict[str, object]]:
        if type(limit) is not int or not 1 <= limit <= MAX_STATION_ISSUES:
            raise StoreError(f"Issue limit must be between 1 and {MAX_STATION_ISSUES}")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM station_issues
                WHERE project_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            return [
                self._station_issue_for_connection(
                    self._connection, str(row["issue_id"]), project_id
                )
                for row in rows
            ]

    def station_issue_action(
        self: Any,
        project_id: str,
        issue_id: str,
        action: str,
        actor: str,
        note: str = "",
    ) -> dict[str, object]:
        if action not in _ISSUE_ACTIONS:
            raise StoreError("Unsupported station issue action")
        actor = _bounded_identifier(actor, "Actor", 200)
        note = _safe_text(
            note or "Operator action recorded", MAX_STATION_NOTE_LENGTH, "Note"
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM station_issues WHERE issue_id = ? AND project_id = ?",
                (issue_id, project_id),
            ).fetchone()
            if row is None:
                raise StoreError("Station issue not found")
            status = str(row["status"])
            if status not in _ISSUE_STATUSES:
                raise StoreError("Station issue has an invalid status")
            if status == "resolved":
                raise StoreError("Resolved station issues cannot be changed")
            now = _now()
            connection.execute(
                """
                INSERT INTO station_issue_attempts(
                    attempt_id, issue_id, action, actor, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), issue_id, action, actor, note, now),
            )
            next_status = {
                "dismiss": "dismissed",
                "address": "addressed",
                "resolve": "resolved",
            }[action]
            connection.execute(
                "UPDATE station_issues SET status = ?, updated_at = ? "
                "WHERE issue_id = ? AND project_id = ?",
                (next_status, now, issue_id, project_id),
            )
            return self._station_issue_for_connection(connection, issue_id, project_id)

    @staticmethod
    def _station_issue_for_connection(
        connection: sqlite3.Connection, issue_id: str, project_id: str
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM station_issues WHERE issue_id = ? AND project_id = ?",
            (issue_id, project_id),
        ).fetchone()
        if row is None:
            raise StoreError("Station issue not found")
        attempts = connection.execute(
            """
            SELECT action, actor, note, created_at
            FROM (
                SELECT action, actor, note, created_at
                FROM station_issue_attempts
                WHERE issue_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (issue_id, MAX_STATION_ATTEMPTS),
        ).fetchall()
        return {
            "id": str(row["issue_id"]),
            "project_id": str(row["project_id"]),
            "repository": row["repository_name"],
            "pbi_number": row["pbi_number"],
            "run_id": row["run_id"],
            "station_id": str(row["station_id"]),
            "explanation": str(row["explanation"]),
            "status": str(row["status"]),
            "visible": str(row["status"]) not in {"dismissed", "resolved"},
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "attempts": [
                {
                    "action": str(attempt["action"]),
                    "actor": str(attempt["actor"]),
                    "note": str(attempt["note"]),
                    "created_at": str(attempt["created_at"]),
                }
                for attempt in attempts
            ],
        }


def _bounded_identifier(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{label} is required")
    safe = _redact(value.strip(), limit)
    if not safe:
        raise StoreError(f"{label} is required")
    return safe


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreError("Station identity must be text")
    return _redact(value.strip(), limit) or None


def _safe_text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{label} is required")
    return _redact(value.strip(), limit)


def _redact(value: str, limit: int) -> str:
    from beehaiive.agent import redact_worker_text, worker_secret_values

    return redact_worker_text(value, worker_secret_values(), max_length=limit)


__all__ = [
    "MAX_STATION_ATTEMPTS",
    "MAX_STATION_ISSUES",
    "MAX_STATION_NOTE_LENGTH",
    "MAX_STATION_TEXT_LENGTH",
    "StationIssueMixin",
]
