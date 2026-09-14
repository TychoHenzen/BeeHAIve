from __future__ import annotations

import json
from typing import Any

from beehaiive.models import ProjectSnapshot, RunStatus, Stage

from .constants import _STAGE_ORDER as _STAGE_ORDER
from .errors import StoreError
from .helpers.claimability import _task_claimability_state as _task_claimability_state
from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _archive_eligible as _archive_eligible


class ProjectSyncMixin:
    def sync_project(self: Any, snapshot: ProjectSnapshot) -> tuple[str, ...]:
        removed_run_ids: list[str] = []
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (snapshot.project_id, snapshot.name, _now()),
            )
            connection.execute(
                "UPDATE repositories SET active = 0 WHERE project_id = ?",
                (snapshot.project_id,),
            )
            connection.execute(
                "UPDATE pbis SET active = 0, archived = 0 WHERE project_id = ?",
                (snapshot.project_id,),
            )
            for repository in snapshot.repositories:
                connection.execute(
                    """
                    INSERT INTO repositories(project_id, name, active)
                    VALUES (?, ?, 1)
                    ON CONFLICT(project_id, name) DO UPDATE SET active = 1
                    """,
                    (snapshot.project_id, repository.name),
                )
                for pbi in repository.pbis:
                    if pbi.repository != repository.name:
                        raise StoreError(
                            "PBI repository does not match its repository snapshot"
                        )
                    incoming_stage = (
                        pbi.stage
                        if pbi.claimable
                        and pbi.stage
                        in {
                            Stage.BACKLOG,
                            Stage.REFINE,
                            Stage.IMPLEMENT,
                        }
                        else None
                    )
                    existing = connection.execute(
                        """
                        SELECT p.stage, p.run_id, p.claimable,
                               r.status AS run_status, r.task_contract_json,
                               r.task_result_json, r.task_answer,
                               r.task_answer_resumed
                        FROM pbis AS p
                        LEFT JOIN runs AS r
                          ON r.project_id = p.project_id
                         AND r.repository_name = p.repository_name
                         AND r.pbi_number = p.number
                        WHERE p.project_id = ? AND p.repository_name = ?
                          AND p.number = ?
                        """,
                        (snapshot.project_id, pbi.repository, pbi.number),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO pbis(
                                project_id, repository_name, number, title,
                                stage, active, archived, planning_status, claimable,
                                metadata_json
                            )
                            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            """,
                            (
                                snapshot.project_id,
                                pbi.repository,
                                pbi.number,
                                pbi.title,
                                (incoming_stage or Stage.BACKLOG).value,
                                int(_archive_eligible(pbi)),
                                pbi.planning_status,
                                int(incoming_stage is not None),
                                json.dumps(dict(pbi.metadata), sort_keys=True),
                            ),
                        )
                        continue

                    current_stage = Stage(str(existing["stage"]))
                    task_pause, _ = _task_claimability_state(
                        existing["task_contract_json"],
                        existing["task_result_json"],
                        existing["task_answer"],
                        existing["task_answer_resumed"],
                    )
                    merged_stage = (
                        max(
                            (current_stage, incoming_stage),
                            key=lambda stage: _STAGE_ORDER[stage],
                        )
                        if incoming_stage is not None
                        else current_stage
                    )
                    connection.execute(
                        """
                        UPDATE pbis
                        SET title = ?, stage = ?, active = 1, archived = ?,
                            metadata_json = ?,
                            planning_status = ?, claimable = ?,
                            last_error = CASE
                                WHEN ? = ? THEN last_error
                                ELSE NULL
                            END
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (
                            pbi.title,
                            merged_stage.value,
                            int(
                                _archive_eligible(
                                    pbi,
                                    str(existing["run_status"])
                                    if isinstance(existing["run_status"], str)
                                    else None,
                                )
                            ),
                            json.dumps(dict(pbi.metadata), sort_keys=True),
                            pbi.planning_status,
                            int(
                                incoming_stage is not None
                                and merged_stage is not Stage.PULL_REQUEST
                                and existing["run_status"] != RunStatus.COMPLETED.value
                                and (
                                    existing["run_status"]
                                    != RunStatus.AWAITING_OPERATOR.value
                                    or bool(existing["task_answer_resumed"])
                                )
                                and not task_pause
                            ),
                            current_stage.value,
                            merged_stage.value,
                            snapshot.project_id,
                            pbi.repository,
                            pbi.number,
                        ),
                    )
                    if merged_stage is not current_stage:
                        run_id = existing["run_id"]
                        if run_id is not None:
                            run_id = str(run_id)
                        self._record_event(
                            connection,
                            snapshot.project_id,
                            pbi.repository,
                            pbi.number,
                            run_id,
                            "external_sync",
                            current_stage,
                            merged_stage,
                            {
                                "source_stage": pbi.planning_status
                                or (
                                    pbi.stage.value
                                    if pbi.stage is not None
                                    else "unknown"
                                )
                            },
                        )
            removed_runs = connection.execute(
                """
                SELECT r.run_id, p.repository_name, p.number, p.stage
                FROM runs AS r
                JOIN pbis AS p
                  ON p.project_id = r.project_id
                 AND p.repository_name = r.repository_name
                 AND p.number = r.pbi_number
                WHERE r.project_id = ? AND r.status = 'active' AND p.active = 0
                """,
                (snapshot.project_id,),
            ).fetchall()
            for removed_run in removed_runs:
                removed_run_ids.append(str(removed_run["run_id"]))
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'failed', execution_token = NULL,
                        last_error = ?, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        "PBI is no longer linked to the selected project",
                        _now(),
                        removed_run["run_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE pbis SET last_error = ?
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (
                        "PBI is no longer linked to the selected project",
                        snapshot.project_id,
                        removed_run["repository_name"],
                        removed_run["number"],
                    ),
                )
                self._record_event(
                    connection,
                    snapshot.project_id,
                    str(removed_run["repository_name"]),
                    int(removed_run["number"]),
                    str(removed_run["run_id"]),
                    "removed",
                    Stage(str(removed_run["stage"])),
                    Stage(str(removed_run["stage"])),
                    {"reason": "pbi no longer linked to selected project"},
                )
        return tuple(removed_run_ids)


__all__ = ["ProjectSyncMixin"]
