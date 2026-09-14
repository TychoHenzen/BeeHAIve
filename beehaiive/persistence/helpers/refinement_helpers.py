from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import cast
from uuid import uuid4

from beehaiive.models import (
    PbiRefinementAttempt,
    PbiRefinementQuestion,
    RefinementStatus,
)

from ..constants import _REFINEMENT_SECRET_ASSIGNMENT as _REFINEMENT_SECRET_ASSIGNMENT
from ..constants import (
    MAX_PBI_REFINEMENT_EVIDENCE_LENGTH as MAX_PBI_REFINEMENT_EVIDENCE_LENGTH,
)
from ..constants import (
    MAX_PBI_REFINEMENT_EVIDENCE_REFS as MAX_PBI_REFINEMENT_EVIDENCE_REFS,
)
from ..constants import MAX_PBI_REFINEMENT_QUESTIONS as MAX_PBI_REFINEMENT_QUESTIONS
from ..constants import MAX_PBI_REFINEMENT_TEXT_LENGTH as MAX_PBI_REFINEMENT_TEXT_LENGTH
from ..errors import StoreError
from .lease_helpers import _now as _now
from .value_helpers import _contains_signed_url as _contains_signed_url
from .value_helpers import _json_mapping as _json_mapping
from .value_helpers import _json_mapping_or_none as _json_mapping_or_none


def _refinement_text(
    value: object,
    limit: int,
    label: str,
    secret_values: Sequence[str] = (),
    *,
    required: bool = True,
) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise StoreError(f"{label} is required")
    if len(value) > limit:
        raise StoreError(f"{label} must be at most {limit:,} characters")
    if _contains_signed_url(value):
        raise StoreError("Signed URLs are not allowed in PBI refinement data")
    from beehaiive.agent import redact_worker_text, worker_secret_values

    safe = redact_worker_text(
        value,
        (*worker_secret_values(), *(secret for secret in secret_values if secret)),
        max_length=None,
    )
    safe = _REFINEMENT_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('name')}=[redacted]", safe
    )
    if len(safe) > limit:
        raise StoreError(f"{label} must be at most {limit:,} characters")
    return safe


def _refinement_evidence_refs(
    value: object, secret_values: Sequence[str] = ()
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise StoreError("Evidence references must be a list")
    references = cast(Sequence[object], value)
    if len(references) > MAX_PBI_REFINEMENT_EVIDENCE_REFS:
        raise StoreError("A question may have at most 10 evidence references")
    return list(
        dict.fromkeys(
            _refinement_text(
                reference,
                MAX_PBI_REFINEMENT_EVIDENCE_LENGTH,
                "Evidence reference",
                secret_values,
            )
            for reference in references
        )
    )


def _new_refinement_questions(
    value: object, secret_values: Sequence[str] = ()
) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise StoreError("Questions must be a list")
    raw_questions = cast(Sequence[object], value)
    if not 1 <= len(raw_questions) <= MAX_PBI_REFINEMENT_QUESTIONS:
        raise StoreError("A generation must contain 1 to 25 questions")
    questions: list[dict[str, object]] = []
    for raw_question in raw_questions:
        if not isinstance(raw_question, Mapping):
            raise StoreError("Each question must be an object")
        question = cast(Mapping[str, object], raw_question)
        questions.append(
            {
                "question_id": uuid4().hex,
                "text": _refinement_text(
                    question.get("text"),
                    MAX_PBI_REFINEMENT_TEXT_LENGTH,
                    "Question",
                    secret_values,
                ),
                "answer_history": [],
                "evidence_refs": _refinement_evidence_refs(
                    question.get("evidence_refs", ()), secret_values
                ),
            }
        )
    return questions


def _refinement_authorization(operator_role: str) -> dict[str, str]:
    if operator_role != "operator":
        raise StoreError("PBI refinement requires the configured operator role")
    return {
        "role": operator_role,
        "method": "X-API-Key",
        "result": "authorized",
        "timestamp": _now(),
    }


def _pbi_refinement_attempt_from_row(row: sqlite3.Row) -> PbiRefinementAttempt:
    try:
        raw_questions = json.loads(str(row["questions_json"]))
        raw_history = json.loads(str(row["history_json"]))
        if not isinstance(raw_questions, list) or not isinstance(raw_history, list):
            raise ValueError("Invalid refinement history")
        raw_questions = cast(list[object], raw_questions)
        raw_history = cast(list[object], raw_history)
        questions_list: list[PbiRefinementQuestion] = []
        for raw_question in raw_questions:
            if not isinstance(raw_question, Mapping):
                raise ValueError("Invalid refinement question")
            question = cast(Mapping[str, object], raw_question)
            raw_answers = question["answer_history"]
            raw_refs = question["evidence_refs"]
            if not isinstance(raw_answers, list) or not isinstance(raw_refs, list):
                raise ValueError("Invalid refinement question history")
            raw_answers = cast(list[object], raw_answers)
            raw_refs = cast(list[object], raw_refs)
            if any(not isinstance(answer, Mapping) for answer in raw_answers):
                raise ValueError("Invalid refinement answer history")
            questions_list.append(
                PbiRefinementQuestion(
                    question_id=str(question["question_id"]),
                    text=str(question["text"]),
                    answer_history=tuple(
                        cast(Mapping[str, object], answer) for answer in raw_answers
                    ),
                    evidence_refs=tuple(str(reference) for reference in raw_refs),
                )
            )
        if any(not isinstance(item, Mapping) for item in raw_history):
            raise ValueError("Invalid refinement history")
        decision = _json_mapping_or_none(row["decision_json"])
        authorization = _json_mapping(row["authorization_json"])
        return PbiRefinementAttempt(
            attempt_id=str(row["attempt_id"]),
            project_id=str(row["project_id"]),
            repository=str(row["repository_name"]),
            pbi_number=int(row["pbi_number"]),
            generation=int(row["generation"]),
            reopen_count=int(row["reopen_count"]),
            revision=int(row["revision"]),
            status=RefinementStatus(str(row["status"])),
            questions=tuple(questions_list),
            decision=decision,
            failure_reason=(
                str(row["failure_reason"])
                if row["failure_reason"] is not None
                else None
            ),
            retryable_failure=bool(row["retryable_failure"]),
            history=tuple(cast(Mapping[str, object], item) for item in raw_history),
            authorization=cast(Mapping[str, str], authorization),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError("Stored PBI refinement data is invalid") from exc


__all__ = [
    "_refinement_text",
    "_refinement_evidence_refs",
    "_new_refinement_questions",
    "_refinement_authorization",
    "_pbi_refinement_attempt_from_row",
]
