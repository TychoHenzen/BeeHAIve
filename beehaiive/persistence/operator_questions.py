from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from beehaiive.contract_types.validation import _bounded_value as _bounded_value
from beehaiive.contract_types.validation import _redact_text as _redact_text
from beehaiive.models import RunState, RunStatus

from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _json_mapping as _json_mapping


class OperatorQuestionsMixin:
    def operator_questions_for_run(
        self: Any, run_id: str, limit: int = 10
    ) -> list[dict[str, object]]:
        if limit < 1:
            raise StoreError("Question history limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT question_id, project_id, repository_name, pbi_number,
                       run_id, revision, kind, status, question, evidence_json,
                       answer, owner_scope, authorization_method, operator_role,
                       created_at, updated_at, answered_at, notification_status,
                       notification_attempts, notification_last_attempt_at,
                       notification_last_status_code, notification_delivered_at,
                       notification_last_error
                FROM operator_questions
                WHERE run_id = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._operator_question_from_row(row) for row in rows]

    def await_operator(
        self: Any,
        run_id: str,
        lease_token: str,
        *,
        kind: str,
        question: str,
        evidence: Mapping[str, object],
    ) -> dict[str, object]:
        if kind not in {"question", "routing_exhausted"}:
            raise StoreError("Unknown operator question kind")
        normalized_question = _redact_text(question, 1_000)
        if not normalized_question:
            raise StoreError("An operator question is required")
        bounded_evidence = _bounded_value(evidence)
        if not isinstance(bounded_evidence, dict):
            raise StoreError("Operator question evidence must be an object")
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            if run.status is RunStatus.AWAITING_OPERATOR:
                existing = self._operator_question_for_run(connection, run_id)
                if (
                    existing is not None
                    and existing["status"] == "pending"
                    and existing["kind"] == kind
                    and existing["question"] == normalized_question
                ):
                    return existing
            if run.status is not RunStatus.ACTIVE:
                raise StoreError("Only an active run can await an operator")
            self._require_lease(run, lease_token)
            return self._create_operator_question(
                connection, run, kind, normalized_question, bounded_evidence
            )

    def await_operator_after_failure(
        self: Any,
        run_id: str,
        *,
        kind: str,
        question: str,
        evidence: Mapping[str, object],
    ) -> dict[str, object]:
        if kind not in {"question", "routing_exhausted"}:
            raise StoreError("Unknown operator question kind")
        normalized_question = _redact_text(question, 1_000)
        if not normalized_question:
            raise StoreError("An operator question is required")
        bounded_evidence = _bounded_value(evidence)
        if not isinstance(bounded_evidence, dict):
            raise StoreError("Operator question evidence must be an object")
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            if run.status is RunStatus.AWAITING_OPERATOR:
                existing = self._operator_question_for_run(connection, run_id)
                if (
                    existing is not None
                    and existing["status"] == "pending"
                    and existing["kind"] == kind
                    and existing["question"] == normalized_question
                ):
                    return existing
                raise StoreError("Run is already awaiting an operator")
            if run.status is not RunStatus.FAILED:
                raise StoreError("Only a failed run can enter operator handoff")
            return self._create_operator_question(
                connection, run, kind, normalized_question, bounded_evidence
            )

    def _create_operator_question(
        self: Any,
        connection: sqlite3.Connection,
        run: RunState,
        kind: str,
        question: str,
        evidence: Mapping[str, object],
    ) -> dict[str, object]:
        existing = connection.execute(
            """
            SELECT question_id, project_id, repository_name, pbi_number,
                   run_id, revision, kind, status, question, evidence_json,
                   answer, owner_scope, authorization_method, operator_role,
                   created_at, updated_at, answered_at, notification_status,
                   notification_attempts, notification_last_attempt_at,
                   notification_last_status_code, notification_delivered_at,
                   notification_last_error
            FROM operator_questions
            WHERE run_id = ? AND status = 'pending'
            """,
            (run.run_id,),
        ).fetchone()
        if existing is not None:
            current = self._operator_question_from_row(existing)
            if current["kind"] == kind and current["question"] == question:
                return current
            raise StoreError("This run already has a pending operator question")
        previous = connection.execute(
            "SELECT MAX(revision) AS revision FROM operator_questions WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        revision = (
            int(previous["revision"])
            if previous is not None and previous["revision"] is not None
            else 0
        ) + 1
        now = _now()
        question_id = str(uuid4())
        question_evidence = dict(evidence)
        if run.owner_id:
            question_evidence["worker_id"] = run.owner_id
        bounded_evidence = _bounded_value(question_evidence)
        if not isinstance(bounded_evidence, dict):
            raise StoreError("Operator question evidence must be an object")
        evidence_json = json.dumps(bounded_evidence, sort_keys=True)
        connection.execute(
            """
            INSERT INTO operator_questions(
                question_id, project_id, repository_name, pbi_number, run_id,
                revision, kind, status, question, evidence_json, owner_scope,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                run.project_id,
                run.repository,
                run.pbi_number,
                run.run_id,
                revision,
                kind,
                question,
                evidence_json,
                run.project_id,
                now,
                now,
            ),
        )
        updated = connection.execute(
            """
            UPDATE runs
            SET status = 'awaiting_operator', task_answer_resumed = 0,
                execution_token = NULL,
                owner_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_error = NULL, last_result = NULL, updated_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (now, run.run_id, run.status.value),
        )
        if updated.rowcount != 1:
            raise StoreError("Run state changed before operator handoff")
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
            run.run_id,
            "operator_question_created",
            run.stage,
            run.stage,
            {"question_id": question_id, "revision": revision, "kind": kind},
        )
        row = connection.execute(
            """
            SELECT question_id, project_id, repository_name, pbi_number,
                   run_id, revision, kind, status, question, evidence_json,
                   answer, owner_scope, authorization_method, operator_role,
                   created_at, updated_at, answered_at, notification_status,
                   notification_attempts, notification_last_attempt_at,
                   notification_last_status_code, notification_delivered_at,
                   notification_last_error
            FROM operator_questions WHERE question_id = ?
            """,
            (question_id,),
        ).fetchone()
        if row is None:
            raise StoreError("Created operator question could not be read back")
        return self._operator_question_from_row(row)

    def operator_question_for_run(self: Any, run_id: str) -> dict[str, object] | None:
        questions = self.operator_questions_for_run(run_id, limit=1)
        return questions[0] if questions else None

    def _operator_question_for_run(
        self: Any, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT question_id, project_id, repository_name, pbi_number,
                   run_id, revision, kind, status, question, evidence_json,
                   answer, owner_scope, authorization_method, operator_role,
                   created_at, updated_at, answered_at,
                   notification_status, notification_attempts,
                   notification_last_attempt_at, notification_last_status_code,
                   notification_delivered_at, notification_last_error
            FROM operator_questions
            WHERE run_id = ?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else self._operator_question_from_row(row)

    def _operator_question_by_id(
        self: Any, connection: sqlite3.Connection, question_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT question_id, project_id, repository_name, pbi_number,
                   run_id, revision, kind, status, question, evidence_json,
                   answer, owner_scope, authorization_method, operator_role,
                   created_at, updated_at, answered_at, notification_status,
                   notification_attempts, notification_last_attempt_at,
                   notification_last_status_code, notification_delivered_at,
                   notification_last_error
            FROM operator_questions WHERE question_id = ?
            """,
            (question_id,),
        ).fetchone()
        return None if row is None else self._operator_question_from_row(row)

    @staticmethod
    def _operator_question_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "question_id": str(row["question_id"]),
            "project_id": str(row["project_id"]),
            "repository": str(row["repository_name"]),
            "pbi_number": int(row["pbi_number"]),
            "run_id": str(row["run_id"]),
            "revision": int(row["revision"]),
            "kind": str(row["kind"]),
            "status": str(row["status"]),
            "question": str(row["question"]),
            "evidence": _json_mapping(row["evidence_json"]),
            "answer": row["answer"],
            "owner_scope": str(row["owner_scope"]),
            "authorization_method": str(row["authorization_method"]),
            "operator_role": str(row["operator_role"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "answered_at": row["answered_at"],
            "notification_status": str(row["notification_status"]),
            "notification_attempts": int(row["notification_attempts"]),
            "notification_last_attempt_at": row["notification_last_attempt_at"],
            "notification_last_status_code": row["notification_last_status_code"],
            "notification_delivered_at": row["notification_delivered_at"],
            "notification_last_error": row["notification_last_error"],
        }


__all__ = ["OperatorQuestionsMixin"]
