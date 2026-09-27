from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal, cast


class BehaviorValidationError(ValueError):
    """Raised when a generated or operator-supplied behavior is unsafe."""


BehaviorStatus = Literal[
    "draft", "confirmed", "assigned", "running", "completed", "failed"
]

MAX_BEHAVIOR_NAME_LENGTH = 128
MAX_BEHAVIOR_PARAMETER_COUNT = 32
MAX_BEHAVIOR_STATES = 16
MAX_BEHAVIOR_TRANSITIONS = 32
MAX_BEHAVIOR_ACTIONS_PER_STATE = 8
MAX_BEHAVIOR_STEPS = 64
MAX_BEHAVIOR_WAIT_SECONDS = 30.0
MAX_BEHAVIOR_BINDING_ID_LENGTH = 128
MAX_BEHAVIOR_UNIT_ID_LENGTH = 128
MAX_BEHAVIOR_PROMPT_LENGTH = 4_000

ALLOWED_PARAMETER_KINDS = frozenset(
    {"storage", "factory", "item", "signal", "color", "wait"}
)
ALLOWED_ACTIONS = frozenset(
    {"grab", "wait", "inspect_signal", "inspect_color", "bring", "deposit"}
)
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_ACTION_ARGUMENT_KINDS: dict[str, dict[str, str]] = {
    "grab": {"storage": "storage", "item": "item"},
    "wait": {"seconds": "wait"},
    "inspect_signal": {"target": "signal"},
    "inspect_color": {"target": "color"},
    "bring": {"target": "factory"},
    "deposit": {"target": "factory"},
}
BEHAVIOR_DEFINITION_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "name",
        "initial_state",
        "parameters",
        "states",
        "transitions",
        "max_steps",
    ],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "initial_state": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]{0,31}$",
        },
        "parameters": {
            "type": "object",
            "maxProperties": MAX_BEHAVIOR_PARAMETER_COUNT,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "enum": sorted(ALLOWED_PARAMETER_KINDS),
                    }
                },
            },
        },
        "states": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_BEHAVIOR_STATES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "actions"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]{0,31}$",
                    },
                    "actions": {
                        "type": "array",
                        "maxItems": MAX_BEHAVIOR_ACTIONS_PER_STATE,
                    },
                },
            },
        },
        "transitions": {
            "type": "array",
            "maxItems": MAX_BEHAVIOR_TRANSITIONS,
        },
        "max_steps": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_BEHAVIOR_STEPS,
        },
    },
}


@dataclass(frozen=True)
class BehaviorRecord:
    behavior_id: str
    project_id: str
    status: BehaviorStatus
    definition: dict[str, object]
    bindings: dict[str, object]
    assignment: dict[str, object] | None
    execution: dict[str, object]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "behavior_id": self.behavior_id,
            "project_id": self.project_id,
            "status": self.status,
            "definition": self.definition,
            "bindings": self.bindings,
            "assignment": self.assignment,
            "execution": self.execution,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def normalize_definition(value: object) -> dict[str, object]:
    mapping = _mapping(value, "Behavior definition must be an object")
    _reject_unknown_keys(
        mapping,
        {
            "schema_version",
            "name",
            "initial_state",
            "parameters",
            "states",
            "transitions",
            "max_steps",
        },
        "behavior definition",
    )
    schema_version = mapping.get("schema_version", 1)
    if type(schema_version) is not int or schema_version != 1:
        raise BehaviorValidationError("Unknown behavior schema version")
    name = _text(mapping.get("name"), "Behavior name", MAX_BEHAVIOR_NAME_LENGTH)
    initial_state = _identifier(mapping.get("initial_state"), "Initial state")
    parameters = _parameters(mapping.get("parameters", {}))
    states = _states(mapping.get("states"), parameters)
    state_ids = {str(state["id"]) for state in states}
    if initial_state not in state_ids:
        raise BehaviorValidationError("Initial state must refer to a declared state")
    transitions = _transitions(mapping.get("transitions", []), state_ids, parameters)
    max_steps = mapping.get("max_steps", MAX_BEHAVIOR_STEPS)
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_BEHAVIOR_STEPS:
        raise BehaviorValidationError(
            f"max_steps must be an integer between 1 and {MAX_BEHAVIOR_STEPS}"
        )
    return {
        "schema_version": 1,
        "name": name,
        "initial_state": initial_state,
        "parameters": parameters,
        "states": states,
        "transitions": transitions,
        "max_steps": max_steps,
    }


def normalize_bindings(
    definition: Mapping[str, object],
    value: object,
    *,
    require_all: bool,
    allowed_target_ids: Mapping[str, Collection[str]] | None = None,
) -> dict[str, object]:
    parameters = _mapping(definition.get("parameters"), "Parameters are invalid")
    bindings = _mapping(value, "Bindings must be an object")
    if len(bindings) > MAX_BEHAVIOR_PARAMETER_COUNT:
        raise BehaviorValidationError("Too many behavior bindings")
    normalized: dict[str, object] = {}
    for name, raw_binding in bindings.items():
        if name not in parameters:
            raise BehaviorValidationError(
                f"Binding references unknown parameter: {name}"
            )
        parameter = _mapping(parameters[name], f"Parameter {name} is invalid")
        expected_kind = parameter.get("kind")
        binding = _mapping(raw_binding, f"Binding {name} must be an object")
        kind = binding.get("kind")
        if kind != expected_kind:
            raise BehaviorValidationError(
                f"Binding {name} must use parameter kind {expected_kind}"
            )
        if kind == "wait":
            _reject_unknown_keys(binding, {"kind", "seconds"}, f"binding {name}")
            normalized[name] = {
                "kind": "wait",
                "seconds": _wait_seconds(binding.get("seconds"), f"Binding {name}"),
            }
            continue
        _reject_unknown_keys(binding, {"kind", "id"}, f"binding {name}")
        reference_id = binding.get("id")
        if not isinstance(reference_id, str) or not _REFERENCE_PATTERN.fullmatch(
            reference_id
        ):
            raise BehaviorValidationError(f"Binding {name} has an invalid target id")
        if allowed_target_ids is None:
            raise BehaviorValidationError("Target allowlists are unavailable")
        known_ids = allowed_target_ids.get(str(kind))
        if known_ids is None or reference_id not in known_ids:
            raise BehaviorValidationError(
                f"Binding {name} references an unknown {kind} target"
            )
        normalized[name] = {"kind": str(kind), "id": reference_id}
    if require_all:
        missing = sorted(set(parameters) - set(normalized))
        if missing:
            raise BehaviorValidationError(
                "All behavior parameters must be bound before confirmation: "
                + ", ".join(missing)
            )
    return normalized


def validate_prompt(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BehaviorValidationError("A non-empty behavior prompt is required")
    prompt = value.strip()
    if len(prompt) > MAX_BEHAVIOR_PROMPT_LENGTH:
        raise BehaviorValidationError(
            f"Behavior prompts are limited to {MAX_BEHAVIOR_PROMPT_LENGTH} characters"
        )
    return prompt


def validate_unit_id(value: object) -> str:
    if not isinstance(value, str) or not _REFERENCE_PATTERN.fullmatch(value):
        raise BehaviorValidationError("A valid automated unit id is required")
    return value


def initial_execution(definition: Mapping[str, object]) -> dict[str, object]:
    return {
        "state": definition["initial_state"],
        "step": 0,
        "history": [],
        "last_observation": None,
        "status": "pending",
        "error": None,
    }


def _parameters(value: object) -> dict[str, object]:
    parameters = _mapping(value, "Parameters must be an object")
    if len(parameters) > MAX_BEHAVIOR_PARAMETER_COUNT:
        raise BehaviorValidationError("Too many behavior parameters")
    normalized: dict[str, object] = {}
    for name, raw_parameter in parameters.items():
        if not _IDENTIFIER_PATTERN.fullmatch(name):
            raise BehaviorValidationError(f"Invalid behavior parameter name: {name}")
        parameter = _mapping(raw_parameter, f"Parameter {name} must be an object")
        _reject_unknown_keys(parameter, {"kind"}, f"parameter {name}")
        kind = parameter.get("kind")
        if kind not in ALLOWED_PARAMETER_KINDS:
            raise BehaviorValidationError(
                f"Unsupported behavior parameter kind: {kind}"
            )
        normalized[name] = {"kind": str(kind)}
    return normalized


def _states(value: object, parameters: Mapping[str, object]) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise BehaviorValidationError("At least one behavior state is required")
    state_values = cast(list[object], value)
    if len(state_values) > MAX_BEHAVIOR_STATES:
        raise BehaviorValidationError("Too many behavior states")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_state in state_values:
        state = _mapping(raw_state, "Behavior states must be objects")
        _reject_unknown_keys(state, {"id", "actions"}, "behavior state")
        state_id = _identifier(state.get("id"), "State id")
        if state_id in seen:
            raise BehaviorValidationError(f"Duplicate behavior state: {state_id}")
        seen.add(state_id)
        actions_value = state.get("actions")
        if not isinstance(actions_value, list):
            raise BehaviorValidationError(
                f"State {state_id} must contain at most "
                f"{MAX_BEHAVIOR_ACTIONS_PER_STATE} actions"
            )
        actions = cast(list[object], actions_value)
        if len(actions) > MAX_BEHAVIOR_ACTIONS_PER_STATE:
            raise BehaviorValidationError(
                f"State {state_id} must contain at most "
                f"{MAX_BEHAVIOR_ACTIONS_PER_STATE} actions"
            )
        normalized.append(
            {
                "id": state_id,
                "actions": _actions(actions, parameters, state_id),
            }
        )
    return normalized


def _actions(
    actions: list[object], parameters: Mapping[str, object], state_id: str
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for raw_action in actions:
        action = _mapping(raw_action, f"Actions in {state_id} must be objects")
        _reject_unknown_keys(action, {"type", "arguments"}, f"action in {state_id}")
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise BehaviorValidationError(f"Unsupported behavior action: {action_type}")
        arguments = _mapping(
            action.get("arguments", {}), "Action arguments must be an object"
        )
        expected_arguments = _ACTION_ARGUMENT_KINDS[str(action_type)]
        allowed_argument_names = (
            {"seconds"} if action_type == "wait" else set(expected_arguments)
        )
        if set(arguments) != allowed_argument_names:
            raise BehaviorValidationError(
                f"Action {action_type} in {state_id} has invalid arguments"
            )
        normalized_arguments: dict[str, object] = {}
        if action_type == "wait":
            seconds = arguments.get("seconds")
            if isinstance(seconds, str):
                parameter_value = parameters.get(seconds)
                parameter = (
                    cast(Mapping[str, object], parameter_value)
                    if isinstance(parameter_value, Mapping)
                    else None
                )
                if parameter is None or parameter.get("kind") != "wait":
                    raise BehaviorValidationError(
                        f"Wait duration in {state_id} must reference a wait parameter"
                    )
                normalized_arguments["seconds"] = seconds
            else:
                normalized_arguments["seconds"] = _wait_seconds(
                    seconds, f"Wait duration in {state_id}"
                )
        else:
            for argument_name, expected_kind in expected_arguments.items():
                parameter_name = arguments.get(argument_name)
                if parameter_name not in parameters:
                    raise BehaviorValidationError(
                        f"Action {action_type} references an unknown parameter"
                    )
                parameter = _mapping(
                    parameters[cast(str, parameter_name)],
                    "Referenced behavior parameter is invalid",
                )
                if parameter.get("kind") != expected_kind:
                    raise BehaviorValidationError(
                        f"Action {action_type} requires a {expected_kind} parameter"
                    )
                normalized_arguments[argument_name] = parameter_name
        normalized.append({"type": str(action_type), "arguments": normalized_arguments})
    return normalized


def _transitions(
    value: object, state_ids: set[str], parameters: Mapping[str, object]
) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise BehaviorValidationError("Transitions must be a bounded list")
    transition_values = cast(list[object], value)
    if len(transition_values) > MAX_BEHAVIOR_TRANSITIONS:
        raise BehaviorValidationError("Transitions must be a bounded list")
    normalized: list[dict[str, object]] = []
    seen_conditions: set[tuple[object, ...]] = set()
    for raw_transition in transition_values:
        transition = _mapping(raw_transition, "Behavior transitions must be objects")
        _reject_unknown_keys(
            transition, {"from", "to", "condition"}, "behavior transition"
        )
        source = _identifier(transition.get("from"), "Transition source")
        target = _identifier(transition.get("to"), "Transition target")
        if source not in state_ids or target not in state_ids:
            raise BehaviorValidationError("Transitions must refer to declared states")
        condition = _mapping(
            transition.get("condition", {"kind": "always"}),
            "Transition condition must be an object",
        )
        kind = condition.get("kind")
        if kind == "always":
            if set(condition) != {"kind"}:
                raise BehaviorValidationError("Always conditions cannot have arguments")
            normalized_condition: dict[str, object] = {"kind": "always"}
        elif kind in {"signal", "color"}:
            _reject_unknown_keys(condition, {"kind", "target", "equals"}, "condition")
            target_parameter = condition.get("target")
            parameter = (
                _mapping(parameters[target_parameter], "Condition target is invalid")
                if isinstance(target_parameter, str) and target_parameter in parameters
                else None
            )
            if parameter is None or parameter.get("kind") != kind:
                raise BehaviorValidationError(
                    f"{kind} conditions require a {kind} parameter"
                )
            equals = condition.get("equals")
            if not _json_scalar(equals):
                raise BehaviorValidationError("Condition equals must be a scalar")
            normalized_condition = {
                "kind": str(kind),
                "target": target_parameter,
                "equals": equals,
            }
        else:
            raise BehaviorValidationError(f"Unsupported transition condition: {kind}")
        condition_key = (
            source,
            normalized_condition.get("kind"),
            normalized_condition.get("target"),
            normalized_condition.get("equals"),
        )
        if condition_key in seen_conditions:
            raise BehaviorValidationError(
                f"Duplicate transition condition from state: {source}"
            )
        seen_conditions.add(condition_key)
        normalized.append(
            {"from": source, "to": target, "condition": normalized_condition}
        )
    return normalized


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BehaviorValidationError(message)
    mapping = cast(Mapping[str, object], value)
    return {key: item for key, item in mapping.items()}


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise BehaviorValidationError(f"{label} must be 1-{limit} characters")
    return value.strip()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise BehaviorValidationError(f"{label} must be a lowercase identifier")
    return value


def _json_scalar(value: object) -> bool:
    return (
        value is None
        or isinstance(value, (str, bool, int))
        or (isinstance(value, float) and math.isfinite(value))
    )


def _wait_seconds(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= MAX_BEHAVIOR_WAIT_SECONDS
    ):
        raise BehaviorValidationError(
            f"{label} must be between 0 and {MAX_BEHAVIOR_WAIT_SECONDS} seconds"
        )
    return float(value)


def _reject_unknown_keys(
    mapping: Mapping[str, object], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise BehaviorValidationError(f"Unknown {label} fields: {', '.join(unknown)}")


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_PARAMETER_KINDS",
    "BEHAVIOR_DEFINITION_SCHEMA",
    "BehaviorRecord",
    "BehaviorStatus",
    "BehaviorValidationError",
    "MAX_BEHAVIOR_ACTIONS_PER_STATE",
    "MAX_BEHAVIOR_BINDING_ID_LENGTH",
    "MAX_BEHAVIOR_NAME_LENGTH",
    "MAX_BEHAVIOR_PARAMETER_COUNT",
    "MAX_BEHAVIOR_PROMPT_LENGTH",
    "MAX_BEHAVIOR_STATES",
    "MAX_BEHAVIOR_STEPS",
    "MAX_BEHAVIOR_TRANSITIONS",
    "MAX_BEHAVIOR_UNIT_ID_LENGTH",
    "MAX_BEHAVIOR_WAIT_SECONDS",
    "initial_execution",
    "normalize_bindings",
    "normalize_definition",
    "validate_prompt",
    "validate_unit_id",
]
