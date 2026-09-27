from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.building_signal import (
    BuildingSignalRecord,
    BuildingSignalStatus,
    initial_signal_state,
)

from .errors import StoreError
from .helpers.lease_helpers import _now


class BuildingSignalAssignmentConflict(StoreError):
    """Raised when a building already has an assigned signal rule."""


class BuildingSignalMixin:
    def create_building_signal_rule(
        self: Any,
        rule_id: str,
        project_id: str,
        rule: Mapping[str, object],
        *,
        created_at: str | None = None,
    ) -> BuildingSignalRecord:
        timestamp = created_at or _now()
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO building_signal_rules(
                        rule_id, project_id, status, rule_json, assignment_json,
                        signal_state_json, created_at, updated_at
                    ) VALUES (?, ?, 'draft', ?, NULL, ?, ?, ?)
                    """,
                    (
                        rule_id,
                        project_id,
                        _json(rule),
                        _json(initial_signal_state()),
                        timestamp,
                        timestamp,
                    ),
                )
            except Exception as exc:
                raise StoreError("Building signal rule could not be created") from exc
        record = self.building_signal_rule_for(project_id, rule_id)
        if record is None:
            raise StoreError("Created building signal rule could not be read back")
        return record

    def update_building_signal_rule(
        self: Any,
        project_id: str,
        rule_id: str,
        *,
        rule: Mapping[str, object] | None = None,
        status: BuildingSignalStatus | None = None,
        assignment: Mapping[str, object] | None = None,
        signal_state: Mapping[str, object] | None = None,
        expected_status: BuildingSignalStatus | None = None,
    ) -> BuildingSignalRecord:
        fields: list[str] = []
        values: list[object] = []
        if rule is not None:
            fields.append("rule_json = ?")
            values.append(_json(rule))
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if assignment is not None:
            fields.append("assignment_json = ?")
            values.append(_json(assignment))
        if signal_state is not None:
            fields.append("signal_state_json = ?")
            values.append(_json(signal_state))
        if not fields:
            record = self.building_signal_rule_for(project_id, rule_id)
            if record is None:
                raise StoreError("Building signal rule not found")
            return record
        fields.append("updated_at = ?")
        values.append(_now())
        values.extend((project_id, rule_id))
        status_clause = ""
        if expected_status is not None:
            status_clause = " AND status = ?"
            values.append(expected_status)
        try:
            with self._transaction() as connection:
                result = connection.execute(
                    "UPDATE building_signal_rules SET "
                    + ", ".join(fields)
                    + " WHERE project_id = ? AND rule_id = ?"
                    + status_clause,
                    tuple(values),
                )
                if result.rowcount != 1:
                    raise StoreError(
                        "Building signal rule state changed before it could be updated"
                    )
        except sqlite3.IntegrityError as exc:
            raise BuildingSignalAssignmentConflict(
                "A building signal rule is already assigned to that building"
            ) from exc
        record = self.building_signal_rule_for(project_id, rule_id)
        if record is None:
            raise StoreError("Updated building signal rule could not be read back")
        return record

    def building_signal_rule_for(
        self: Any, project_id: str, rule_id: str
    ) -> BuildingSignalRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT rule_id, project_id, status, rule_json, assignment_json,
                       signal_state_json, created_at, updated_at
                FROM building_signal_rules
                WHERE project_id = ? AND rule_id = ?
                """,
                (project_id, rule_id),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def building_signal_rules_for(
        self: Any, project_id: str
    ) -> tuple[BuildingSignalRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT rule_id, project_id, status, rule_json, assignment_json,
                       signal_state_json, created_at, updated_at
                FROM building_signal_rules
                WHERE project_id = ?
                ORDER BY created_at, rule_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)


def _json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StoreError("Building signal data is not JSON serializable") from exc


def _record_from_row(row: Any) -> BuildingSignalRecord:
    try:
        rule = json.loads(str(row["rule_json"]))
        assignment_raw = row["assignment_json"]
        assignment_value = (
            None if assignment_raw is None else json.loads(str(assignment_raw))
        )
        signal_state = json.loads(str(row["signal_state_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError("Stored building signal JSON is invalid") from exc
    if not isinstance(rule, dict) or not isinstance(signal_state, dict):
        raise StoreError("Stored building signal data is invalid")
    if assignment_value is not None and not isinstance(assignment_value, dict):
        raise StoreError("Stored building signal assignment is invalid")
    status = str(row["status"])
    if status not in {"draft", "confirmed", "assigned"}:
        raise StoreError("Stored building signal status is invalid")
    return BuildingSignalRecord(
        rule_id=str(row["rule_id"]),
        project_id=str(row["project_id"]),
        status=cast(BuildingSignalStatus, status),
        rule=cast(dict[str, object], rule),
        assignment=cast(dict[str, object] | None, assignment_value),
        signal_state=cast(dict[str, object], signal_state),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = ["BuildingSignalAssignmentConflict", "BuildingSignalMixin"]
