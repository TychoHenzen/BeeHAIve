from __future__ import annotations

from typing import Any

from beehaiive.models import RunState, RunStatus, Stage

from .errors import StoreError


class RunTransitionMixin:
    def advance(self: Any, run_id: str, target: Stage, lease_token: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE:
                if row.stage is target:
                    return row
                raise StoreError(f"Run {run_id} is {row.status.value}, not active")
            if row.stage is target:
                return row
            allowed = {
                Stage.REFINE: Stage.IMPLEMENT,
            }
            if allowed.get(row.stage) is not target:
                raise StoreError(
                    f"Cannot advance {row.stage.value} directly to {target.value}"
                )
            connection.execute(
                """
                UPDATE pbis SET stage = ?, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (target.value, row.project_id, row.repository, row.pbi_number),
            )
            self._renew_lease(connection, run_id, lease_token)
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "transition",
                row.stage,
                target,
                {},
            )
            return self._run_for_id(connection, run_id) or row

    def set_run_claimable(self: Any, run_id: str, claimable: bool) -> None:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            connection.execute(
                """
                UPDATE pbis SET claimable = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(claimable),
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )

    def active_runs_for_project(self: Any, project_id: str) -> tuple[RunState, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM runs WHERE project_id = ? AND status = 'active'",
                (project_id,),
            ).fetchall()
            return tuple(
                run
                for row in rows
                if (run := self._run_for_id(self._connection, str(row["run_id"])))
                is not None
            )


__all__ = ["RunTransitionMixin"]
