from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.graph import GraphDefinition, GraphDefinitionError

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class GraphDefinitionMixin:
    def save_graph_definition(
        self: Any, definition: GraphDefinition
    ) -> GraphDefinition:
        if not isinstance(cast(object, definition), GraphDefinition):
            raise StoreError("A GraphDefinition is required")
        try:
            definition_data = definition.as_dict()
        except GraphDefinitionError as error:
            raise StoreError(str(error)) from error
        payload = _definition_payload(definition_data)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT definition_json
                FROM graph_definitions
                WHERE workflow_id = ? AND revision = ?
                """,
                (definition.workflow_id, definition.revision),
            ).fetchone()
            if row is not None:
                stored_payload = str(row["definition_json"])
                if stored_payload != payload:
                    try:
                        stored_value = json.loads(stored_payload)
                        equivalent_payload = (
                            _definition_payload(
                                cast(Mapping[str, object], stored_value)
                            )
                            if isinstance(stored_value, Mapping)
                            else ""
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        equivalent_payload = ""
                else:
                    equivalent_payload = payload
                if equivalent_payload != payload:
                    raise StoreError("Graph definition revisions are immutable")
                return definition
            connection.execute(
                """
                INSERT INTO graph_definitions(
                    workflow_id, revision, schema_version, definition_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    definition.workflow_id,
                    definition.revision,
                    definition.schema_version,
                    payload,
                    _now(),
                ),
            )
        return definition

    def graph_definition_for(
        self: Any, workflow_id: str, revision: int | None = None
    ) -> GraphDefinition | None:
        if not isinstance(cast(object, workflow_id), str) or not workflow_id.strip():
            raise StoreError("A workflow id is required")
        query = """
            SELECT definition_json
            FROM graph_definitions
            WHERE workflow_id = ?
        """
        parameters: tuple[object, ...]
        if revision is None:
            query += " ORDER BY revision DESC LIMIT 1"
            parameters = (workflow_id,)
        else:
            if type(revision) is not int or revision <= 0:
                raise StoreError("revision must be a positive integer")
            query += " AND revision = ? LIMIT 1"
            parameters = (workflow_id, revision)
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        try:
            decoded = json.loads(row["definition_json"])
            if not isinstance(decoded, Mapping):
                raise ValueError("definition must be an object")
            return GraphDefinition.from_dict(cast(Mapping[str, object], decoded))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StoreError("Stored graph definition is invalid") from error

    def graph_definitions_for(
        self: Any, workflow_id: str
    ) -> tuple[GraphDefinition, ...]:
        if not isinstance(cast(object, workflow_id), str) or not workflow_id.strip():
            raise StoreError("A workflow id is required")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT definition_json
                FROM graph_definitions
                WHERE workflow_id = ?
                ORDER BY revision
                """,
                (workflow_id,),
            ).fetchall()
        definitions: list[GraphDefinition] = []
        for row in rows:
            try:
                decoded = json.loads(row["definition_json"])
                if not isinstance(decoded, Mapping):
                    raise ValueError("definition must be an object")
                definitions.append(
                    GraphDefinition.from_dict(cast(Mapping[str, object], decoded))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise StoreError("Stored graph definition is invalid") from error
        return tuple(definitions)


__all__ = ["GraphDefinitionMixin"]


def _definition_payload(value: Mapping[str, object]) -> str:
    normalized = dict(value)
    limits = normalized.get("limits")
    if isinstance(limits, Mapping):
        normalized_limits = dict(cast(Mapping[str, object], limits))
        if "timeout_seconds" in normalized_limits:
            normalized_limits["timeout_seconds"] = float(
                cast(float, normalized_limits["timeout_seconds"])
            )
        normalized["limits"] = normalized_limits
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
