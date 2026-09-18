from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .helpers.lease_helpers import _lease_is_active as _lease_is_active
from .helpers.lease_helpers import _now as _now


class IdeaCaptureMixin:
    @property
    def idea_capture_heartbeat_seconds(self: Any) -> float:
        return max(min(self._lease_seconds / 3, 30.0), 0.05)

    def begin_idea_capture(
        self: Any,
        project_id: str,
        key_hash: str,
        request: dict[str, object],
        repository: str | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Atomically reserve one dashboard idea and its action record."""

        if not project_id.strip() or not key_hash.strip():
            raise ValueError("An idea capture project and key are required")
        action_id = str(uuid4())
        lease_owner = str(uuid4())
        now = _now()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
        ).isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT a.*, c.lease_expires_at AS idea_lease_expires_at,
                       c.side_effect_started AS idea_side_effect_started
                FROM idea_captures AS c
                JOIN actions AS a ON a.action_id = c.action_id
                WHERE c.project_id = ? AND c.key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "pending" and not _lease_is_active(
                    existing["idea_lease_expires_at"]
                ):
                    detail = (
                        "Idea capture lease expired; outcome is unknown and "
                        "must be reconciled before retry."
                    )
                    if not existing["idea_side_effect_started"]:
                        connection.execute(
                            """
                            UPDATE actions
                            SET status = 'failed', result_json = ?, error = ?,
                                updated_at = ?
                            WHERE action_id = ?
                            """,
                            (
                                json.dumps(
                                    {
                                        "status": "failed",
                                        "retryable": True,
                                        "error": (
                                            "Idea capture lease expired before launch."
                                        ),
                                    },
                                    sort_keys=True,
                                ),
                                "Idea capture lease expired before launch.",
                                now,
                                existing["action_id"],
                            ),
                        )
                        connection.execute(
                            "DELETE FROM idea_captures WHERE action_id = ?",
                            (existing["action_id"],),
                        )
                        existing = None
                    else:
                        connection.execute(
                            """
                            UPDATE actions
                            SET status = 'failed', result_json = ?, error = ?,
                                updated_at = ?
                            WHERE action_id = ?
                            """,
                            (
                                json.dumps(
                                    {
                                        "status": "outcome_unknown",
                                        "error": detail,
                                        "operator_action": "Reconcile before retry.",
                                    },
                                    sort_keys=True,
                                ),
                                detail,
                                now,
                                existing["action_id"],
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE idea_captures
                            SET lease_owner = NULL, lease_expires_at = NULL,
                                updated_at = ?
                            WHERE project_id = ? AND key_hash = ?
                            """,
                            (now, project_id, key_hash),
                        )
                        existing = connection.execute(
                            """
                            SELECT a.*
                            FROM idea_captures AS c
                            JOIN actions AS a ON a.action_id = c.action_id
                            WHERE c.project_id = ? AND c.key_hash = ?
                            """,
                            (project_id, key_hash),
                        ).fetchone()
                        if existing is None:
                            raise RuntimeError(
                                "Idea capture disappeared during recovery"
                            )
                if existing is None:
                    pass
                else:
                    return self._action_from_row(existing), False

            connection.execute(
                """
                INSERT INTO actions(
                    action_id, project_id, kind, status, repository_name,
                    request_json, created_at, updated_at
                ) VALUES (?, ?, 'capture_idea', 'pending', ?, ?, ?, ?)
                """,
                (
                    action_id,
                    project_id,
                    repository,
                    json.dumps(request, sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO idea_captures(
                    project_id, key_hash, action_id, lease_owner,
                    lease_expires_at, side_effect_started, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    project_id,
                    key_hash,
                    action_id,
                    lease_owner,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Could not create idea capture action: {action_id}")
            return self._action_from_row(row), True

    def release_idea_capture(self: Any, action_id: str) -> None:
        """Release a claim when no external side effect was attempted."""

        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM idea_captures WHERE action_id = ?", (action_id,)
            )

    def mark_idea_capture_started(self: Any, action_id: str) -> bool | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT side_effect_started, lease_expires_at
                FROM idea_captures
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                action = connection.execute(
                    "SELECT request_json FROM actions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if action is None:
                    return None
                try:
                    request = json.loads(str(action["request_json"]))
                except json.JSONDecodeError:
                    return None
                return (
                    False
                    if isinstance(request, dict) and "idea_key" in request
                    else None
                )
            if row["side_effect_started"] or not _lease_is_active(
                row["lease_expires_at"]
            ):
                return False
            updated = connection.execute(
                """
                UPDATE idea_captures
                SET side_effect_started = 1, updated_at = ?
                WHERE action_id = ?
                """,
                (_now(), action_id),
            )
            return updated.rowcount == 1

    def renew_idea_capture(self: Any, action_id: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT lease_expires_at, side_effect_started
                FROM idea_captures
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if (
                row is None
                or not row["side_effect_started"]
                or not _lease_is_active(row["lease_expires_at"])
            ):
                return False
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
            ).isoformat()
            connection.execute(
                """
                UPDATE idea_captures
                SET lease_expires_at = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (expires_at, _now(), action_id),
            )
            return True

    def recover_idea_capture(self: Any, action_id: str) -> bool:
        """Release a pending claim only when no external call started."""

        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT side_effect_started
                FROM idea_captures
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                return False
            if not row["side_effect_started"]:
                connection.execute(
                    "DELETE FROM idea_captures WHERE action_id = ?", (action_id,)
                )
                return True
            connection.execute(
                """
                UPDATE idea_captures
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE action_id = ?
                """,
                (_now(), action_id),
            )
            return False

    def mark_idea_capture_outcome_unknown(self: Any, action_id: str) -> None:
        """Keep a failed action claimed when its external outcome is uncertain."""

        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE idea_captures
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE action_id = ?
                """,
                (_now(), action_id),
            )

    def complete_idea_capture(self: Any, action_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE idea_captures
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE action_id = ?
                """,
                (_now(), action_id),
            )


__all__ = ["IdeaCaptureMixin"]
