from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from beehaiive.models import PbiRefinementAttempt, RefinementStatus

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


class PbiRefinementStartMixin:
    @staticmethod
    def _validate_pbi_refinement_key(
        project_id: str, repository: str, pbi_number: int
    ) -> None:
        if not project_id.strip() or len(project_id) > 500:
            raise StoreError("Project id is invalid")
        if not repository.strip() or len(repository) > 300:
            raise StoreError("Repository is invalid")
        if type(pbi_number) is not int or not 1 <= pbi_number <= 2_147_483_647:
            raise StoreError("PBI number is outside the supported range")

    @staticmethod
    def _require_active_refinement_repository(
        connection: sqlite3.Connection, project_id: str, repository: str
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM repositories
            WHERE project_id = ? AND name = ? AND active = 1
            """,
            (project_id, repository),
        ).fetchone()
        if row is None:
            raise StoreError("Repository is not active in the configured Project")

    def create_pbi_refinement_attempt(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        questions: Sequence[Mapping[str, object]],
        *,
        operator_role: str,
        secret_values: Sequence[str] = (),
    ) -> PbiRefinementAttempt:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
        new_questions = _new_refinement_questions(questions, secret_values)
        authorization = _refinement_authorization(operator_role)
        now = _now()
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
                attempt_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO pbi_refinement_attempts(
                        attempt_id, project_id, repository_name, pbi_number,
                        generation, reopen_count, revision, status, questions_json,
                        authorization_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 0, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        project_id,
                        repository,
                        pbi_number,
                        RefinementStatus.AWAITING_ANSWERS.value,
                        json.dumps(new_questions, sort_keys=True),
                        json.dumps(authorization, sort_keys=True),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
            else:
                connection.execute(
                    """
                    UPDATE pbi_refinement_attempts SET authorization_json = ?
                    WHERE attempt_id = ?
                    """,
                    (json.dumps(authorization, sort_keys=True), row["attempt_id"]),
                )
                row = connection.execute(
                    "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()
            if row is None:
                raise StoreError("PBI refinement attempt could not be loaded")
            return _pbi_refinement_attempt_from_row(row)

    def get_pbi_refinement_attempt(
        self: Any,
        project_id: str,
        repository: str,
        pbi_number: int,
        *,
        operator_role: str,
    ) -> PbiRefinementAttempt | None:
        self._validate_pbi_refinement_key(project_id, repository, pbi_number)
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
                return None
            connection.execute(
                "UPDATE pbi_refinement_attempts SET authorization_json = ? "
                "WHERE attempt_id = ?",
                (json.dumps(authorization, sort_keys=True), row["attempt_id"]),
            )
            row = connection.execute(
                "SELECT * FROM pbi_refinement_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            return _pbi_refinement_attempt_from_row(row) if row is not None else None


__all__ = ["PbiRefinementStartMixin"]
