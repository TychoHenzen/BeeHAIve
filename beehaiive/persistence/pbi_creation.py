from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .constants import PBI_CREATION_LEASE_SECONDS as PBI_CREATION_LEASE_SECONDS
from .errors import StoreError
from .helpers.lease_helpers import _lease_is_active as _lease_is_active
from .helpers.lease_helpers import _now as _now


class PbiCreationMixin:
    def get_pbi_creation(
        self: Any, project_id: str, key_hash: str
    ) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
        return self._pbi_creation_from_row(row) if row is not None else None

    def begin_pbi_creation(
        self: Any,
        project_id: str,
        key_hash: str,
        request_hash: str,
        repository: str,
        lease_owner: str,
    ) -> tuple[dict[str, object], bool]:
        """Reserve one key and claim its durable attempt when no lease is active."""

        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO pbi_creations(
                    project_id, key_hash, request_hash, repository_name,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (project_id, key_hash, request_hash, repository, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if row is None:
                raise StoreError("Could not reserve PBI creation key")
            if row["request_hash"] != request_hash:
                return self._pbi_creation_from_row(row), False
            if row["status"] in {"complete", "outcome_unknown"}:
                return self._pbi_creation_from_row(row), False
            if _lease_is_active(row["lease_expires_at"]):
                return self._pbi_creation_from_row(row), False
            if row["issue_create_started"] and row["issue_id"] is None:
                connection.execute(
                    """
                    UPDATE pbi_creations
                    SET status = 'outcome_unknown',
                        failed_step = COALESCE(current_step, 'create_issue'),
                        failure_code = 'outcome_unknown',
                        failure_class = 'ProcessInterrupted',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE project_id = ? AND key_hash = ?
                    """,
                    (now, project_id, key_hash),
                )
                row = connection.execute(
                    """
                    SELECT * FROM pbi_creations
                    WHERE project_id = ? AND key_hash = ?
                    """,
                    (project_id, key_hash),
                ).fetchone()
                if row is None:
                    raise StoreError("PBI creation disappeared during recovery")
                return self._pbi_creation_from_row(row), False

            lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=PBI_CREATION_LEASE_SECONDS)
            ).isoformat()
            connection.execute(
                """
                UPDATE pbi_creations
                SET status = 'in_progress', lease_owner = ?, lease_expires_at = ?,
                    current_step = COALESCE(current_step, 'preflight'),
                    failed_step = NULL, failure_code = NULL, failure_class = NULL,
                    updated_at = ?
                WHERE project_id = ? AND key_hash = ?
                """,
                (lease_owner, lease_expires_at, now, project_id, key_hash),
            )
            row = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if row is None:
                raise StoreError("PBI creation disappeared after reservation")
            return self._pbi_creation_from_row(row), True

    def checkpoint_pbi_creation(
        self: Any,
        project_id: str,
        key_hash: str,
        lease_owner: str,
        progress: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist one confirmed mutation step before attempting the next."""

        issue_create_started = progress.get("issue_create_started") is True
        issue_id = progress.get("issue_id")
        issue_number = progress.get("issue_number")
        issue_url = progress.get("issue_url")
        project_item_id = progress.get("project_item_id")
        current_step = progress.get("current_step")
        raw_completed_steps: object = progress.get("completed_steps", [])
        if not isinstance(raw_completed_steps, list) or any(
            not isinstance(step, str)
            for step in cast(list[object], raw_completed_steps)
        ):
            raise StoreError("PBI creation progress has invalid completed steps")
        completed_steps = cast(list[str], raw_completed_steps)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if row is None or row["lease_owner"] != lease_owner:
                raise StoreError("PBI creation lease is not active")
            lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=PBI_CREATION_LEASE_SECONDS)
            ).isoformat()
            connection.execute(
                """
                UPDATE pbi_creations
                SET issue_create_started = ?, issue_id = ?, issue_number = ?,
                    issue_url = ?, project_item_id = ?, completed_steps_json = ?,
                    current_step = ?, failed_step = NULL, failure_code = NULL,
                    failure_class = NULL, lease_expires_at = ?, updated_at = ?
                WHERE project_id = ? AND key_hash = ? AND lease_owner = ?
                """,
                (
                    int(issue_create_started),
                    issue_id if isinstance(issue_id, str) else None,
                    issue_number if type(issue_number) is int else None,
                    issue_url if isinstance(issue_url, str) else None,
                    project_item_id if isinstance(project_item_id, str) else None,
                    json.dumps(completed_steps, sort_keys=True),
                    current_step if isinstance(current_step, str) else None,
                    lease_expires_at,
                    _now(),
                    project_id,
                    key_hash,
                    lease_owner,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if updated is None:
                raise StoreError("PBI creation disappeared after checkpoint")
            return self._pbi_creation_from_row(updated)

    def finish_pbi_creation(
        self: Any,
        project_id: str,
        key_hash: str,
        lease_owner: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        """Record success only after the provider completed remote readback."""

        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if row is None or row["lease_owner"] != lease_owner:
                raise StoreError("PBI creation lease is not active")
            connection.execute(
                """
                UPDATE pbi_creations
                SET status = 'complete', result_json = ?, current_step = NULL,
                    failed_step = NULL, failure_code = NULL, failure_class = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE project_id = ? AND key_hash = ? AND lease_owner = ?
                """,
                (
                    json.dumps(dict(result), sort_keys=True),
                    _now(),
                    project_id,
                    key_hash,
                    lease_owner,
                ),
            )
            completed = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if completed is None:
                raise StoreError("PBI creation disappeared after completion")
            return self._pbi_creation_from_row(completed)

    def fail_pbi_creation(
        self: Any,
        project_id: str,
        key_hash: str,
        lease_owner: str,
        status: str,
        failed_step: str,
        failure_code: str,
        failure_class: str,
    ) -> dict[str, object]:
        """Persist a bounded failure classification and release the attempt lease."""

        if status not in {"incomplete", "outcome_unknown"}:
            raise StoreError(f"Invalid PBI creation status: {status}")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if row is None or row["lease_owner"] != lease_owner:
                raise StoreError("PBI creation lease is not active")
            if status == "outcome_unknown" and row["issue_id"] is not None:
                raise StoreError("Known issue identity cannot have an unknown outcome")
            connection.execute(
                """
                UPDATE pbi_creations
                SET status = ?, failed_step = ?, failure_code = ?, failure_class = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE project_id = ? AND key_hash = ? AND lease_owner = ?
                """,
                (
                    status,
                    failed_step[:100],
                    failure_code[:80],
                    failure_class[:80],
                    _now(),
                    project_id,
                    key_hash,
                    lease_owner,
                ),
            )
            failed = connection.execute(
                """
                SELECT * FROM pbi_creations
                WHERE project_id = ? AND key_hash = ?
                """,
                (project_id, key_hash),
            ).fetchone()
            if failed is None:
                raise StoreError("PBI creation disappeared after failure")
            return self._pbi_creation_from_row(failed)


__all__ = ["PbiCreationMixin"]
