from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from beehaiive.models import RunState, RunStatus

from .errors import StoreError
from .helpers.lease_helpers import _lease_is_active as _lease_is_active
from .helpers.lease_helpers import _now as _now


class LeaseStoreMixin:
    def _lease_deadline(self: Any) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self._lease_seconds)).isoformat()

    def _require_lease(self: Any, run: RunState, lease_token: str) -> None:
        if not lease_token or run.lease_token != lease_token:
            raise StoreError("Invalid or missing run lease token")
        if not _lease_is_active(run.lease_expires_at):
            raise StoreError(f"Run {run.run_id} lease has expired")
        self._require_admission(run)

    def _renew_lease(
        self: Any,
        connection: sqlite3.Connection,
        run_id: str,
        lease_token: str,
    ) -> None:
        if self._admission_capacity is not None:
            self._require_lease(self._run_for_id(connection, run_id), lease_token)
        deadline = self._lease_deadline()
        connection.execute(
            """
            UPDATE runs
            SET lease_expires_at = ?, updated_at = ?
            WHERE run_id = ? AND status = 'active' AND lease_token = ?
            """,
            (deadline, _now(), run_id, lease_token),
        )
        connection.execute(
            "UPDATE admissions SET expires_at = ? WHERE run_id = ? AND lease_token = ?",
            (deadline, run_id, lease_token),
        )

    def renew_lease(self: Any, run_id: str, lease_token: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE:
                return row
            self._renew_lease(connection, run_id, lease_token)
            return self._run_for_id(connection, run_id) or row

    def validate_lease(self: Any, run_id: str, lease_token: str) -> RunState:
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            return run

    @property
    def lease_heartbeat_seconds(self: Any) -> float:
        return max(0.05, self._lease_seconds / 3)


__all__ = ["LeaseStoreMixin"]
