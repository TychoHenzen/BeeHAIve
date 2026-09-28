from __future__ import annotations

import re
import secrets
from collections.abc import Collection, Mapping
from typing import Protocol, cast

from beehaiive.persistence import OrchestratorStore, StateConflictError, StoreError

from .behavior import (
    MAX_BEHAVIOR_WAIT_SECONDS,
    BehaviorRecord,
    BehaviorValidationError,
    initial_execution,
    normalize_bindings,
    normalize_definition,
    validate_prompt,
    validate_unit_id,
)
from .behavior_model import BehaviorModelClient, BehaviorModelError
from .service_failures import (
    FailureCategory,
    TargetProviderError,
    WorldActionError,
)


class BehaviorServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        category: FailureCategory = FailureCategory.VALIDATION,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category


class UnitWorld(Protocol):
    def allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        """Return the authoritative target ids available to a project."""

        ...

    def execute(
        self,
        project_id: str,
        unit_id: str,
        action_type: str,
        arguments: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]:
        """Execute one allowlisted action and return a bounded observation."""

        ...


class RecordingUnitWorld:
    def __init__(self, target_ids: Mapping[str, Collection[str]] | None = None) -> None:
        defaults = {
            "storage": {"storage-a"},
            "factory": {"factory-a"},
            "item": {"item-1"},
            "signal": {"signal-a"},
            "color": {"color-a"},
        }
        source = target_ids or defaults
        self._target_ids = {kind: frozenset(values) for kind, values in source.items()}
        self._results: dict[str, dict[str, object]] = {}
        self.executed_actions: list[tuple[str, str]] = []

    def allowed_targets(self, _project_id: str) -> Mapping[str, Collection[str]]:
        return {kind: tuple(values) for kind, values in self._target_ids.items()}

    def execute(
        self,
        project_id: str,
        unit_id: str,
        action_type: str,
        arguments: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]:
        if idempotency_key is not None and idempotency_key in self._results:
            return dict(self._results[idempotency_key])
        result: dict[str, object] = {
            "ok": True,
            "project_id": project_id,
            "unit_id": unit_id,
            "action": action_type,
            "arguments": dict(arguments),
        }
        if action_type == "wait":
            seconds = arguments.get("seconds")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not 0 <= float(seconds) <= MAX_BEHAVIOR_WAIT_SECONDS
            ):
                raise WorldActionError("Wait duration is unavailable")
            result.update({"kind": "wait", "seconds": float(seconds)})
        elif action_type == "inspect_signal":
            target_id = _target_id(arguments, "target", "signal", self._target_ids)
            result.update({"kind": "signal", "target_id": target_id, "value": "ready"})
        elif action_type == "inspect_color":
            target_id = _target_id(arguments, "target", "color", self._target_ids)
            result.update({"kind": "color", "target_id": target_id, "value": "blue"})
        elif action_type == "grab":
            _target_id(arguments, "storage", "storage", self._target_ids)
            _target_id(arguments, "item", "item", self._target_ids)
        elif action_type in {"bring", "deposit"}:
            _target_id(arguments, "target", "factory", self._target_ids)
        self.executed_actions.append((unit_id, action_type))
        if idempotency_key is not None:
            self._results[idempotency_key] = dict(result)
        return result


class TargetUnitWorld:
    def __init__(
        self,
        targets: Mapping[str, Mapping[str, Mapping[str, Mapping[str, object]]]]
        | None = None,
    ) -> None:
        self._targets = {
            project_id: {
                kind: {
                    target_id: dict(properties)
                    for target_id, properties in values.items()
                }
                for kind, values in project_targets.items()
            }
            for project_id, project_targets in (targets or {}).items()
        }
        self._results: dict[str, dict[str, object]] = {}
        self._held_items: dict[tuple[str, str], str] = {}
        self._locations: dict[tuple[str, str], str] = {}

    def allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        project_targets = self._targets.get(project_id, {})
        return {
            kind: tuple(sorted(values))
            for kind, values in project_targets.items()
            if kind in {"storage", "factory", "item", "signal", "color"}
        }

    def execute(
        self,
        project_id: str,
        unit_id: str,
        action_type: str,
        arguments: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]:
        if idempotency_key is not None and idempotency_key in self._results:
            return dict(self._results[idempotency_key])
        result: dict[str, object]
        if action_type == "wait":
            seconds = arguments.get("seconds")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not 0 <= float(seconds) <= MAX_BEHAVIOR_WAIT_SECONDS
            ):
                raise WorldActionError("Wait duration is unavailable")
            result = {"ok": True, "kind": "wait", "seconds": float(seconds)}
        elif action_type == "grab":
            storage_id, _ = self._target(project_id, arguments, "storage", "storage")
            item_id, _ = self._target(project_id, arguments, "item", "item")
            held_by = self._held_items.get((project_id, item_id))
            if held_by is not None and held_by != unit_id:
                raise WorldActionError("Item is already held by another unit")
            location = self._locations.get((project_id, item_id))
            if location is not None and location != storage_id:
                raise WorldActionError("Item is not available at the requested storage")
            self._held_items[(project_id, item_id)] = unit_id
            self._locations[(project_id, item_id)] = storage_id
            result = {
                "ok": True,
                "kind": "item",
                "target_id": item_id,
                "value": "held",
            }
        elif action_type in {"inspect_signal", "inspect_color"}:
            kind = "signal" if action_type == "inspect_signal" else "color"
            target_id, target = self._target(project_id, arguments, kind, "target")
            value = target.get("value")
            if value is None or not isinstance(value, (str, bool, int, float)):
                raise WorldActionError(f"{kind} target has no bounded value")
            result = {
                "ok": True,
                "kind": kind,
                "target_id": target_id,
                "value": value,
            }
        elif action_type in {"bring", "deposit"}:
            factory_id, _ = self._target(project_id, arguments, "factory", "target")
            held_item = next(
                (
                    item_id
                    for (target_project, item_id), holder in self._held_items.items()
                    if target_project == project_id and holder == unit_id
                ),
                None,
            )
            if held_item is None:
                raise WorldActionError("Unit is not holding an item")
            self._locations[(project_id, held_item)] = factory_id
            if action_type == "deposit":
                del self._held_items[(project_id, held_item)]
            result = {
                "ok": True,
                "kind": "factory",
                "target_id": factory_id,
                "value": action_type,
            }
        else:
            raise WorldActionError("Action is not supported by the target world")
        if idempotency_key is not None:
            self._results[idempotency_key] = dict(result)
        return result

    def _target(
        self,
        project_id: str,
        arguments: Mapping[str, object],
        kind: str,
        argument_name: str,
    ) -> tuple[str, Mapping[str, object]]:
        target_id = _target_id(
            arguments,
            argument_name,
            kind,
            self.allowed_targets(project_id),
        )
        target = self._targets.get(project_id, {}).get(kind, {}).get(target_id)
        if target is None:
            raise WorldActionError(f"{kind} target is unavailable")
        return target_id, target


class BehaviorService:
    def __init__(
        self,
        store: OrchestratorStore,
        model_client: BehaviorModelClient | None = None,
        unit_world: UnitWorld | None = None,
    ) -> None:
        self.store = store
        self.model_client = model_client
        self.unit_world = unit_world or TargetUnitWorld()

    def generate(self, prompt: object) -> dict[str, object]:
        try:
            prompt_text = validate_prompt(prompt)
        except BehaviorValidationError as exc:
            raise BehaviorServiceError("invalid_prompt", str(exc)) from exc
        if self.model_client is None:
            raise BehaviorServiceError(
                "model_unavailable",
                "A local behavior model is not configured",
                FailureCategory.MODEL,
            )
        try:
            raw_definition = self.model_client.generate(prompt_text)
        except BehaviorModelError as exc:
            raise BehaviorServiceError(
                exc.code, str(exc), FailureCategory.MODEL
            ) from exc
        try:
            definition = normalize_definition(raw_definition)
        except BehaviorValidationError as exc:
            raise BehaviorServiceError("invalid_definition", str(exc)) from exc
        return {"prompt": prompt_text, "definition": definition, "status": "draft"}

    def create(
        self,
        project_id: str,
        definition: object,
        bindings: object = None,
    ) -> BehaviorRecord:
        normalized_definition = _normalize_definition(definition)
        normalized_bindings = _normalize_bindings(
            normalized_definition,
            {} if bindings is None else bindings,
            require_all=False,
            allowed_target_ids=self._allowed_targets(project_id),
        )
        behavior_id = secrets.token_hex(12)
        try:
            return self.store.create_unit_behavior(
                behavior_id,
                _project_id(project_id),
                normalized_definition,
                normalized_bindings,
            )
        except StoreError as exc:
            raise BehaviorServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE
            ) from exc

    def get(self, project_id: str, behavior_id: str) -> BehaviorRecord:
        try:
            record = self.store.unit_behavior_for(_project_id(project_id), behavior_id)
        except StoreError as exc:
            raise BehaviorServiceError(
                "persistence",
                "Behavior readback is unavailable",
                FailureCategory.PERSISTENCE,
            ) from exc
        if record is None:
            raise BehaviorServiceError("not_found", "Behavior not found")
        return record

    def list(self, project_id: str) -> tuple[BehaviorRecord, ...]:
        try:
            return self.store.unit_behaviors_for(_project_id(project_id))
        except StoreError as exc:
            raise BehaviorServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE
            ) from exc

    def bind(
        self, project_id: str, behavior_id: str, bindings: object
    ) -> BehaviorRecord:
        record = self.get(project_id, behavior_id)
        if record.status != "draft":
            raise BehaviorServiceError(
                "state_conflict", "Only draft behaviors can be bound"
            )
        normalized = _normalize_bindings(
            record.definition,
            bindings,
            require_all=False,
            allowed_target_ids=self._allowed_targets(record.project_id),
        )
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                bindings=normalized,
                expected_status="draft",
            )
        except StoreError as exc:
            raise _behavior_update_error(exc) from exc

    def confirm(self, project_id: str, behavior_id: str) -> BehaviorRecord:
        record = self.get(project_id, behavior_id)
        if record.status != "draft":
            raise BehaviorServiceError(
                "state_conflict", "Only draft behaviors can be confirmed"
            )
        _normalize_bindings(
            record.definition,
            record.bindings,
            require_all=True,
            allowed_target_ids=self._allowed_targets(record.project_id),
        )
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                status="confirmed",
                expected_status="draft",
            )
        except StoreError as exc:
            raise _behavior_update_error(exc) from exc

    def assign(
        self, project_id: str, behavior_id: str, unit_id: object
    ) -> BehaviorRecord:
        record = self.get(project_id, behavior_id)
        if record.status != "confirmed":
            raise BehaviorServiceError(
                "state_conflict", "Only confirmed behaviors can be assigned"
            )
        assignment = {"unit_id": validate_unit_id(unit_id)}
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                status="assigned",
                assignment=assignment,
                expected_status="confirmed",
            )
        except StoreError as exc:
            raise _behavior_update_error(exc) from exc

    def run(self, project_id: str, behavior_id: str) -> BehaviorRecord:
        record = self.get(project_id, behavior_id)
        if record.status == "completed":
            return record
        if record.status not in {"assigned", "running"}:
            raise BehaviorServiceError(
                "state_conflict", "Only assigned behaviors can execute"
            )
        if record.assignment is None:
            raise BehaviorServiceError(
                "invalid_assignment", "Behavior assignment is missing"
            )
        unit_id = record.assignment.get("unit_id")
        if not isinstance(unit_id, str):
            raise BehaviorServiceError(
                "invalid_assignment", "Behavior unit id is invalid"
            )
        if record.status == "assigned":
            record = self._update_running(record)
        execution = _execution(record.execution, record.definition)
        states = _state_map(record.definition)
        transitions = _transitions(record.definition)
        max_steps = record.definition.get("max_steps")
        if type(max_steps) is not int:
            raise BehaviorServiceError(
                "invalid_definition", "Behavior max_steps is invalid"
            )
        while True:
            step = cast(int, execution["step"])
            if step >= max_steps:
                return self._fail(record, execution, "Behavior exceeded its step limit")
            state_id = execution.get("state")
            if not isinstance(state_id, str):
                return self._fail(record, execution, "Behavior checkpoint has no state")
            state = states.get(state_id)
            if state is None:
                return self._fail(
                    record, execution, "Behavior checkpoint names an unknown state"
                )
            actions_value = state.get("actions", [])
            if not isinstance(actions_value, list):
                return self._fail(
                    record, execution, "Behavior state actions are invalid"
                )
            actions = cast(list[object], actions_value)
            action_index = execution.get("action_index", 0)
            if type(action_index) is not int or action_index < 0:
                return self._fail(
                    record, execution, "Behavior checkpoint has an invalid action index"
                )
            if action_index < len(actions):
                action = actions[action_index]
                try:
                    action_type, arguments = _resolved_action(action, record.bindings)
                    idempotency_key = _action_idempotency_key(
                        record.behavior_id, step, state_id, action_index
                    )
                    in_flight = execution.get("in_flight")
                    if in_flight is None:
                        execution = {
                            **execution,
                            "in_flight": {
                                "key": idempotency_key,
                                "step": step,
                                "state": state_id,
                                "action_index": action_index,
                                "action": action_type,
                            },
                        }
                        record = self._save_execution(record, execution)
                    elif not _matches_in_flight(
                        in_flight,
                        idempotency_key,
                        step,
                        state_id,
                        action_index,
                        action_type,
                    ):
                        return self._fail(
                            record, execution, "Behavior in-flight action is invalid"
                        )
                    observation = self.unit_world.execute(
                        record.project_id,
                        unit_id,
                        action_type,
                        arguments,
                        idempotency_key=idempotency_key,
                    )
                    bounded_observation = _bounded_observation(observation)
                except WorldActionError as exc:
                    return self._fail(
                        record, execution, str(exc), FailureCategory.WORLD
                    )
                history = list(cast(list[object], execution.get("history", [])))
                history.append(
                    {
                        "step": step,
                        "state": state_id,
                        "action": action_type,
                        "observation": bounded_observation,
                    }
                )
                next_execution = {
                    **execution,
                    "action_index": action_index + 1,
                    "history": history[-128:],
                    "last_observation": bounded_observation,
                }
                next_execution.pop("in_flight", None)
                execution = next_execution
                record = self._save_execution(record, execution)
                continue
            transition = _next_transition(
                transitions, state_id, execution, record.bindings
            )
            if transition is None:
                execution = {**execution, "status": "completed", "error": None}
                try:
                    return self.store.update_unit_behavior(
                        record.project_id,
                        record.behavior_id,
                        status="completed",
                        execution=execution,
                        expected_status="running",
                    )
                except StoreError as exc:
                    raise BehaviorServiceError(
                        "persistence", str(exc), FailureCategory.PERSISTENCE
                    ) from exc
            execution = {
                **execution,
                "state": transition["to"],
                "action_index": 0,
                "step": step + 1,
            }
            record = self._save_execution(record, execution)

    def _update_running(self, record: BehaviorRecord) -> BehaviorRecord:
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                status="running",
                execution=initial_execution(record.definition),
                expected_status="assigned",
            )
        except StoreError as exc:
            raise _behavior_update_error(exc) from exc

    def _save_execution(
        self, record: BehaviorRecord, execution: dict[str, object]
    ) -> BehaviorRecord:
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                execution=execution,
                expected_status="running",
            )
        except StoreError as exc:
            raise BehaviorServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE
            ) from exc

    def _fail(
        self,
        record: BehaviorRecord,
        execution: dict[str, object],
        message: str,
        category: FailureCategory = FailureCategory.VALIDATION,
    ) -> BehaviorRecord:
        failed_execution = {
            **execution,
            "status": "failed",
            "error": _redact_text(message)[:512],
            "failure_class": category.value,
        }
        try:
            return self.store.update_unit_behavior(
                record.project_id,
                record.behavior_id,
                status="failed",
                execution=failed_execution,
                expected_status="running",
            )
        except StoreError as exc:
            raise BehaviorServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE
            ) from exc

    def _allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        try:
            return self.unit_world.allowed_targets(_project_id(project_id))
        except TargetProviderError as exc:
            raise BehaviorServiceError(
                "target_provider",
                "Target allowlists are unavailable",
                FailureCategory.TARGET_PROVIDER,
            ) from exc


def _normalize_definition(value: object) -> dict[str, object]:
    try:
        return normalize_definition(value)
    except BehaviorValidationError as exc:
        raise BehaviorServiceError("invalid_definition", str(exc)) from exc


def _normalize_bindings(
    definition: Mapping[str, object],
    value: object,
    *,
    require_all: bool,
    allowed_target_ids: Mapping[str, Collection[str]],
) -> dict[str, object]:
    try:
        return normalize_bindings(
            definition,
            value,
            require_all=require_all,
            allowed_target_ids=allowed_target_ids,
        )
    except BehaviorValidationError as exc:
        raise BehaviorServiceError("invalid_bindings", str(exc)) from exc


def _project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise BehaviorServiceError("invalid_project", "A valid project id is required")
    return value.strip()


def _execution(
    execution: Mapping[str, object], definition: Mapping[str, object]
) -> dict[str, object]:
    if not execution:
        return initial_execution(definition)
    state = execution.get("state")
    step = execution.get("step")
    history = execution.get("history", [])
    if (
        not isinstance(state, str)
        or type(step) is not int
        or not isinstance(history, list)
    ):
        raise BehaviorServiceError(
            "invalid_checkpoint", "Stored behavior checkpoint is invalid"
        )
    return dict(execution)


def _state_map(definition: Mapping[str, object]) -> dict[str, dict[str, object]]:
    states = definition.get("states")
    if not isinstance(states, list):
        raise BehaviorServiceError("invalid_definition", "Behavior states are invalid")
    state_map: dict[str, dict[str, object]] = {}
    for raw_state in cast(list[object], states):
        if not isinstance(raw_state, Mapping):
            continue
        state = cast(Mapping[str, object], raw_state)
        state_id = state.get("id")
        if isinstance(state_id, str):
            state_map[state_id] = dict(state)
    return state_map


def _transitions(definition: Mapping[str, object]) -> list[dict[str, object]]:
    transitions = definition.get("transitions", [])
    if not isinstance(transitions, list):
        raise BehaviorServiceError(
            "invalid_definition", "Behavior transitions are invalid"
        )
    transition_values = cast(list[object], transitions)
    normalized: list[dict[str, object]] = []
    for raw_transition in transition_values:
        if isinstance(raw_transition, Mapping):
            normalized.append(dict(cast(Mapping[str, object], raw_transition)))
    return normalized


def _resolved_action(
    raw_action: object, bindings: Mapping[str, object]
) -> tuple[str, dict[str, object]]:
    if not isinstance(raw_action, Mapping):
        raise BehaviorServiceError("invalid_definition", "Behavior action is invalid")
    action_mapping = cast(Mapping[str, object], raw_action)
    action_type = action_mapping.get("type")
    arguments = action_mapping.get("arguments")
    if not isinstance(action_type, str) or not isinstance(arguments, Mapping):
        raise BehaviorServiceError("invalid_definition", "Behavior action is invalid")
    argument_mapping = cast(Mapping[str, object], arguments)
    resolved: dict[str, object] = {}
    for name, value in argument_mapping.items():
        if isinstance(value, str):
            binding = bindings.get(value)
            if not isinstance(binding, Mapping):
                raise BehaviorServiceError(
                    "invalid_bindings", f"Binding {value} is missing"
                )
            binding_mapping = cast(Mapping[str, object], binding)
            if name == "seconds":
                if binding_mapping.get("kind") != "wait":
                    raise BehaviorServiceError(
                        "invalid_bindings", f"Binding {value} is not a wait duration"
                    )
                resolved[str(name)] = binding_mapping.get("seconds")
            else:
                resolved[str(name)] = dict(binding_mapping)
        else:
            resolved[str(name)] = value
    return action_type, resolved


def _bounded_observation(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorldActionError("Unit returned an invalid observation")
    observation = cast(Mapping[object, object], value)
    bounded: dict[str, object] = {}
    for key, item in observation.items():
        if not isinstance(key, str):
            raise WorldActionError("Unit returned an invalid observation key")
        if len(key) > 64:
            continue
        if item is None or isinstance(item, (bool, int, float)):
            bounded[key] = item
        elif isinstance(item, str):
            bounded[key] = _redact_text(item)
    return bounded


def _behavior_update_error(error: StoreError) -> BehaviorServiceError:
    if isinstance(error, StateConflictError):
        return BehaviorServiceError("state_conflict", str(error))
    return BehaviorServiceError("persistence", str(error), FailureCategory.PERSISTENCE)


def _next_transition(
    transitions: list[dict[str, object]],
    state_id: str,
    execution: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object] | None:
    for transition in transitions:
        if transition.get("from") != state_id:
            continue
        condition_value = transition.get("condition")
        if not isinstance(condition_value, Mapping):
            continue
        condition = cast(Mapping[str, object], condition_value)
        kind = condition.get("kind")
        if kind == "always":
            return transition
        observation_value = execution.get("last_observation")
        if not isinstance(observation_value, Mapping):
            continue
        observation = cast(Mapping[str, object], observation_value)
        target_name = condition.get("target")
        target_binding = (
            bindings.get(target_name) if isinstance(target_name, str) else None
        )
        target_binding_mapping = (
            cast(Mapping[str, object], target_binding)
            if isinstance(target_binding, Mapping)
            else None
        )
        if (
            observation.get("kind") == kind
            and target_binding_mapping is not None
            and observation.get("target_id") == target_binding_mapping.get("id")
            and observation.get("value") == condition.get("equals")
        ):
            return transition
    return None


def _target_id(
    arguments: Mapping[str, object],
    argument_name: str,
    expected_kind: str,
    allowed_target_ids: Mapping[str, Collection[str]],
) -> str:
    binding = arguments.get(argument_name)
    if not isinstance(binding, Mapping):
        raise WorldActionError(f"{expected_kind} target is unavailable")
    binding_mapping = cast(Mapping[str, object], binding)
    target_id = binding_mapping.get("id")
    if (
        binding_mapping.get("kind") != expected_kind
        or not isinstance(target_id, str)
        or target_id not in allowed_target_ids.get(expected_kind, ())
    ):
        raise WorldActionError(f"{expected_kind} target is unavailable")
    return target_id


def _action_idempotency_key(
    behavior_id: str, step: int, state_id: str, action_index: int
) -> str:
    return f"{behavior_id}:{step}:{state_id}:{action_index}"


def _matches_in_flight(
    value: object,
    key: str,
    step: int,
    state_id: str,
    action_index: int,
    action_type: str,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    in_flight = cast(Mapping[str, object], value)
    return (
        in_flight.get("key") == key
        and in_flight.get("step") == step
        and in_flight.get("state") == state_id
        and in_flight.get("action_index") == action_index
        and in_flight.get("action") == action_type
    )


_SENSITIVE_TEXT = re.compile(
    r"(?i)\b[\w-]*(?:secret|token|password|api[_-]?key|authorization)[\w-]*\s*=\s*[^\s,;]+"
)


def _redact_text(value: str) -> str:
    return _SENSITIVE_TEXT.sub("[redacted]", value)


__all__ = [
    "BehaviorModelClient",
    "BehaviorService",
    "BehaviorServiceError",
    "RecordingUnitWorld",
    "TargetUnitWorld",
    "UnitWorld",
]
