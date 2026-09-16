from __future__ import annotations

from typing import Any
from uuid import uuid4

from beehaiive.models import RunState, Stage

from .errors import StoreError
from .helpers.lease_helpers import _lease_is_active as _lease_is_active
from .helpers.lease_helpers import _now as _now


class ProjectClaimMixin:
    def claim_next(
        self: Any,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
        *,
        expected_run_id: str | None = None,
        expected_pbi_number: int | None = None,
        agent_session: tuple[str, str] | None = None,
        allow_failed_expected: bool = False,
    ) -> RunState | None:
        if not owner_id.strip():
            raise StoreError("A worker owner is required")
        if expected_pbi_number is not None and (
            type(expected_pbi_number) is not int or expected_pbi_number <= 0
        ):
            raise StoreError("A PBI number must be a positive integer")
        if agent_session is not None and (
            not agent_session[0].strip() or not agent_session[1].strip()
        ):
            raise StoreError("An agent worker and task are required")
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT p.*, r.run_id, r.status, r.attempt,
                       r.owner_id, r.lease_token, r.lease_expires_at,
                       r.last_error AS run_error, r.last_result AS run_result,
                       r.task_contract_json, r.task_result_json, r.task_answer
                FROM pbis AS p
                JOIN repositories AS repository
                  ON repository.project_id = p.project_id
                 AND repository.name = p.repository_name
                 AND repository.active = 1
                 AND p.active = 1
                JOIN runs AS r
                  ON r.project_id = p.project_id
                 AND r.repository_name = p.repository_name
                 AND r.pbi_number = p.number
                WHERE p.project_id = ? AND p.repository_name = ?
                  AND r.status = 'active'
                  AND (? IS NULL OR r.run_id = ?)
                  AND (? IS NULL OR p.number = ?)
                LIMIT 1
                """,
                (
                    project_id,
                    repository,
                    expected_run_id,
                    expected_run_id,
                    expected_pbi_number,
                    expected_pbi_number,
                ),
            ).fetchone()
            if (
                expected_run_id is not None
                and active is None
                and not allow_failed_expected
            ):
                return None
            if (
                allow_failed_expected
                and expected_run_id is not None
                and active is not None
            ):
                return None
            if active is not None:
                active_run = self._run_from_row(active)
                if (
                    active_run.owner_id == owner_id
                    and lease_token is not None
                    and active_run.lease_token == lease_token
                    and _lease_is_active(active_run.lease_expires_at)
                ):
                    self._renew_lease(connection, active_run.run_id, lease_token)
                    if agent_session is not None:
                        self._upsert_agent_session(
                            connection, active_run.run_id, *agent_session
                        )
                    return self._run_for_id(connection, active_run.run_id)
                if _lease_is_active(active_run.lease_expires_at):
                    return None
                run_id = active_run.run_id
                new_lease_token = str(uuid4())
                connection.execute(
                    """
                    UPDATE runs
                    SET owner_id = ?, lease_token = ?, lease_expires_at = ?,
                        execution_token = NULL, last_error = NULL,
                        last_result = NULL, updated_at = ?
                    WHERE run_id = ? AND status = 'active'
                    """,
                    (
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                        run_id,
                    ),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    int(active["number"]),
                    run_id,
                    "lease_reclaimed",
                    Stage(str(active["stage"])),
                    Stage(str(active["stage"])),
                    {"owner_id": owner_id},
                )
                if agent_session is not None:
                    self._upsert_agent_session(connection, run_id, *agent_session)
                return self._run_for_id(connection, run_id)

            candidate_query = """
                SELECT p.*, r.run_id AS existing_run_id, r.status AS existing_status,
                       r.attempt AS existing_attempt
                FROM pbis AS p
                JOIN repositories AS repository
                  ON repository.project_id = p.project_id
                 AND repository.name = p.repository_name
                 AND repository.active = 1
                 AND p.active = 1
                LEFT JOIN runs AS r
                  ON r.project_id = p.project_id
                 AND r.repository_name = p.repository_name
                 AND r.pbi_number = p.number
                WHERE p.project_id = ?
                  AND p.repository_name = ?
                  AND p.claimable = 1
                  AND p.stage != ?
                  AND (? IS NULL OR p.number = ?)
                  AND (
                      r.status IS NULL OR r.status = 'failed'
                      OR (r.status = 'awaiting_operator' AND r.task_answer_resumed = 1)
                  )
                ORDER BY p.number
                LIMIT 1
                """
            candidate_parameters: tuple[object, ...] = (
                project_id,
                repository,
                Stage.PULL_REQUEST.value,
            )
            if allow_failed_expected and expected_run_id is not None:
                candidate_query = candidate_query.replace(
                    "AND p.stage != ?",
                    "AND p.stage != ?\n                  AND r.run_id = ?",
                )
                resumable_statuses = (
                    "AND (\n"
                    "                      r.status IS NULL OR r.status = 'failed'\n"
                    "                      OR (r.status = 'awaiting_operator' "
                    "AND r.task_answer_resumed = 1)\n"
                    "                  )"
                )
                candidate_query = candidate_query.replace(
                    resumable_statuses,
                    "AND r.status = 'failed'",
                )
                candidate_parameters += (expected_run_id,)
            candidate_parameters += (expected_pbi_number, expected_pbi_number)
            candidate = connection.execute(
                candidate_query, candidate_parameters
            ).fetchone()
            if candidate is None:
                return None

            run_id = candidate["existing_run_id"]
            new_lease_token = str(uuid4())
            if isinstance(run_id, str):
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'active', attempt = attempt + 1,
                        owner_id = ?, lease_token = ?, lease_expires_at = ?,
                        execution_token = NULL, last_error = NULL,
                        last_result = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                        run_id,
                    ),
                )
                event_type = "resumed"
            else:
                run_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, project_id, repository_name, pbi_number,
                        status, attempt, owner_id, lease_token,
                        lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        project_id,
                        repository,
                        candidate["number"],
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                    ),
                )
                event_type = "claimed"

            current_stage = Stage(candidate["stage"])
            if current_stage is Stage.BACKLOG:
                connection.execute(
                    """
                    UPDATE pbis SET stage = ?, run_id = ?, last_error = NULL
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (
                        Stage.REFINE.value,
                        run_id,
                        project_id,
                        repository,
                        candidate["number"],
                    ),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    candidate["number"],
                    run_id,
                    "transition",
                    Stage.BACKLOG,
                    Stage.REFINE,
                    {},
                )
            else:
                connection.execute(
                    """
                    UPDATE pbis SET run_id = ?, last_error = NULL
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (run_id, project_id, repository, candidate["number"]),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    candidate["number"],
                    run_id,
                    event_type,
                    current_stage,
                    current_stage,
                    {},
                )
            if agent_session is not None:
                self._upsert_agent_session(connection, run_id, *agent_session)
            return self._run_for_id(connection, run_id)


__all__ = ["ProjectClaimMixin"]
