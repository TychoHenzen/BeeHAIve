from __future__ import annotations

from typing import Any

from beehaiive.models import RunState

from .constants import DEFAULT_EVENT_LIMIT as DEFAULT_EVENT_LIMIT
from .constants import MAX_EVENT_LIMIT as MAX_EVENT_LIMIT
from .errors import StoreError
from .helpers.value_helpers import _json_mapping as _json_mapping
from .helpers.value_helpers import _json_mapping_or_none as _json_mapping_or_none


class ProjectReadMixin:
    def project_state(
        self: Any,
        project_id: str,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        if not 1 <= event_limit <= MAX_EVENT_LIMIT:
            raise StoreError(f"event_limit must be between 1 and {MAX_EVENT_LIMIT}")
        with self._lock:
            project = self._connection.execute(
                """
                SELECT project_id, name, updated_at
                FROM projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if project is None:
                raise StoreError(f"Unknown project: {project_id}")
            events_by_pbi = self._events_for_project(project_id, event_limit)
            evidence_by_pbi = self.lifecycle_evidence_for_project(project_id)
            repositories: list[dict[str, object]] = []
            repository_rows = self._connection.execute(
                """
                SELECT name, active
                FROM repositories
                WHERE project_id = ?
                ORDER BY name
                """,
                (project_id,),
            ).fetchall()
            for repository_row in repository_rows:
                repository = str(repository_row["name"])
                writer_row = (
                    self._connection.execute(
                        """
                        SELECT run_id, pbi_number, owner_id, lease_expires_at,
                               updated_at
                        FROM runs
                        WHERE project_id = ?
                          AND repository_name = ?
                          AND status = 'active'
                        """,
                        (project_id, repository),
                    ).fetchone()
                    if repository_row["active"]
                    else None
                )
                writer: dict[str, object] | None = None
                if writer_row is not None:
                    writer = {
                        "run_id": writer_row["run_id"],
                        "pbi_number": writer_row["pbi_number"],
                        "owner_id": writer_row["owner_id"],
                        "lease_expires_at": writer_row["lease_expires_at"],
                        "updated_at": writer_row["updated_at"],
                    }
                pbis: list[dict[str, object]] = []
                pbi_rows = self._connection.execute(
                    """
                    SELECT p.*, r.status, r.attempt, r.last_result AS run_result,
                           r.task_contract_json, r.task_result_json, r.task_answer
                    FROM pbis AS p
                    LEFT JOIN runs AS r
                      ON r.project_id = p.project_id
                     AND r.repository_name = p.repository_name
                     AND r.pbi_number = p.number
                    WHERE p.project_id = ? AND p.repository_name = ?
                    ORDER BY p.number
                    """,
                    (project_id, repository),
                ).fetchall()
                for pbi_row in pbi_rows:
                    pbi_key = (repository, int(pbi_row["number"]))
                    events = events_by_pbi.get(pbi_key, [])
                    transition_evidence = evidence_by_pbi.get(pbi_key, [])
                    task_result = _json_mapping_or_none(pbi_row["task_result_json"])
                    required_action = (
                        task_result.get("required_action")
                        if task_result is not None
                        else None
                    )
                    if (
                        not isinstance(required_action, str)
                        or not required_action.strip()
                    ):
                        required_action = None
                    canonical_lifecycle = {
                        "state": str(pbi_row["canonical_state"] or "unknown"),
                        "facts": _json_mapping(pbi_row["canonical_facts_json"]),
                        "source_version": str(
                            pbi_row["canonical_source_version"] or ""
                        ),
                        "reason_code": (
                            transition_evidence[-1]["reason_code"]
                            if transition_evidence
                            else "evidence_missing"
                        ),
                        "required_action": required_action,
                        "transition_evidence": transition_evidence,
                    }
                    pbi: dict[str, object] = {
                        "id": f"{repository}#{pbi_row['number']}",
                        "number": pbi_row["number"],
                        "title": pbi_row["title"],
                        "stage": pbi_row["stage"],
                        "run_id": pbi_row["run_id"],
                        "metadata": _json_mapping(pbi_row["metadata_json"]),
                        "status": pbi_row["status"],
                        "attempt": pbi_row["attempt"],
                        "branch": pbi_row["branch"],
                        "pull_request_url": pbi_row["pull_request_url"],
                        "last_error": pbi_row["last_error"],
                        "result": pbi_row["run_result"],
                        "task_contract": _json_mapping_or_none(
                            pbi_row["task_contract_json"]
                        ),
                        "task_result": task_result,
                        "task_answer": pbi_row["task_answer"],
                        "canonical_lifecycle": canonical_lifecycle,
                        "operator_questions": (
                            self.operator_questions_for_run(str(pbi_row["run_id"]))
                            if pbi_row["run_id"]
                            else []
                        ),
                        "active": bool(pbi_row["active"]),
                        "archived": bool(pbi_row["archived"]),
                        "planning_status": pbi_row["planning_status"],
                        "claimable": bool(pbi_row["claimable"]),
                        "events": events,
                    }
                    agent_session = (
                        self._agent_session_for_run(
                            self._connection, str(pbi_row["run_id"])
                        )
                        if pbi_row["run_id"]
                        else None
                    )
                    if agent_session is not None:
                        pbi["agent_session"] = agent_session
                    pbis.append(pbi)
                repositories.append(
                    {
                        "name": repository,
                        "active": bool(repository_row["active"]),
                        "writer": writer,
                        "pbis": pbis,
                    }
                )
            return {
                "project_id": project["project_id"],
                "name": project["name"],
                "updated_at": project["updated_at"],
                "event_limit": event_limit,
                "repositories": repositories,
            }

    def is_active_repository(self: Any, project_id: str, repository: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM repositories
                WHERE project_id = ? AND name = ? AND active = 1
                """,
                (project_id, repository),
            ).fetchone()
            return row is not None

    def get_run(self: Any, run_id: str) -> RunState | None:
        with self._lock:
            return self._run_for_id(self._connection, run_id)


__all__ = ["ProjectReadMixin"]
