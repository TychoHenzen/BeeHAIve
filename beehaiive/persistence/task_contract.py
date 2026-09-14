from __future__ import annotations

import json
from typing import Any, cast

from beehaiive.contracts import ContractError, TaskContract, TaskOutcome, TaskResult
from beehaiive.models import RunState, RunStatus, Stage

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class TaskContractMixin:
    def ensure_task_contract(
        self: Any, run_id: str, contract: TaskContract, lease_token: str
    ) -> RunState:
        try:
            contract_data = contract.as_dict()
        except ContractError as exc:
            raise StoreError(str(exc)) from exc
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can set a task contract"
                )
            if row.task_contract == contract_data:
                return row
            connection.execute(
                """
                UPDATE runs
                SET task_contract_json = ?, task_result_json = NULL,
                    task_answer_resumed = 0, updated_at = ?
                WHERE run_id = ?
                """,
                (json.dumps(contract_data, sort_keys=True), _now(), run_id),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "task_contract",
                row.stage,
                row.stage,
                {"contract": contract_data},
            )
            return self._run_for_id(connection, run_id) or row

    def record_task_result(
        self: Any, run_id: str, result: TaskResult, lease_token: str
    ) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can record a task result"
                )
            if row.task_contract is None:
                raise StoreError("A task contract is required before its result")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                validated = result.validated(contract)
                if validated.answer is not None:
                    raise ContractError("Worker task results cannot contain an answer")
                result_data = validated.as_dict()
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            connection.execute(
                """
                UPDATE runs SET task_result_json = ?, task_answer_resumed = 0,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (json.dumps(result_data, sort_keys=True), _now(), run_id),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "task_result",
                row.stage,
                row.stage,
                {"result": result_data},
            )
            return self._run_for_id(connection, run_id) or row

    def answer_task_question(self: Any, run_id: str, answer: str) -> RunState:
        if not answer.strip():
            raise StoreError("An answer is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.task_contract is None or row.task_result is None:
                raise StoreError("Run has no pending task question")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                current = TaskResult.from_payload(
                    row.task_result, contract, allow_answer=True
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if current.outcome is not TaskOutcome.QUESTION:
                raise StoreError("Run has no pending task question")
            answered = TaskResult(
                current.outcome,
                current.evidence,
                current.artifact_refs,
                current.question,
                current.required_action,
                current.validation_reason,
                answer,
            ).validated(contract)
            if current.answer is not None:
                if current.answer == answered.answer:
                    connection.execute(
                        """
                        UPDATE runs SET task_answer_resumed = 0, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (_now(), run_id),
                    )
                    connection.execute(
                        """
                        UPDATE pbis SET claimable = 0
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (row.project_id, row.repository, row.pbi_number),
                    )
                    return self._run_for_id(connection, run_id) or row
                raise StoreError("A different answer is already recorded")
            result_data = answered.as_dict()
            connection.execute(
                """
                UPDATE runs
                SET task_result_json = ?, task_answer = ?, last_error = NULL,
                    task_answer_resumed = 0, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    json.dumps(result_data, sort_keys=True),
                    cast(str, answered.answer),
                    _now(),
                    run_id,
                ),
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
                "question_answered",
                row.stage,
                row.stage,
                {"answer": cast(str, answered.answer)},
            )
            return self._run_for_id(connection, run_id) or row

    def mark_task_question_resumed(self: Any, run_id: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            operator_question = connection.execute(
                """
                SELECT question_id, status, answer FROM operator_questions
                WHERE run_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if operator_question is not None:
                if (
                    operator_question["status"] != "answered"
                    or operator_question["answer"] != row.task_answer
                ):
                    raise StoreError("Run has no answered task question")
                previous = connection.execute(
                    "SELECT task_answer_resumed FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE runs SET task_answer_resumed = 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_now(), run_id),
                )
                connection.execute(
                    """
                    UPDATE pbis SET claimable = 1, last_error = NULL
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (row.project_id, row.repository, row.pbi_number),
                )
                if previous is not None and not bool(previous["task_answer_resumed"]):
                    self._record_event(
                        connection,
                        row.project_id,
                        row.repository,
                        row.pbi_number,
                        run_id,
                        "question_resumed",
                        row.stage,
                        row.stage,
                        {"question_id": operator_question["question_id"]}
                        if "question_id" in operator_question
                        else {},
                    )
                return self._run_for_id(connection, run_id) or row
            if row.task_contract is None or row.task_result is None:
                raise StoreError("Run has no answered task question")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                result = TaskResult.from_payload(
                    row.task_result, contract, allow_answer=True
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if (
                result.outcome is not TaskOutcome.QUESTION
                or result.answer is None
                or result.answer != row.task_answer
            ):
                raise StoreError("Run has no answered task question")
            previous = connection.execute(
                "SELECT task_answer_resumed FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE runs SET task_answer_resumed = 1, updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(row.stage is not Stage.PULL_REQUEST),
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            if previous is not None and not bool(previous["task_answer_resumed"]):
                self._record_event(
                    connection,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                    run_id,
                    "question_resumed",
                    row.stage,
                    row.stage,
                    {},
                )
            return self._run_for_id(connection, run_id) or row


__all__ = ["TaskContractMixin"]
