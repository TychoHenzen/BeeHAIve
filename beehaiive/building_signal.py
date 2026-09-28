from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from beehaiive.contract_types.validation import _redact_text


class BuildingSignalValidationError(ValueError):
    """Raised when a building signal rule or target is unsafe."""


class BuildingSignalServiceError(RuntimeError):
    def __init__(self, code: str, message: str, category: str = "validation") -> None:
        super().__init__(message)
        self.code = code
        self.category = category


BuildingSignalStatus = Literal["draft", "confirmed", "assigned"]
Comparison = Literal["lt", "lte", "eq", "gte", "gt"]

MAX_BUILDING_SIGNAL_PROMPT_LENGTH = 4_000
MAX_BUILDING_SIGNAL_ID_LENGTH = 128
MAX_BUILDING_SIGNAL_QUANTITY = 1_000_000
MAX_BUILDING_SIGNAL_RULES_PER_PROJECT = 128
COMPARISONS: tuple[Comparison, ...] = ("lt", "lte", "eq", "gte", "gt")
_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_REFERENCE_SCHEMA_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"

BUILDING_SIGNAL_RULE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "comparison", "quantity", "item", "signal"],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "comparison": {"enum": list(COMPARISONS)},
        "quantity": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_BUILDING_SIGNAL_QUANTITY,
        },
        "item": {"type": "string", "pattern": _REFERENCE_SCHEMA_PATTERN},
        "signal": {"type": "string", "pattern": _REFERENCE_SCHEMA_PATTERN},
    },
}


@dataclass(frozen=True)
class BuildingSignalRecord:
    rule_id: str
    project_id: str
    status: BuildingSignalStatus
    rule: dict[str, object]
    assignment: dict[str, object] | None
    signal_state: dict[str, object]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "project_id": self.project_id,
            "status": self.status,
            "rule": self.rule,
            "assignment": self.assignment,
            "signal_state": self.signal_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class BuildingSignalWorld(Protocol):
    def allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        """Return authoritative building, item, and signal ids."""

        ...

    def inventory_count(self, project_id: str, building_id: str, item_id: str) -> int:
        """Read one bounded inventory count."""

        ...

    def set_signal(
        self, project_id: str, building_id: str, signal_id: str, active: bool
    ) -> Mapping[str, object]:
        """Apply one bounded current-signal state."""

        ...


class TargetBuildingSignalWorld:
    def __init__(
        self,
        targets: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self._targets: dict[str, dict[str, object]] = {
            project_id: dict(project_targets)
            for project_id, project_targets in (targets or {}).items()
        }
        self.signal_states: dict[tuple[str, str, str], bool] = {}

    def allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        project_targets = self._targets.get(project_id, {})
        return {
            kind: tuple(sorted(cast(Mapping[str, object], values)))
            for kind, values in project_targets.items()
            if kind in {"building", "item", "signal"} and isinstance(values, Mapping)
        }

    def inventory_count(self, project_id: str, building_id: str, item_id: str) -> int:
        project_targets = self._targets.get(project_id, {})
        self._require_target(project_targets, "building", building_id)
        self._require_target(project_targets, "item", item_id)
        inventories = project_targets.get("inventory")
        if not isinstance(inventories, Mapping):
            raise ValueError("Building inventory is unavailable")
        inventories_map = cast(Mapping[str, object], inventories)
        building_inventory = inventories_map.get(building_id)
        if not isinstance(building_inventory, Mapping):
            raise ValueError("Building inventory is unavailable")
        count = cast(Mapping[str, object], building_inventory).get(item_id)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > MAX_BUILDING_SIGNAL_QUANTITY
        ):
            raise ValueError("Building inventory count is invalid")
        return count

    def set_signal(
        self, project_id: str, building_id: str, signal_id: str, active: bool
    ) -> Mapping[str, object]:
        project_targets = self._targets.get(project_id, {})
        self._require_target(project_targets, "building", building_id)
        self._require_target(project_targets, "signal", signal_id)
        self.signal_states[(project_id, building_id, signal_id)] = active
        return {
            "ok": True,
            "building_id": building_id,
            "signal": signal_id,
            "active": active,
        }

    @staticmethod
    def _require_target(
        project_targets: Mapping[str, object],
        kind: str,
        target_id: str,
    ) -> None:
        values = project_targets.get(kind)
        if not isinstance(values, Mapping) or target_id not in values:
            raise ValueError(f"Unknown {kind} target")


def normalize_rule(value: object) -> dict[str, object]:
    mapping = _mapping(value, "Building signal rule must be an object")
    _reject_unknown_keys(
        mapping,
        {"schema_version", "comparison", "quantity", "item", "signal"},
        "building signal rule",
    )
    if "schema_version" not in mapping:
        raise BuildingSignalValidationError(
            "Building signal schema version is required"
        )
    schema_version = mapping["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise BuildingSignalValidationError("Unknown building signal schema version")
    comparison = _comparison(mapping.get("comparison"))
    quantity = mapping.get("quantity")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or not 0 <= quantity <= MAX_BUILDING_SIGNAL_QUANTITY
    ):
        raise BuildingSignalValidationError(
            f"Quantity must be an integer between 0 and {MAX_BUILDING_SIGNAL_QUANTITY}"
        )
    item = _reference(mapping.get("item"), "Item")
    signal = _reference(mapping.get("signal"), "Signal")
    return {
        "schema_version": 1,
        "comparison": comparison,
        "quantity": quantity,
        "item": item,
        "signal": signal,
    }


def validate_prompt(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildingSignalValidationError(
            "A non-empty building signal prompt is required"
        )
    prompt = value.strip()
    if len(prompt) > MAX_BUILDING_SIGNAL_PROMPT_LENGTH:
        raise BuildingSignalValidationError(
            "Building signal prompts are limited to "
            f"{MAX_BUILDING_SIGNAL_PROMPT_LENGTH} characters"
        )
    return prompt


def validate_targets(
    rule: Mapping[str, object],
    allowed_target_ids: Mapping[str, Collection[str]],
) -> None:
    for kind in ("item", "signal"):
        target_id = rule.get(kind)
        if not isinstance(target_id, str) or target_id not in allowed_target_ids.get(
            kind, ()
        ):
            raise BuildingSignalValidationError(
                f"Rule references an unknown {kind} target"
            )


def compare_inventory(comparison: object, inventory_count: int, quantity: int) -> bool:
    normalized = _comparison(comparison)
    if normalized == "lt":
        return inventory_count < quantity
    if normalized == "lte":
        return inventory_count <= quantity
    if normalized == "eq":
        return inventory_count == quantity
    if normalized == "gte":
        return inventory_count >= quantity
    return inventory_count > quantity


def initial_signal_state() -> dict[str, object]:
    return {
        "active": False,
        "signal": None,
        "condition_result": None,
        "inventory_count": None,
        "last_evaluated_at": None,
        "last_error": None,
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


def redact_text(value: str) -> str:
    return _redact_text(value, 512)


def _comparison(value: object) -> Comparison:
    aliases = {
        "less_than": "lt",
        "less_than_or_equal": "lte",
        "equal": "eq",
        "greater_than_or_equal": "gte",
        "greater_than": "gt",
    }
    if not isinstance(value, str):
        raise BuildingSignalValidationError(
            f"Comparison must be one of: {', '.join(COMPARISONS)}"
        )
    normalized = aliases.get(value, value)
    if normalized not in COMPARISONS:
        raise BuildingSignalValidationError(
            f"Comparison must be one of: {', '.join(COMPARISONS)}"
        )
    return normalized


def _reference(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REFERENCE_PATTERN.fullmatch(value):
        raise BuildingSignalValidationError(
            f"{label} must be a bounded target identifier"
        )
    return value


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BuildingSignalValidationError(message)
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise BuildingSignalValidationError(message)
    return {cast(str, key): item for key, item in mapping.items()}


def _reject_unknown_keys(
    mapping: Mapping[str, object], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise BuildingSignalValidationError(
            f"Unknown {label} field: {', '.join(unknown)}"
        )


__all__ = [
    "BUILDING_SIGNAL_RULE_SCHEMA",
    "BuildingSignalRecord",
    "BuildingSignalServiceError",
    "BuildingSignalStatus",
    "BuildingSignalValidationError",
    "BuildingSignalWorld",
    "COMPARISONS",
    "TargetBuildingSignalWorld",
    "compare_inventory",
    "initial_signal_state",
    "normalize_rule",
    "now",
    "redact_text",
    "validate_prompt",
    "validate_targets",
]
