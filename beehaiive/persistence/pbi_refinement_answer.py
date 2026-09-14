from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, cast

from beehaiive.models import PbiRefinementAttempt, RefinementStatus

from .constants import MAX_PBI_REFINEMENT_CORRECTIONS as MAX_PBI_REFINEMENT_CORRECTIONS
from .constants import (
    MAX_PBI_REFINEMENT_EVIDENCE_REFS as MAX_PBI_REFINEMENT_EVIDENCE_REFS,
)
from .constants import MAX_PBI_REFINEMENT_TEXT_LENGTH as MAX_PBI_REFINEMENT_TEXT_LENGTH
from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.refinement_helpers import (
    _pbi_refinement_attempt_from_row as _pbi_refinement_attempt_from_row,
)
from .helpers.refinement_helpers import (
    _refinement_authorization as _refinement_authorization,
)
from .helpers.refinement_helpers import (
    _refinement_evidence_refs as _refinement_evidence_refs,
)
from .helpers.refinement_helpers import _refinement_text as _refinement_text


class PbiRefinementAnswerMixin:
    def answer_pbi_refinement_question(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        question_id: str,
        answer: str,
        *,
        expected_revision: int,
        evidence_refs: Sequence[str] = (),
        operator_role: str,
        secret_values: Sequence[str] = (),
    ) -> PbiRefinementAttempt:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StoreError("Answer revision is invalid")
        if not re.fullmatch(r"[0-9a-f]{32}", question_id):
            raise StoreError("Question id is invalid")
        safe_answer = _refinement_text(
            answer, MAX_PBI_REFINEMENT_TEXT_LENGTH, "Answer", secret_values
        )
        new_refs = _refinement_evidence_refs(evidence_refs, secret_values)
        authorization = _refinement_authorization(operator_role)
        with self._transaction() as connection:
            self._require_active_refinement_repository(
                connection, project_id, repository
            )
            row = connection.execute(
                """
                SELECT * FROM pbi_refinement_attempts
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                """,
                (project_id, repository, pbi_number),
            ).fetchone()
            if row is None:
                raise StoreError("PBI refinement attempt was not found")
            if row["status"] not in {
                RefinementStatus.AWAITING_ANSWERS.value,
                RefinementStatus.EVALUATING.value,
            }:
                raise StoreError("PBI refinement attempt does not accept answers")
            questions = json.loads(str(row["questions_json"]))
            if not isinstance(questions, list):
                raise StoreError("Stored PBI refinement questions are invalid")
            questions = cast(list[object], questions)
            question: dict[str, object] | None = None
            for item in questions:
                if not isinstance(item, dict):
                    continue
                candidate = cast(dict[str, object], item)
                if candidate.get("question_id") == question_id:
                    question = candidate
                    break
            if question is None:
                raise StoreError("Question was not found in the active generation")
            answer_history = question.get("answer_history")
            question_refs = question.get("evidence_refs")
            if not isinstance(answer_history, list) or not isinstance(
                question_refs, list
            ):
                raise StoreError("Stored PBI refinement question is invalid")
            answer_history = cast(list[object], answer_history)
            question_refs = cast(list[object], question_refs)
            if any(not isinstance(item, dict) for item in answer_history) or any(
                not isinstance(reference, str) for reference in question_refs
            ):
                raise StoreError("Stored PBI refinement question is invalid")
            answer_history = cast(list[dict[str, object]], answer_history)
            question_refs = cast(list[str], question_refs)
            latest = answer_history[-1] if answer_history else None
            latest_refs = latest.get("evidence_refs") if latest is not None else None
            if (
                latest is not None
                and latest.get("text") == safe_answer
                and isinstance(latest_refs, list)
                and all(
                    reference in cast(list[object], latest_refs)
                    for reference in new_refs
                )
            ):
                connection.execute(
                    "UPDATE pbi_refinement_attempts SET authorization_json = ? "
                    "WHERE attempt_id = ?",
                    (json.dumps(authorization, sort_keys=True), row["attempt_id"]),
                )
                refreshed = connection.execute(
                    "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()
                if refreshed is None:
                    raise StoreError("PBI refinement attempt could not be loaded")
                return _pbi_refinement_attempt_from_row(refreshed)
            if expected_revision != len(answer_history):
                raise StoreError("Answer revision does not match the current revision")
            if len(answer_history) >= MAX_PBI_REFINEMENT_CORRECTIONS + 1:
                raise StoreError("A question may have at most three corrected answers")
            combined_refs = list(dict.fromkeys([*question_refs, *new_refs]))
            if len(combined_refs) > MAX_PBI_REFINEMENT_EVIDENCE_REFS:
                raise StoreError("A question may have at most 10 evidence references")
            answer_history.append(
                {
                    "revision": len(answer_history) + 1,
                    "text": safe_answer,
                    "evidence_refs": new_refs,
                }
            )
            question["evidence_refs"] = combined_refs
            status = (
                RefinementStatus.EVALUATING.value
                if all(
                    isinstance(item, dict)
                    and bool(cast(dict[str, object], item).get("answer_history"))
                    for item in questions
                )
                else RefinementStatus.AWAITING_ANSWERS.value
            )
            connection.execute(
                """
                UPDATE pbi_refinement_attempts
                SET revision = revision + 1, status = ?, questions_json = ?,
                    authorization_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    status,
                    json.dumps(questions, sort_keys=True),
                    json.dumps(authorization, sort_keys=True),
                    _now(),
                    row["attempt_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            if updated is None:
                raise StoreError("PBI refinement attempt could not be loaded")
            return _pbi_refinement_attempt_from_row(updated)

    def complete_pbi_refinement_attempt(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        *,
        expected_revision: int,
        summary: str,
        evidence_refs: Sequence[str] = (),
        operator_role: str,
        secret_values: Sequence[str] = (),
    ) -> PbiRefinementAttempt:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StoreError("Attempt revision is invalid")
        decision = {
            "summary": _refinement_text(
                summary,
                MAX_PBI_REFINEMENT_TEXT_LENGTH,
                "Decision summary",
                secret_values,
            ),
            "evidence_refs": _refinement_evidence_refs(evidence_refs, secret_values),
        }
        authorization = _refinement_authorization(operator_role)
        with self._transaction() as connection:
            self._require_active_refinement_repository(
                connection, project_id, repository
            )
            row = connection.execute(
                """
                SELECT * FROM pbi_refinement_attempts
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                """,
                (project_id, repository, pbi_number),
            ).fetchone()
            if row is None:
                raise StoreError("PBI refinement attempt was not found")
            if row["status"] != RefinementStatus.EVALUATING.value:
                raise StoreError("Only an evaluating PBI refinement can complete")
            if expected_revision != row["revision"]:
                raise StoreError("Attempt revision does not match the current revision")
            connection.execute(
                """
                UPDATE pbi_refinement_attempts
                SET status = ?, decision_json = ?, failure_reason = NULL,
                    retryable_failure = 0, authorization_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RefinementStatus.COMPLETED.value,
                    json.dumps(decision, sort_keys=True),
                    json.dumps(authorization, sort_keys=True),
                    _now(),
                    row["attempt_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            if updated is None:
                raise StoreError("PBI refinement attempt could not be loaded")
            return _pbi_refinement_attempt_from_row(updated)


__all__ = ["PbiRefinementAnswerMixin"]
