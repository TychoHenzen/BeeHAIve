from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from beehaiive.models import PbiRefinementAttempt, RefinementStatus

from .constants import MAX_PBI_REFINEMENT_GENERATIONS as MAX_PBI_REFINEMENT_GENERATIONS
from .constants import (
    MAX_PBI_REFINEMENT_REASON_LENGTH as MAX_PBI_REFINEMENT_REASON_LENGTH,
)
from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.refinement_helpers import (
    _new_refinement_questions as _new_refinement_questions,
)
from .helpers.refinement_helpers import (
    _pbi_refinement_attempt_from_row as _pbi_refinement_attempt_from_row,
)
from .helpers.refinement_helpers import (
    _refinement_authorization as _refinement_authorization,
)
from .helpers.refinement_helpers import _refinement_text as _refinement_text
from .helpers.value_helpers import _json_mapping_or_none as _json_mapping_or_none


class PbiRefinementReopenMixin:
    def fail_pbi_refinement_attempt(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        *,
        expected_revision: int,
        reason: str,
        retryable: bool,
        operator_role: str,
        secret_values: Sequence[str] = (),
    ) -> PbiRefinementAttempt:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StoreError("Attempt revision is invalid")
        if type(retryable) is not bool:
            raise StoreError("Retryable failure value is invalid")
        safe_reason = _refinement_text(
            reason, MAX_PBI_REFINEMENT_REASON_LENGTH, "Failure reason", secret_values
        )
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
                raise StoreError("Only an evaluating PBI refinement can fail")
            if expected_revision != row["revision"]:
                raise StoreError("Attempt revision does not match the current revision")
            connection.execute(
                """
                UPDATE pbi_refinement_attempts
                SET status = ?, decision_json = NULL, failure_reason = ?,
                    retryable_failure = ?, authorization_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RefinementStatus.FAILED.value,
                    safe_reason,
                    int(retryable),
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

    def reopen_pbi_refinement_attempt(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        *,
        reason: str,
        questions: Sequence[Mapping[str, object]],
        operator_role: str,
        secret_values: Sequence[str] = (),
    ) -> PbiRefinementAttempt:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
        safe_reason = _refinement_text(
            reason, MAX_PBI_REFINEMENT_REASON_LENGTH, "Reopen reason", secret_values
        )
        new_questions = _new_refinement_questions(questions, secret_values)
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
            status = str(row["status"])
            if status != RefinementStatus.COMPLETED.value and not (
                status == RefinementStatus.FAILED.value
                and bool(row["retryable_failure"])
            ):
                raise StoreError(
                    "Only a terminal decision or retryable failure can reopen"
                )
            reopen_count = int(row["reopen_count"])
            generation = int(row["generation"])
            if (
                reopen_count >= MAX_PBI_REFINEMENT_GENERATIONS - 1
                or generation >= MAX_PBI_REFINEMENT_GENERATIONS
            ):
                raise StoreError(
                    "A PBI refinement attempt may have at most two reopens"
                )
            history = json.loads(str(row["history_json"]))
            old_questions = json.loads(str(row["questions_json"]))
            if not isinstance(history, list) or not isinstance(old_questions, list):
                raise StoreError("Stored PBI refinement history is invalid")
            history = cast(list[object], history)
            old_questions = cast(list[object], old_questions)
            history.append(
                {
                    "generation": generation,
                    "status": status,
                    "questions": old_questions,
                    "decision": _json_mapping_or_none(row["decision_json"]),
                    "failure_reason": row["failure_reason"],
                    "retryable_failure": bool(row["retryable_failure"]),
                    "closed_at": _now(),
                    "reopen_reason": safe_reason,
                }
            )
            connection.execute(
                """
                UPDATE pbi_refinement_attempts
                SET generation = ?, reopen_count = ?, status = ?, questions_json = ?,
                    decision_json = NULL, failure_reason = NULL, retryable_failure = 0,
                    history_json = ?, authorization_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    generation + 1,
                    reopen_count + 1,
                    RefinementStatus.AWAITING_ANSWERS.value,
                    json.dumps(new_questions, sort_keys=True),
                    json.dumps(history, sort_keys=True),
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


__all__ = ["PbiRefinementReopenMixin"]
