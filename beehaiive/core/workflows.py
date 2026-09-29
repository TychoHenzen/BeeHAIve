from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .database import SnapshotDatabase

SCHEMA_VERSION = 1
PARAMETER_TYPES = frozenset(
    {"status", "label", "skill", "repository", "item_type", "text", "number", "boolean"}
)
ITEM_CONDITION_KINDS = frozenset(
    {
        "item_status_is",
        "item_type_is",
        "item_has_label",
        "item_lacks_label",
        "item_repository_is",
    }
)
CONDITION_KINDS = ITEM_CONDITION_KINDS | {"outcome_is", "always"}
_PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_DEFINITION_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "description",
        "source_prompt",
        "auto_reset_on_stall",
        "max_steps_per_pass",
        "parameters",
        "initial",
        "states",
        "transitions",
    }
)
_PARAMETER_FIELDS = frozenset({"name", "type", "const", "value"})
_STATE_FIELDS = frozenset(
    {"id", "title", "action", "layout", "max_visits", "skill", "prompt", "outcomes"}
)
_TRANSITION_FIELDS = frozenset({"from", "to", "priority", "conditions"})
_CONDITION_FIELDS = frozenset({"kind", "value"})


@dataclass(frozen=True)
class WorkflowIssue:
    location: str
    message: str
    level: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "location": self.location,
            "message": self.message,
            "level": self.level,
        }


@dataclass(frozen=True)
class WorkflowValidation:
    definition: dict[str, Any]
    errors: tuple[WorkflowIssue, ...]
    warnings: tuple[WorkflowIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "definition": self.definition,
            "errors": [issue.as_dict() for issue in self.errors],
            "warnings": [issue.as_dict() for issue in self.warnings],
        }


def _validate_definition_shape(
    value: Any,
    error: Callable[[str, str], None],
) -> bool:
    valid = True

    def invalid(location: str, message: str) -> None:
        nonlocal valid
        valid = False
        error(location, message)

    def check_keys(
        value_object: Mapping[Any, Any],
        location: str,
        allowed: frozenset[str],
        required: frozenset[str],
    ) -> None:
        for key in value_object:
            if not isinstance(key, str) or key not in allowed:
                invalid(f"{location}.{key}", "unknown field")
        for key in required:
            if key not in value_object:
                invalid(f"{location}.{key}", "is required")

    if not isinstance(value, Mapping):
        invalid("definition", "must be an object")
        return False
    definition = cast(Mapping[str, Any], value)
    check_keys(
        definition,
        "definition",
        _DEFINITION_FIELDS,
        frozenset(_DEFINITION_FIELDS),
    )
    if not isinstance(definition.get("schema_version"), int) or isinstance(
        definition.get("schema_version"), bool
    ):
        invalid("schema_version", "must be an integer")
    for field in ("name", "description", "source_prompt", "initial"):
        if not isinstance(definition.get(field), str):
            invalid(field, "must be a string")
    if not isinstance(definition.get("auto_reset_on_stall"), bool):
        invalid("auto_reset_on_stall", "must be a boolean")
    if not isinstance(definition.get("max_steps_per_pass"), int) or isinstance(
        definition.get("max_steps_per_pass"), bool
    ):
        invalid("max_steps_per_pass", "must be an integer")

    parameters = definition.get("parameters")
    if not isinstance(parameters, list):
        invalid("parameters", "must be an array")
    else:
        for index, raw_parameter in enumerate(cast(list[Any], parameters)):
            location = f"parameters[{index}]"
            if not isinstance(raw_parameter, Mapping):
                invalid(location, "must be an object")
                continue
            parameter = cast(Mapping[str, Any], raw_parameter)
            check_keys(
                parameter,
                location,
                _PARAMETER_FIELDS,
                frozenset(_PARAMETER_FIELDS),
            )
            if not isinstance(parameter.get("name"), str):
                invalid(f"{location}.name", "must be a string")
            if not isinstance(parameter.get("type"), str):
                invalid(f"{location}.type", "must be a string")
            if not isinstance(parameter.get("const"), bool):
                invalid(f"{location}.const", "must be a boolean")

    states = definition.get("states")
    if not isinstance(states, list):
        invalid("states", "must be an array")
    else:
        for index, raw_state in enumerate(cast(list[Any], states)):
            location = f"states[{index}]"
            if not isinstance(raw_state, Mapping):
                invalid(location, "must be an object")
                continue
            state = cast(Mapping[str, Any], raw_state)
            check_keys(
                state,
                location,
                _STATE_FIELDS,
                frozenset({"id", "title", "action", "max_visits"}),
            )
            if not isinstance(state.get("id"), str):
                invalid(f"{location}.id", "must be a string")
            if not isinstance(state.get("title"), str):
                invalid(f"{location}.title", "must be a string")
            if not isinstance(state.get("action"), str):
                invalid(f"{location}.action", "must be a string")
            if not isinstance(state.get("max_visits"), int) or isinstance(
                state.get("max_visits"), bool
            ):
                invalid(f"{location}.max_visits", "must be an integer")
            for field in ("skill", "prompt"):
                if (
                    field in state
                    and state[field] is not None
                    and not isinstance(state[field], str)
                ):
                    invalid(f"{location}.{field}", "must be a string or null")
            if "outcomes" in state:
                outcomes = state["outcomes"]
                if not isinstance(outcomes, list) or not all(
                    isinstance(outcome, str) for outcome in cast(list[Any], outcomes)
                ):
                    invalid(f"{location}.outcomes", "must be an array of strings")
            if "layout" in state:
                layout = state["layout"]
                if not isinstance(layout, Mapping):
                    invalid(f"{location}.layout", "must be an object")
                else:
                    layout_object = cast(Mapping[str, Any], layout)
                    check_keys(
                        layout_object,
                        f"{location}.layout",
                        frozenset({"x", "y"}),
                        frozenset({"x", "y"}),
                    )
                    for axis in ("x", "y"):
                        if not isinstance(
                            layout_object.get(axis), (int, float)
                        ) or isinstance(layout_object.get(axis), bool):
                            invalid(f"{location}.layout.{axis}", "must be a number")
            action = state.get("action")
            if action == "run_skill":
                for field in ("skill", "prompt", "outcomes"):
                    if field not in state:
                        invalid(f"{location}.{field}", "is required for run_skill")

    transitions = definition.get("transitions")
    if not isinstance(transitions, list):
        invalid("transitions", "must be an array")
    else:
        for index, raw_transition in enumerate(cast(list[Any], transitions)):
            location = f"transitions[{index}]"
            if not isinstance(raw_transition, Mapping):
                invalid(location, "must be an object")
                continue
            transition = cast(Mapping[str, Any], raw_transition)
            check_keys(
                transition,
                location,
                _TRANSITION_FIELDS,
                frozenset(_TRANSITION_FIELDS),
            )
            for field in ("from", "to"):
                if not isinstance(transition.get(field), str):
                    invalid(f"{location}.{field}", "must be a string")
            if not isinstance(transition.get("priority"), int) or isinstance(
                transition.get("priority"), bool
            ):
                invalid(f"{location}.priority", "must be an integer")
            conditions = transition.get("conditions")
            if not isinstance(conditions, list):
                invalid(f"{location}.conditions", "must be an array")
                continue
            for condition_index, raw_condition in enumerate(
                cast(list[Any], conditions)
            ):
                condition_location = f"{location}.conditions[{condition_index}]"
                if not isinstance(raw_condition, Mapping):
                    invalid(condition_location, "must be an object")
                    continue
                condition = cast(Mapping[str, Any], raw_condition)
                check_keys(
                    condition,
                    condition_location,
                    _CONDITION_FIELDS,
                    frozenset({"kind"}),
                )
                if not isinstance(condition.get("kind"), str):
                    invalid(f"{condition_location}.kind", "must be a string")
                if (
                    "value" in condition
                    and condition["value"] is not None
                    and not isinstance(condition["value"], str)
                ):
                    invalid(
                        f"{condition_location}.value",
                        "must be a string or null",
                    )
                if condition.get("kind") != "always" and "value" not in condition:
                    invalid(f"{condition_location}.value", "is required")

    return valid


def validate_workflow(
    value: Any,
    *,
    skills_dirs: Sequence[Path] = (),
    status_options: Iterable[str] = (),
) -> WorkflowValidation:
    errors: list[WorkflowIssue] = []
    warnings: list[WorkflowIssue] = []
    statuses = {str(option) for option in status_options}

    def error(location: str, message: str) -> None:
        errors.append(WorkflowIssue(location, message))

    def warning(location: str, message: str) -> None:
        warnings.append(WorkflowIssue(location, message, "warning"))

    if not _validate_definition_shape(value, error):
        return WorkflowValidation(
            normalize_workflow(value), tuple(errors), tuple(warnings)
        )
    definition = normalize_workflow(value)

    if definition["schema_version"] != SCHEMA_VERSION:
        error("schema_version", f"must be {SCHEMA_VERSION}")
    if not str(definition["name"]).strip():
        error("name", "must not be empty")
    prompt_size = len(str(definition.get("source_prompt", "")))
    if prompt_size > 8192:
        error("source_prompt", "must be at most 8 KB")
    max_steps = definition.get("max_steps_per_pass")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool):
        error("max_steps_per_pass", "must be an integer")
    elif max_steps < 1 or max_steps > 100:
        error("max_steps_per_pass", "must be between 1 and 100")

    raw_parameters = cast(list[dict[str, Any]], definition["parameters"])
    if len(raw_parameters) > 32:
        error("parameters", "must contain at most 32 parameters")
    parameters: dict[str, dict[str, Any]] = {}
    parameter_uses: defaultdict[str, list[str]] = defaultdict(list)
    for index, parameter in enumerate(raw_parameters):
        location = f"parameters[{index}]"
        name = str(parameter.get("name", ""))
        parameter_type = str(parameter.get("type", ""))
        if not _PARAMETER_NAME.fullmatch(name):
            error(f"{location}.name", "must match [a-z][a-z0-9_]{0,31}")
        if name in parameters:
            error(f"{location}.name", f"duplicates parameter {name!r}")
        else:
            parameters[name] = parameter
        if parameter_type not in PARAMETER_TYPES:
            error(f"{location}.type", "is not a supported parameter type")
        is_const = parameter.get("const") is True
        if is_const and parameter.get("value") is None:
            error(f"{location}.value", "is required for a const parameter")
        if not is_const and parameter.get("value") is not None:
            error(f"{location}.value", "must be null for a non-const parameter")
        if is_const and parameter_type in PARAMETER_TYPES:
            _validate_parameter_value(
                parameter.get("value"),
                parameter_type,
                f"{location}.value",
                error,
            )
            if parameter_type == "status":
                _validate_status(
                    str(parameter.get("value")),
                    statuses,
                    f"{location}.value",
                    error,
                    warning,
                )

    _record_text_placeholders(
        str(definition.get("source_prompt", "")),
        "source_prompt",
        parameter_uses,
        error,
        parameters,
        expected_type="text",
    )

    raw_states = cast(list[dict[str, Any]], definition["states"])
    if len(raw_states) > 32:
        error("states", "must contain at most 32 states")
    states: dict[str, dict[str, Any]] = {}
    for index, state in enumerate(raw_states):
        location = f"states[{index}]"
        state_id = str(state.get("id", ""))
        if not state_id:
            error(f"{location}.id", "must not be empty")
        if state_id in states:
            error(f"{location}.id", f"duplicates state {state_id!r}")
        else:
            states[state_id] = state
        action = str(state.get("action", ""))
        if action not in {"wait_for_work", "run_skill", "escalate"}:
            error(f"{location}.action", "must be wait_for_work, run_skill, or escalate")
        max_visits = state.get("max_visits")
        if not isinstance(max_visits, int) or isinstance(max_visits, bool):
            error(f"{location}.max_visits", "must be an integer")
        elif max_visits < 1:
            error(f"{location}.max_visits", "must be positive")
        _record_placeholders(
            state,
            location,
            parameter_uses,
            error,
            parameters,
            expected_type="text",
        )
        if action == "run_skill":
            state_prompt = state.get("prompt")
            if not isinstance(state_prompt, str) or not state_prompt.strip():
                error(f"{location}.prompt", "must not be empty")
            elif len(state_prompt) > 8192:
                error(f"{location}.prompt", "must be at most 8 KB")
            outcomes = _string_list(state.get("outcomes"))
            if not outcomes:
                error(f"{location}.outcomes", "must contain at least one outcome")
            if len(outcomes) != len(set(outcomes)):
                error(f"{location}.outcomes", "must not contain duplicates")
            skill = state.get("skill")
            if not isinstance(skill, str) or not skill.strip():
                error(f"{location}.skill", "must name a skill or skill parameter")
            elif _is_placeholder(skill):
                _check_placeholder_type(
                    skill, "skill", f"{location}.skill", parameters, error
                )
            elif not _skill_exists(skill, skills_dirs):
                error(
                    f"{location}.skill",
                    f"skill {skill!r} does not resolve to <dir>/{skill}/SKILL.md",
                )
        if action == "escalate" and state.get("skill") is not None:
            error(f"{location}.skill", "is not valid for an escalate state")

    transitions = cast(list[dict[str, Any]], definition["transitions"])
    if len(transitions) > 128:
        error("transitions", "must contain at most 128 transitions")
    outgoing: defaultdict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    graph: defaultdict[str, set[str]] = defaultdict(set)
    reverse_graph: defaultdict[str, set[str]] = defaultdict(set)
    for index, transition in enumerate(transitions):
        location = f"transitions[{index}]"
        source = str(transition.get("from", ""))
        target = str(transition.get("to", ""))
        if source not in states:
            error(f"{location}.from", f"references unknown state {source!r}")
        if target not in states:
            error(f"{location}.to", f"references unknown state {target!r}")
        priority = transition.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            error(f"{location}.priority", "must be an integer")
        raw_conditions = transition.get("conditions", [])
        conditions = (
            cast(list[Any], raw_conditions) if isinstance(raw_conditions, list) else []
        )
        if not conditions:
            error(f"{location}.conditions", "must contain at least one condition")
            conditions = []
        if source in states:
            outgoing[source].append((index, transition))
            graph[source].add(target)
            reverse_graph[target].add(source)
        for condition_index, condition in enumerate(conditions):
            condition_location = f"{location}.conditions[{condition_index}]"
            if not isinstance(condition, dict):
                error(condition_location, "must be an object")
                continue
            condition_object = cast(dict[str, Any], condition)
            kind = str(condition_object.get("kind", ""))
            if kind not in CONDITION_KINDS:
                error(f"{condition_location}.kind", "is not a supported condition kind")
                continue
            if kind == "always":
                if condition_object.get("value") is not None:
                    error(f"{condition_location}.value", "must be omitted for always")
                continue
            if "value" not in condition_object:
                error(f"{condition_location}.value", "is required")
                continue
            value_for_placeholder = condition_object.get("value")
            if isinstance(value_for_placeholder, str):
                _record_text_placeholders(
                    value_for_placeholder,
                    condition_location,
                    parameter_uses,
                    error,
                    parameters,
                )
            _validate_condition_value(
                kind,
                value_for_placeholder,
                condition_location,
                parameters,
                statuses,
                error,
                warning,
            )

    initial = str(definition.get("initial", ""))
    if initial not in states:
        error("initial", f"references unknown state {initial!r}")
    elif states[initial].get("action") != "wait_for_work":
        error("initial", "must identify a wait_for_work state")

    if initial in states:
        reachable = _reachable(initial, graph)
        for state_id in states:
            if state_id not in reachable:
                error(f"states[{state_id}]", "is not reachable from initial")
        reverse_reachable = _reachable(initial, reverse_graph)
        for state_id in states:
            if state_id not in reverse_reachable:
                error(f"states[{state_id}]", "cannot reach initial")

    for state_id, state in states.items():
        state_edges = outgoing.get(state_id, [])
        action = state.get("action")
        if action == "wait_for_work":
            if not state_edges:
                error(f"states[{state_id}]", "wait_for_work must have an outgoing edge")
            for transition_index, transition in state_edges:
                raw_conditions = transition.get("conditions", [])
                conditions = (
                    cast(list[Any], raw_conditions)
                    if isinstance(raw_conditions, list)
                    else []
                )
                if not any(
                    _condition_kind(condition) in ITEM_CONDITION_KINDS
                    for condition in conditions
                ):
                    error(
                        f"transitions[{transition_index}].conditions",
                        "wait_for_work edges need an item condition",
                    )
                if any(
                    _condition_kind(condition) == "outcome_is"
                    for condition in conditions
                ):
                    error(
                        f"transitions[{transition_index}].conditions",
                        "wait_for_work edges cannot use outcome_is",
                    )
        elif action == "run_skill":
            outcomes = set(_string_list(state.get("outcomes")))
            matched: set[str] = set()
            for transition_index, transition in state_edges:
                raw_conditions = transition.get("conditions", [])
                conditions = (
                    cast(list[Any], raw_conditions)
                    if isinstance(raw_conditions, list)
                    else []
                )
                for condition in conditions:
                    if _condition_kind(condition) == "outcome_is":
                        value = cast(dict[str, Any], condition).get("value")
                        if isinstance(value, str) and not _is_placeholder(value):
                            if value not in outcomes:
                                error(
                                    f"transitions[{transition_index}].conditions",
                                    f"outcome {value!r} is not declared by "
                                    f"{state_id!r}",
                                )
                            matched.add(value)
            for outcome in outcomes - matched:
                error(
                    f"states[{state_id}].outcomes",
                    f"declared outcome {outcome!r} has no matching edge",
                )
        elif action == "escalate":
            if len(state_edges) != 1:
                error(f"states[{state_id}]", "escalate must have exactly one edge")
            elif state_edges[0][1].get("conditions") != [{"kind": "always"}]:
                error(
                    f"transitions[{state_edges[0][0]}].conditions",
                    "escalate must use exactly one always condition",
                )

    used_parameters = set(parameter_uses)
    for name in parameters:
        if name not in used_parameters:
            warning(f"parameters[{name}]", "parameter is declared but unused")

    return WorkflowValidation(definition, tuple(errors), tuple(warnings))


def normalize_workflow(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    source = cast(Mapping[str, Any], value)
    definition = dict(source)
    parameters = source.get("parameters")
    if isinstance(parameters, list):
        definition["parameters"] = [
            dict(cast(Mapping[str, Any], parameter))
            if isinstance(parameter, Mapping)
            else parameter
            for parameter in cast(list[Any], parameters)
        ]
    states = source.get("states")
    if isinstance(states, list):
        normalized_states: list[Any] = []
        for raw_state in cast(list[Any], states):
            if not isinstance(raw_state, Mapping):
                normalized_states.append(raw_state)
                continue
            state = dict(cast(Mapping[str, Any], raw_state))
            if isinstance(state.get("layout"), Mapping):
                state["layout"] = dict(cast(Mapping[str, Any], state["layout"]))
            normalized_states.append(state)
        definition["states"] = normalized_states
    transitions = source.get("transitions")
    if isinstance(transitions, list):
        normalized_transitions: list[Any] = []
        for raw_transition in cast(list[Any], transitions):
            if not isinstance(raw_transition, Mapping):
                normalized_transitions.append(raw_transition)
                continue
            transition = dict(cast(Mapping[str, Any], raw_transition))
            raw_conditions = transition.get("conditions")
            if isinstance(raw_conditions, list):
                conditions: list[Any] = []
                for raw_condition in cast(list[Any], raw_conditions):
                    if not isinstance(raw_condition, Mapping):
                        conditions.append(raw_condition)
                        continue
                    condition = dict(cast(Mapping[str, Any], raw_condition))
                    if (
                        condition.get("kind") == "always"
                        and condition.get("value") is None
                    ):
                        condition.pop("value", None)
                    conditions.append(condition)
                transition["conditions"] = conditions
            normalized_transitions.append(transition)
        definition["transitions"] = normalized_transitions
    return definition


def assign_layered_layout(definition: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_workflow(definition)
    states = cast(list[dict[str, Any]], normalized["states"])
    state_ids = [str(state["id"]) for state in states]
    edges: defaultdict[str, list[str]] = defaultdict(list)
    transitions = cast(list[dict[str, Any]], normalized["transitions"])
    for transition in transitions:
        edges[str(transition["from"])].append(str(transition["to"]))
    initial = str(normalized["initial"])
    levels: dict[str, int] = {initial: 0} if initial in state_ids else {}
    queue: deque[str] = deque([initial]) if initial in levels else deque()
    while queue:
        source = queue.popleft()
        for target in edges[source]:
            if target not in levels:
                levels[target] = levels[source] + 1
                queue.append(target)
    columns: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for state in states:
        if "layout" not in state:
            columns[levels.get(str(state["id"]), 0)].append(state)
    for level, level_states in columns.items():
        for index, state in enumerate(level_states):
            state["layout"] = {"x": level * 280, "y": index * 150}
    return normalized


class WorkflowStore:
    def __init__(
        self,
        database: SnapshotDatabase,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_workflows(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT id, name, created_at FROM workflows ORDER BY id"
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

        return self.database.transaction(read)

    def get_workflow(self, workflow_id: int) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            workflow = connection.execute(
                "SELECT id, name, created_at FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if workflow is None:
                return None
            revisions = connection.execute(
                """
                SELECT revision, source_prompt, definition_json, created_at
                FROM workflow_revisions
                WHERE workflow_id = ?
                ORDER BY revision
                """,
                (workflow_id,),
            ).fetchall()
            revision_values = [self._revision_dict(row) for row in revisions]
            return {
                "id": int(workflow["id"]),
                "name": str(workflow["name"]),
                "created_at": str(workflow["created_at"]),
                "latest": revision_values[-1] if revision_values else None,
                "revisions": revision_values,
            }

        return self.database.transaction(read)

    def create_workflow(
        self,
        name: str,
        source_prompt: str,
        definition: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock().astimezone(UTC).isoformat()
        payload = json.dumps(dict(definition), separators=(",", ":"))

        def create(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "INSERT INTO workflows(name, created_at) VALUES (?, ?)",
                (name, now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("workflow insert did not return an id")
            workflow_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO workflow_revisions(
                    workflow_id, revision, source_prompt, definition_json, created_at
                ) VALUES (?, 1, ?, ?, ?)
                """,
                (workflow_id, source_prompt, payload, now),
            )
            return workflow_id

        workflow_id = self.database.transaction(create)
        result = self.get_workflow(workflow_id)
        if result is None:
            raise RuntimeError("created workflow could not be read back")
        return result

    def add_revision(
        self,
        workflow_id: int,
        source_prompt: str,
        definition: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        now = self._clock().astimezone(UTC).isoformat()
        payload = json.dumps(dict(definition), separators=(",", ":"))

        def add(connection: sqlite3.Connection) -> bool:
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if exists is None:
                return False
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM workflow_revisions WHERE workflow_id = ?
                """,
                (workflow_id,),
            ).fetchone()
            next_revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO workflow_revisions(
                    workflow_id, revision, source_prompt, definition_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workflow_id, next_revision, source_prompt, payload, now),
            )
            return True

        if not self.database.transaction(add):
            return None
        return self.get_workflow(workflow_id)

    def delete_workflow(self, workflow_id: int) -> str:
        def delete(connection: sqlite3.Connection) -> str:
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if exists is None:
                return "missing"
            assigned = connection.execute(
                "SELECT 1 FROM workflow_assignments WHERE workflow_id = ? LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if assigned is not None:
                return "assigned"
            connection.execute(
                "DELETE FROM workflow_revisions WHERE workflow_id = ?", (workflow_id,)
            )
            connection.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
            return "deleted"

        return self.database.transaction(delete)

    @staticmethod
    def _revision_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            definition = json.loads(str(row["definition_json"]))
        except json.JSONDecodeError as error:
            raise RuntimeError("Persisted workflow definition is invalid") from error
        return {
            "revision": int(row["revision"]),
            "source_prompt": str(row["source_prompt"]),
            "definition": definition,
            "created_at": str(row["created_at"]),
        }


def _record_placeholders(
    state: Mapping[str, Any],
    location: str,
    uses: defaultdict[str, list[str]],
    error: Callable[[str, str], None],
    parameters: Mapping[str, Mapping[str, Any]],
    *,
    expected_type: str,
) -> None:
    prompt = state.get("prompt")
    if isinstance(prompt, str):
        _record_text_placeholders(
            prompt,
            f"{location}.prompt",
            uses,
            error,
            parameters,
            expected_type=expected_type,
        )
    skill = state.get("skill")
    if isinstance(skill, str):
        _record_text_placeholders(skill, f"{location}.skill", uses, error, parameters)


def _record_text_placeholders(
    text: str,
    location: str,
    uses: defaultdict[str, list[str]],
    error: Callable[[str, str], None],
    parameters: Mapping[str, Mapping[str, Any]],
    *,
    expected_type: str | None = None,
) -> None:
    for match in _PLACEHOLDER.finditer(text):
        name = match.group(1)
        uses[name].append(location)
        if name not in parameters:
            error(location, f"uses undeclared parameter {name!r}")
        elif expected_type is not None and _is_placeholder(match.group(0)):
            _check_placeholder_type(
                match.group(0), expected_type, location, parameters, error
            )


def _check_placeholder_type(
    value: str,
    expected: str,
    location: str,
    parameters: Mapping[str, Mapping[str, Any]],
    error: Callable[[str, str], None],
) -> None:
    match = _PLACEHOLDER.fullmatch(value)
    if match is None:
        return
    parameter = parameters.get(match.group(1))
    if parameter is not None and parameter.get("type") != expected:
        error(
            location,
            f"parameter {match.group(1)!r} must have type {expected!r}",
        )


def _condition_parameter_type(kind: str) -> str:
    return {
        "item_status_is": "status",
        "item_type_is": "item_type",
        "item_has_label": "label",
        "item_lacks_label": "label",
        "item_repository_is": "repository",
    }[kind]


def _validate_condition_value(
    kind: str,
    value: Any,
    location: str,
    parameters: Mapping[str, Mapping[str, Any]],
    statuses: set[str],
    error: Callable[[str, str], None],
    warning: Callable[[str, str], None],
) -> None:
    value_location = f"{location}.value"
    if not isinstance(value, str) or not value:
        error(value_location, "must be a non-empty string")
        return
    if _is_placeholder(value):
        expected = "text" if kind == "outcome_is" else _condition_parameter_type(kind)
        _check_placeholder_type(value, expected, value_location, parameters, error)
        return
    if kind == "item_type_is" and value not in {"issue", "pull_request"}:
        error(value_location, "must be issue or pull_request")
    elif kind == "item_status_is":
        _validate_status(value, statuses, value_location, error, warning)


def _validate_status(
    value: str,
    statuses: set[str],
    location: str,
    error: Callable[[str, str], None],
    warning: Callable[[str, str], None],
) -> None:
    if not statuses:
        warning(
            location, "Project Status options were unavailable; value was not checked"
        )
    elif value not in statuses:
        error(location, f"status {value!r} is not a Project Status option")


def _validate_parameter_value(
    value: Any,
    parameter_type: str,
    location: str,
    error: Callable[[str, str], None],
) -> None:
    valid = {
        "status": isinstance(value, str),
        "label": isinstance(value, str),
        "skill": isinstance(value, str),
        "repository": isinstance(value, str),
        "item_type": value in {"issue", "pull_request"},
        "text": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }[parameter_type]
    if not valid:
        error(location, f"does not match parameter type {parameter_type!r}")


def _skill_exists(name: str, skills_dirs: Sequence[Path]) -> bool:
    if not name or Path(name).name != name or name in {".", ".."}:
        return False
    for directory in skills_dirs:
        try:
            root = directory.expanduser().resolve()
            candidate = (root / name / "SKILL.md").resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return True
    return False


def _is_placeholder(value: str) -> bool:
    return _PLACEHOLDER.fullmatch(value) is not None


def _reachable(start: str, graph: Mapping[str, Iterable[str]]) -> set[str]:
    seen: set[str] = set()
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(graph.get(current, ()))
    return seen


def _condition_kind(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(cast(dict[str, Any], value).get("kind", ""))


def _string_list(value: Any) -> list[str]:
    values = cast(list[Any], value) if isinstance(value, list) else []
    return [str(item) for item in values]
