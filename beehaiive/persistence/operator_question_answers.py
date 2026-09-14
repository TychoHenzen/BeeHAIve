from __future__ import annotations

import json
from typing import Any

from beehaiive.contract_types.validation import _redact_text as _redact_text
from beehaiive.contracts import ContractError, TaskContract, TaskOutcome, TaskResult
from beehaiive.models import RunStatus

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class OperatorQuestionAnswersMixin:
    def answer_operator_question(
        self: Any,
        run_id: str,
        *,
        question_id: str,
        revision: int,
        answer: str,
        authorization_method: str,
        operator_role: str = "operator",
    ) -> dict[str, object]:
        normalized_answer = _redact_text(answer, 1_000)
        if not normalized_answer:
            raise StoreError("An answer is required")
        if not question_id.strip() or revision < 1:
            raise StoreError("A question ID and positive revision are required")
        if authorization_method != "X-API-Key" or operator_role != "operator":
            raise StoreError("Question answers require the configured operator role")
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            latest = connection.execute(
                """
                SELECT question_id, revision FROM operator_questions
                WHERE run_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if (
                latest is None
                or latest["question_id"] != question_id
                or int(latest["revision"]) != revision
            ):
                raise StoreError("The operator question is stale or does not exist")
            row = connection.execute(
                """
                SELECT question_id, project_id, repository_name, pbi_number,
                       run_id, revision, kind, status, question, evidence_json,
                       answer, owner_scope, authorization_method, operator_role,
                       created_at, updated_at, answered_at, notification_status,
                       notification_attempts, notification_last_attempt_at,
                       notification_last_status_code, notification_delivered_at,
                       notification_last_error
                FROM operator_questions
                WHERE run_id = ? AND question_id = ? AND revision = ?
                """,
                (run_id, question_id, revision),
            ).fetchone()
            if row is None:
                raise StoreError("The operator question is stale or does not exist")
            if row["status"] == "answered":
                if row["answer"] == normalized_answer:
                    return self._operator_question_from_row(row)
                raise StoreError("A different answer is already recorded")
            if (
                row["status"] != "pending"
                or run.status is not RunStatus.AWAITING_OPERATOR
            ):
                raise StoreError("The operator question is not awaiting an answer")
            task_result_json: str | None = None
            if row["kind"] == "question" and run.task_result is not None:
                try:
                    contract = TaskContract.from_dict(run.task_contract or {})
                    current = TaskResult.from_payload(
                        run.task_result, contract, allow_answer=True
                    )
                    if current.outcome is not TaskOutcome.QUESTION:
                        raise StoreError("The run has no pending task question")
                    if (
                        current.answer is not None
                        and current.answer != normalized_answer
                    ):
                        raise StoreError("A different answer is already recorded")
                    answered = TaskResult(
                        current.outcome,
                        current.evidence,
                        current.artifact_refs,
                        current.question,
                        current.required_action,
                        current.validation_reason,
                        normalized_answer,
                    ).validated(contract)
                    task_result_json = json.dumps(answered.as_dict(), sort_keys=True)
                except ContractError as exc:
                    raise StoreError(str(exc)) from exc
            now = _now()
            if task_result_json is None:
                connection.execute(
                    """
                    UPDATE runs SET task_answer = ?, task_answer_resumed = 0,
                        last_error = NULL, updated_at = ?
                    WHERE run_id = ? AND status = 'awaiting_operator'
                    """,
                    (normalized_answer, now, run_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE runs SET task_result_json = ?, task_answer = ?,
                        task_answer_resumed = 0, last_error = NULL, updated_at = ?
                    WHERE run_id = ? AND status = 'awaiting_operator'
                    """,
                    (task_result_json, normalized_answer, now, run_id),
                )
            connection.execute(
                """
                UPDATE operator_questions
                SET status = 'answered', answer = ?, authorization_method = ?,
                    operator_role = ?, answered_at = ?, updated_at = ?,
                    notification_status = CASE
                        WHEN notification_status IN (
                            'pending', 'not_configured', 'sending'
                        ) THEN 'cancelled' ELSE notification_status END,
                    notification_lease_token = NULL,
                    notification_lease_expires_at = NULL
                WHERE question_id = ? AND revision = ? AND status = 'pending'
                """,
                (
                    normalized_answer,
                    authorization_method,
                    operator_role,
                    now,
                    now,
                    question_id,
                    revision,
                ),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (run.project_id, run.repository, run.pbi_number),
            )
            self._record_event(
                connection,
                run.project_id,
                run.repository,
                run.pbi_number,
                run_id,
                "operator_question_answered",
                run.stage,
                run.stage,
                {
                    "question_id": question_id,
                    "revision": revision,
                    "authorization_method": authorization_method,
                    "operator_role": operator_role,
                },
            )
            answered_row = connection.execute(
                """
                SELECT question_id, project_id, repository_name, pbi_number,
                       run_id, revision, kind, status, question, evidence_json,
                       answer, owner_scope, authorization_method, operator_role,
                       created_at, updated_at, answered_at, notification_status,
                       notification_attempts, notification_last_attempt_at,
                       notification_last_status_code, notification_delivered_at,
                       notification_last_error
                FROM operator_questions WHERE question_id = ? AND revision = ?
                """,
                (question_id, revision),
            ).fetchone()
            if answered_row is None:
                raise StoreError("Answered operator question could not be read back")
            return self._operator_question_from_row(answered_row)


__all__ = ["OperatorQuestionAnswersMixin"]
