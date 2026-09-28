from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.behavior import BehaviorRecord, BehaviorStatus

from .errors import StateConflictError, StoreError
from .helpers.lease_helpers import _now


class UnitBehaviorMixin:
    def create_unit_behavior(
        self: Any,
        behavior_id: str,
        project_id: str,
        definition: Mapping[str, object],
        bindings: Mapping[str, object],
        *,
        created_at: str | None = None,
    ) -> BehaviorRecord:
        timestamp = created_at or _now()
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO unit_behaviors(
                        behavior_id, project_id, status, definition_json,
                        bindings_json, assignment_json, execution_json,
                        created_at, updated_at
                    ) VALUES (?, ?, 'draft', ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        behavior_id,
                        project_id,
                        _json(definition),
                        _json(bindings),
                        _json({}),
                        timestamp,
                        timestamp,
                    ),
                )
            except Exception as exc:
                raise StoreError("Behavior could not be created") from exc
        record = self.unit_behavior_for(project_id, behavior_id)
        if record is None:
            raise StoreError("Created behavior could not be read back")
        return record

    def update_unit_behavior(
        self: Any,
        project_id: str,
        behavior_id: str,
        *,
        status: BehaviorStatus | None = None,
        bindings: Mapping[str, object] | None = None,
        assignment: Mapping[str, object] | None = None,
        execution: Mapping[str, object] | None = None,
        expected_status: BehaviorStatus | None = None,
    ) -> BehaviorRecord:
        fields: list[str] = []
        values: list[object] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if bindings is not None:
            fields.append("bindings_json = ?")
            values.append(_json(bindings))
        if assignment is not None:
            fields.append("assignment_json = ?")
            values.append(_json(assignment))
        if execution is not None:
            fields.append("execution_json = ?")
            values.append(_json(execution))
        if not fields:
            record = self.unit_behavior_for(project_id, behavior_id)
            if record is None:
                raise StoreError("Behavior not found")
            return record
        fields.append("updated_at = ?")
        values.append(_now())
        values.extend((project_id, behavior_id))
        status_clause = ""
        if expected_status is not None:
            status_clause = " AND status = ?"
            values.append(expected_status)
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE unit_behaviors SET "
                + ", ".join(fields)
                + " WHERE project_id = ? AND behavior_id = ?"
                + status_clause,
                tuple(values),
            )
            if result.rowcount != 1:
                raise StateConflictError(
                    "Behavior state changed before it could be updated"
                )
        record = self.unit_behavior_for(project_id, behavior_id)
        if record is None:
            raise StoreError("Updated behavior could not be read back")
        return record

    def unit_behavior_for(
        self: Any, project_id: str, behavior_id: str
    ) -> BehaviorRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT behavior_id, project_id, status, definition_json,
                       bindings_json, assignment_json, execution_json,
                       created_at, updated_at
                FROM unit_behaviors
                WHERE project_id = ? AND behavior_id = ?
                """,
                (project_id, behavior_id),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def unit_behaviors_for(self: Any, project_id: str) -> tuple[BehaviorRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT behavior_id, project_id, status, definition_json,
                       bindings_json, assignment_json, execution_json,
                       created_at, updated_at
                FROM unit_behaviors
                WHERE project_id = ?
                ORDER BY created_at, behavior_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)


def _json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StoreError("Behavior data is not JSON serializable") from exc


def _record_from_row(row: Any) -> BehaviorRecord:
    try:
        definition = json.loads(str(row["definition_json"]))
        bindings = json.loads(str(row["bindings_json"]))
        assignment_raw = row["assignment_json"]
        assignment_value = (
            None if assignment_raw is None else json.loads(str(assignment_raw))
        )
        execution = json.loads(str(row["execution_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError("Stored behavior JSON is invalid") from exc
    if not isinstance(definition, dict) or not isinstance(bindings, dict):
        raise StoreError("Stored behavior data is invalid")
    if assignment_value is not None and not isinstance(assignment_value, dict):
        raise StoreError("Stored behavior assignment is invalid")
    if not isinstance(execution, dict):
        raise StoreError("Stored behavior execution is invalid")
    status = str(row["status"])
    if status not in {
        "draft",
        "confirmed",
        "assigned",
        "running",
        "completed",
        "failed",
    }:
        raise StoreError("Stored behavior status is invalid")
    return BehaviorRecord(
        behavior_id=str(row["behavior_id"]),
        project_id=str(row["project_id"]),
        status=cast(BehaviorStatus, status),
        definition=cast(dict[str, object], definition),
        bindings=cast(dict[str, object], bindings),
        assignment=cast(dict[str, object] | None, assignment_value),
        execution=cast(dict[str, object], execution),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = ["UnitBehaviorMixin"]
