from __future__ import annotations

import json
import sqlite3
from typing import Any

from beehaiive.models import RunState, RunStatus, Stage

from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _json_list as _json_list
from .helpers.value_helpers import _json_mapping_or_none as _json_mapping_or_none


class RowMappingMixin:
    @staticmethod
    def _pbi_creation_from_row(row: sqlite3.Row) -> dict[str, object]:
        raw_steps: object = row["completed_steps_json"]
        completed_steps: list[object] = _json_list(raw_steps)
        return {
            "project_id": row["project_id"],
            "key_hash": row["key_hash"],
            "request_hash": row["request_hash"],
            "repository": row["repository_name"],
            "status": row["status"],
            "issue_create_started": bool(row["issue_create_started"]),
            "issue_id": row["issue_id"],
            "issue_number": row["issue_number"],
            "issue_url": row["issue_url"],
            "project_item_id": row["project_item_id"],
            "completed_steps": [
                step for step in completed_steps if isinstance(step, str)
            ],
            "current_step": row["current_step"],
            "failed_step": row["failed_step"],
            "failure_code": row["failure_code"],
            "failure_class": row["failure_class"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "result": _json_mapping_or_none(row["result_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> dict[str, object]:
        request = json.loads(str(row["request_json"]))
        result = (
            json.loads(str(row["result_json"]))
            if row["result_json"] is not None
            else None
        )
        return {
            "id": row["action_id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "status": row["status"],
            "repository": row["repository_name"],
            "pbi_number": row["pbi_number"],
            "run_id": row["run_id"],
            "request": request,
            "result": result,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _meta_review_run_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "review_id": row["review_id"],
            "project_id": row["project_id"],
            "status": row["status"],
            "record_limit": row["record_limit"],
            "input_token_limit": row["input_token_limit"],
            "selected_records": row["selected_records"],
            "input_tokens": row["input_tokens"],
            "missing_evidence": _json_list(row["missing_evidence_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _meta_review_suggestion_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "suggestion_id": row["suggestion_id"],
            "project_id": row["project_id"],
            "suggestion_key": row["suggestion_key"],
            "proposed_outcome": row["proposed_outcome"],
            "rationale": row["rationale"],
            "evidence_refs": _json_list(row["evidence_refs_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_review_id": row["last_review_id"],
        }

    def _run_for_id(
        self: Any, connection: sqlite3.Connection, run_id: str
    ) -> RunState | None:
        row = connection.execute(
            """
            SELECT p.project_id, p.repository_name, p.number, p.title, p.stage,
                   p.branch, p.pull_request_url, p.last_error,
                   r.run_id, r.status, r.attempt, r.owner_id, r.lease_token,
                   r.lease_expires_at, r.last_error AS run_error,
                   r.last_result AS run_result, r.task_contract_json,
                   r.task_result_json, r.task_answer, r.admission_generation
            FROM runs AS r
            JOIN pbis AS p
              ON p.project_id = r.project_id
             AND p.repository_name = r.repository_name
             AND p.number = r.pbi_number
            WHERE r.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunState:
        return RunState(
            run_id=str(row["run_id"]),
            project_id=str(row["project_id"]),
            repository=str(row["repository_name"]),
            pbi_number=int(row["number"]),
            title=str(row["title"]),
            stage=Stage(str(row["stage"])),
            status=RunStatus(str(row["status"])),
            attempt=int(row["attempt"]),
            branch=row["branch"],
            pull_request_url=row["pull_request_url"],
            last_error=row["run_error"] or row["last_error"],
            owner_id=row["owner_id"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            last_result=row["run_result"],
            task_contract=_json_mapping_or_none(row["task_contract_json"]),
            task_result=_json_mapping_or_none(row["task_result_json"]),
            task_answer=row["task_answer"],
            admission_generation=row["admission_generation"],
        )

    def _events_for_project(
        self: Any, project_id: str, event_limit: int
    ) -> dict[tuple[str, int], list[dict[str, object]]]:
        rows = self._connection.execute(
            """
            SELECT event_id, repository_name, pbi_number, run_id, event_type,
                   from_stage, to_stage, details_json, created_at
            FROM (
                SELECT event_id, repository_name, pbi_number, run_id,
                       event_type, from_stage, to_stage, details_json,
                       created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY repository_name, pbi_number
                           ORDER BY event_id DESC
                       ) AS event_rank
                FROM events
                WHERE project_id = ?
            )
            WHERE event_rank <= ?
            ORDER BY repository_name, pbi_number, event_id
            """,
            (project_id, event_limit),
        ).fetchall()
        events_by_pbi: dict[tuple[str, int], list[dict[str, object]]] = {}
        for row in rows:
            events_by_pbi.setdefault(
                (str(row["repository_name"]), int(row["pbi_number"])), []
            ).append(
                {
                    "id": row["event_id"],
                    "run_id": row["run_id"],
                    "type": row["event_type"],
                    "from_stage": row["from_stage"],
                    "to_stage": row["to_stage"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
            )
        return events_by_pbi

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        project_id: str,
        repository: str,
        number: int,
        run_id: str | None,
        event_type: str,
        from_stage: Stage,
        to_stage: Stage,
        details: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(
                project_id, repository_name, pbi_number, run_id, event_type,
                from_stage, to_stage, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                repository,
                number,
                run_id,
                event_type,
                from_stage.value,
                to_stage.value,
                json.dumps(details, sort_keys=True),
                _now(),
            ),
        )


__all__ = ["RowMappingMixin"]
