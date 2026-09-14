from __future__ import annotations

from typing import Any

from beehaiive.contracts import ContractError, TaskContract, TaskOutcome, TaskResult
from beehaiive.models import RunState, RunStatus, Stage

from .constants import MAX_AGENT_RESULT_LENGTH as MAX_AGENT_RESULT_LENGTH
from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class RunCompletionMixin:
    def complete_agent_run(
        self: Any, run_id: str, result: str, lease_token: str
    ) -> RunState:
        """Persist a bounded worker result and release its lease."""

        normalized_result = result.strip()
        if not normalized_result:
            raise StoreError("An agent result is required")
        normalized_result = normalized_result[:MAX_AGENT_RESULT_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status is RunStatus.COMPLETED:
                return row
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError("Only an active implementation run can complete")
            task_result_data = row.task_result
            if row.task_contract is None:
                raise StoreError("A task contract is required before completion")
            if task_result_data is None:
                raise StoreError("A task result is required before completion")
            try:
                task_result = TaskResult.from_payload(
                    task_result_data, TaskContract.from_dict(row.task_contract)
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if task_result.outcome is not TaskOutcome.PASS:
                raise StoreError("Only a passing task result can complete")
            now = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', execution_token = NULL,
                    last_error = NULL, last_result = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_result, now, run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "completed",
                row.stage,
                row.stage,
                {
                    "result": normalized_result,
                    "task_result": task_result_data,
                },
            )
            return self._run_for_id(connection, run_id) or row


__all__ = ["RunCompletionMixin"]
