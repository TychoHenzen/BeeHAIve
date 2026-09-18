from __future__ import annotations

from typing import Any
from uuid import uuid4

from beehaiive.models import RunStatus, Stage

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class ExecutionLeaseMixin:
    def claim_execution(self: Any, run_id: str, lease_token: str) -> str:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can execute a model"
                )
            existing = connection.execute(
                "SELECT execution_token FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                raise StoreError(f"Unknown run: {run_id}")
            if existing["execution_token"] is not None:
                raise StoreError("A model execution is already active")
            execution_token = str(uuid4())
            connection.execute(
                """
                UPDATE runs
                SET execution_token = ?, updated_at = ?
                WHERE run_id = ? AND status = 'active' AND lease_token = ?
                """,
                (execution_token, _now(), run_id, lease_token),
            )
            self._renew_lease(connection, run_id, lease_token)
            return execution_token

    def validate_execution(
        self: Any, run_id: str, lease_token: str, execution_token: str
    ) -> None:
        with self._transaction():
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            current = self._connection.execute(
                "SELECT execution_token FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is None or current["execution_token"] != execution_token:
                raise StoreError("Model execution claim is no longer valid")

    def heartbeat_execution(
        self: Any, run_id: str, lease_token: str, execution_token: str
    ) -> None:
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            current = connection.execute(
                """
                SELECT status, lease_token, execution_token
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if current is None:
                raise StoreError(f"Unknown run: {run_id}")
            if (
                current["status"] != RunStatus.ACTIVE.value
                or current["lease_token"] != lease_token
                or current["execution_token"] != execution_token
            ):
                raise StoreError("Model execution claim is no longer valid")
            self._renew_lease(connection, run_id, lease_token)

    def release_execution(self: Any, run_id: str, execution_token: str) -> None:
        with self._transaction() as connection:
            if self._admission_capacity is not None:
                run = self._run_for_id(connection, run_id)
                if run is None:
                    return
                current = connection.execute(
                    "SELECT execution_token FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if current is None or current["execution_token"] != execution_token:
                    return
                self._require_lease(run, run.lease_token or "")
            connection.execute(
                """
                UPDATE runs
                SET execution_token = NULL, updated_at = ?
                WHERE run_id = ? AND execution_token = ?
                """,
                (_now(), run_id, execution_token),
            )


__all__ = ["ExecutionLeaseMixin"]
