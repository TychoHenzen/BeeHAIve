from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .constants import DEFAULT_ACTION_LIMIT as DEFAULT_ACTION_LIMIT
from .constants import MAX_ACTION_LIMIT as MAX_ACTION_LIMIT
from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class ActionsMixin:
    def begin_action(
        self: Any,
        project_id: str,
        kind: str,
        request: dict[str, object],
        repository: str | None = None,
        pbi_number: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Persist an operator action before its side effect starts."""

        if not project_id.strip() or not kind.strip():
            raise StoreError("An action project and kind are required")
        action_id = str(uuid4())
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO actions(
                    action_id, project_id, kind, status, repository_name,
                    pbi_number, run_id, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    project_id,
                    kind,
                    repository,
                    pbi_number,
                    run_id,
                    json.dumps(request, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Could not create action: {action_id}")
            return self._action_from_row(row)

    def finish_action(
        self: Any,
        action_id: str,
        status: str,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> dict[str, object]:
        """Record the observable result of an operator action."""

        if status not in {"succeeded", "failed", "uncertain"}:
            raise StoreError(f"Invalid action status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE actions
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (
                    status,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    error,
                    _now(),
                    action_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown action: {action_id}")
            return self._action_from_row(row)

    def actions_for_project(
        self: Any, project_id: str, limit: int = DEFAULT_ACTION_LIMIT
    ) -> list[dict[str, object]]:
        """Return recent pending, successful, and failed dashboard actions."""

        if not 1 <= limit <= MAX_ACTION_LIMIT:
            raise StoreError(f"action_limit must be between 1 and {MAX_ACTION_LIMIT}")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM actions
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            return [self._action_from_row(row) for row in rows]


__all__ = ["ActionsMixin"]
