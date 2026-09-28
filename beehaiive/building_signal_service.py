from __future__ import annotations

import secrets
from collections.abc import Collection, Mapping
from typing import cast

from beehaiive.behavior_model import (
    BehaviorModelError,
    StructuredBehaviorModelClient,
)
from beehaiive.persistence import OrchestratorStore, StoreError
from beehaiive.persistence.building_signals import BuildingSignalAssignmentConflict

from .building_signal import (
    BUILDING_SIGNAL_RULE_SCHEMA,
    MAX_BUILDING_SIGNAL_QUANTITY,
    MAX_BUILDING_SIGNAL_RULES_PER_PROJECT,
    BuildingSignalRecord,
    BuildingSignalServiceError,
    BuildingSignalValidationError,
    BuildingSignalWorld,
    TargetBuildingSignalWorld,
    compare_inventory,
    normalize_rule,
    now,
    redact_text,
    validate_prompt,
    validate_targets,
)
from .service_failures import FailureCategory, TargetProviderError, WorldActionError


class BuildingSignalService:
    def __init__(
        self,
        store: OrchestratorStore,
        model_client: StructuredBehaviorModelClient | None = None,
        world: BuildingSignalWorld | None = None,
    ) -> None:
        self.store = store
        self.model_client = model_client
        self.world = world or TargetBuildingSignalWorld()

    def generate(self, project_id: str, prompt: object) -> dict[str, object]:
        normalized_project = _project_id(project_id)
        prompt_text = _prompt(prompt)
        raw_rule = self._generate_rule(prompt_text)
        rule = self._normalize_and_bind(prompt_text, raw_rule, normalized_project)
        return {"prompt": prompt_text, "rule": rule, "status": "draft"}

    def create_from_prompt(
        self, project_id: str, prompt: object
    ) -> BuildingSignalRecord:
        generated = self.generate(project_id, prompt)
        return self.create(project_id, generated["rule"])

    def create(self, project_id: str, rule: object) -> BuildingSignalRecord:
        normalized_project = _project_id(project_id)
        normalized_rule = self._normalize_and_bind("", rule, normalized_project)
        self._ensure_capacity(normalized_project)
        try:
            return self.store.create_building_signal_rule(
                secrets.token_hex(12), normalized_project, normalized_rule
            )
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def update(
        self, project_id: str, rule_id: str, rule: object
    ) -> BuildingSignalRecord:
        normalized_project = _project_id(project_id)
        record = self.get(normalized_project, rule_id)
        if record is None:
            raise BuildingSignalServiceError(
                "not_found", "Building signal rule not found"
            )
        if record.status != "draft":
            raise BuildingSignalServiceError(
                "invalid_state", "Only draft building signal rules can be edited"
            )
        normalized_rule = self._normalize_and_bind("", rule, normalized_project)
        try:
            return self.store.update_building_signal_rule(
                normalized_project,
                record.rule_id,
                rule=normalized_rule,
                expected_status="draft",
            )
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def confirm(self, project_id: str, rule_id: str) -> BuildingSignalRecord:
        normalized_project = _project_id(project_id)
        record = self._required(normalized_project, rule_id)
        if record.status != "draft":
            raise BuildingSignalServiceError(
                "invalid_state", "Only draft building signal rules can be confirmed"
            )
        try:
            return self.store.update_building_signal_rule(
                normalized_project,
                record.rule_id,
                status="confirmed",
                expected_status="draft",
            )
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def assign(
        self, project_id: str, rule_id: str, building_id: object
    ) -> BuildingSignalRecord:
        normalized_project = _project_id(project_id)
        record = self._required(normalized_project, rule_id)
        if record.status != "confirmed":
            raise BuildingSignalServiceError(
                "invalid_state", "Only confirmed building signal rules can be assigned"
            )
        normalized_building = self._building_target(normalized_project, building_id)
        for existing in self.list(normalized_project):
            if existing.status != "assigned" or existing.rule_id == record.rule_id:
                continue
            if (
                existing.assignment
                and existing.assignment.get("building_id") == normalized_building
            ):
                raise BuildingSignalServiceError(
                    "conflict",
                    "A building signal rule is already assigned to that building",
                )
        try:
            return self.store.update_building_signal_rule(
                normalized_project,
                record.rule_id,
                status="assigned",
                assignment={"building_id": normalized_building},
                expected_status="confirmed",
            )
        except BuildingSignalAssignmentConflict as exc:
            raise BuildingSignalServiceError("conflict", str(exc)) from exc
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def get(self, project_id: str, rule_id: str) -> BuildingSignalRecord | None:
        normalized_project = _project_id(project_id)
        normalized_rule_id = _rule_id(rule_id)
        try:
            return self.store.building_signal_rule_for(
                normalized_project, normalized_rule_id
            )
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def list(self, project_id: str) -> tuple[BuildingSignalRecord, ...]:
        normalized_project = _project_id(project_id)
        try:
            return self.store.building_signal_rules_for(normalized_project)
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def evaluate(self, project_id: str, rule_id: str) -> BuildingSignalRecord:
        normalized_project = _project_id(project_id)
        record = self._required(normalized_project, rule_id)
        if record.status != "assigned" or not record.assignment:
            raise BuildingSignalServiceError(
                "invalid_state", "Only assigned building signal rules can be evaluated"
            )
        building_id = record.assignment.get("building_id")
        if not isinstance(building_id, str):
            raise BuildingSignalServiceError(
                "invalid_state", "The building signal assignment is invalid"
            )
        try:
            normalized_rule = self._normalize_and_bind(
                "", record.rule, normalized_project
            )
            self._building_target(normalized_project, building_id)
            item_id = cast(str, normalized_rule["item"])
            signal_id = cast(str, normalized_rule["signal"])
            quantity = cast(int, normalized_rule["quantity"])
            comparison = cast(str, normalized_rule["comparison"])
            inventory_count = self.world.inventory_count(
                normalized_project, building_id, item_id
            )
            if (
                type(inventory_count) is not int
                or inventory_count < 0
                or inventory_count > MAX_BUILDING_SIGNAL_QUANTITY
            ):
                raise ValueError("Building inventory count is invalid")
            condition_result = compare_inventory(comparison, inventory_count, quantity)
            self.world.set_signal(
                normalized_project, building_id, signal_id, condition_result
            )
            state = {
                **record.signal_state,
                "active": condition_result,
                "signal": signal_id,
                "condition_result": condition_result,
                "inventory_count": inventory_count,
                "last_evaluated_at": now(),
                "last_error": None,
            }
        except (
            BuildingSignalServiceError,
            BuildingSignalValidationError,
            TargetProviderError,
            WorldActionError,
            RuntimeError,
            ValueError,
        ) as exc:
            state = {
                **record.signal_state,
                "last_evaluated_at": now(),
                "last_error": redact_text(f"{type(exc).__name__}: {exc}")[:512],
                "failure_class": _evaluation_failure_category(exc),
            }
        try:
            return self.store.update_building_signal_rule(
                normalized_project,
                record.rule_id,
                signal_state=state,
                expected_status="assigned",
            )
        except StoreError as exc:
            raise BuildingSignalServiceError(
                "persistence", str(exc), FailureCategory.PERSISTENCE.value
            ) from exc

    def poll(self, project_ids: Collection[str]) -> tuple[str, ...]:
        evaluated: list[str] = []
        seen: set[str] = set()
        for project_id in project_ids:
            normalized_project = _project_id(project_id)
            if normalized_project in seen:
                continue
            seen.add(normalized_project)
            for record in self.list(normalized_project):
                if record.status == "assigned":
                    self.evaluate(normalized_project, record.rule_id)
                    evaluated.append(record.rule_id)
        return tuple(evaluated)

    def _generate_rule(self, prompt: str) -> object:
        if self.model_client is None:
            raise BuildingSignalServiceError(
                "model_unavailable",
                "A local behavior model is not configured",
                FailureCategory.MODEL.value,
            )
        try:
            return self.model_client.generate_structured(
                prompt, BUILDING_SIGNAL_RULE_SCHEMA
            )
        except BehaviorModelError as exc:
            messages = {
                "model_unavailable": "The local behavior model is unavailable",
                "response_too_large": "The behavior model response is too large",
                "invalid_response": "The behavior model returned malformed JSON",
                "invalid_prompt": "A non-empty building signal prompt is required",
            }
            raise BuildingSignalServiceError(
                exc.code,
                messages.get(exc.code, "The behavior model request failed"),
                FailureCategory.MODEL.value,
            ) from exc

    def _normalize_and_bind(
        self,
        prompt: str,
        value: object,
        project_id: str | None = None,
    ) -> dict[str, object]:
        try:
            normalized = normalize_rule(value)
            if project_id is not None:
                validate_targets(normalized, self._allowed_targets(project_id))
            return normalized
        except BuildingSignalValidationError as exc:
            code = "invalid_target" if "unknown" in str(exc).lower() else "invalid_rule"
            raise BuildingSignalServiceError(code, str(exc)) from exc

    def _allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        try:
            targets = cast(object, self.world.allowed_targets(project_id))
        except (TargetProviderError, RuntimeError, ValueError) as exc:
            raise BuildingSignalServiceError(
                "target_provider",
                "Building signal targets are unavailable",
                FailureCategory.TARGET_PROVIDER.value,
            ) from exc
        if not isinstance(targets, Mapping):
            raise BuildingSignalServiceError(
                "invalid_target", "Building signal targets are unavailable"
            )
        targets_map = cast(Mapping[str, object], targets)
        normalized: dict[str, tuple[str, ...]] = {}
        for kind in ("building", "item", "signal"):
            values = targets_map.get(kind, ())
            if isinstance(values, (str, bytes)):
                raise BuildingSignalServiceError(
                    "invalid_target", "Building signal targets are unavailable"
                )
            try:
                normalized_values = tuple(cast(Collection[object], values))
            except TypeError as exc:
                raise BuildingSignalServiceError(
                    "invalid_target", "Building signal targets are unavailable"
                ) from exc
            checked_values: list[str] = []
            for value in normalized_values:
                if not isinstance(value, str) or not value.strip() or len(value) > 128:
                    raise BuildingSignalServiceError(
                        "invalid_target", "Building signal targets are unavailable"
                    )
                checked_values.append(value)
            normalized[kind] = tuple(checked_values)
        return normalized

    def _building_target(self, project_id: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BuildingSignalServiceError(
                "invalid_target", "A building target is required"
            )
        normalized = value.strip()
        allowed = self._allowed_targets(project_id).get("building", ())
        if normalized not in allowed:
            raise BuildingSignalServiceError(
                "invalid_target", "The building target is not available"
            )
        return normalized

    def _ensure_capacity(self, project_id: str) -> None:
        if len(self.list(project_id)) >= MAX_BUILDING_SIGNAL_RULES_PER_PROJECT:
            raise BuildingSignalServiceError(
                "limit", "The project has reached its building signal rule limit"
            )

    def _required(self, project_id: str, rule_id: str) -> BuildingSignalRecord:
        record = self.get(project_id, rule_id)
        if record is None:
            raise BuildingSignalServiceError(
                "not_found", "Building signal rule not found"
            )
        return record


def _prompt(value: object) -> str:
    try:
        return validate_prompt(value)
    except BuildingSignalValidationError as exc:
        raise BuildingSignalServiceError("invalid_prompt", str(exc)) from exc


def _project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise BuildingSignalServiceError(
            "invalid_project", "A valid project id is required"
        )
    return value.strip()


def _rule_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise BuildingSignalServiceError("invalid_rule", "A valid rule id is required")
    return value.strip()


def _evaluation_failure_category(error: BaseException) -> str:
    if isinstance(error, BuildingSignalServiceError):
        return error.category
    if isinstance(error, BuildingSignalValidationError):
        return FailureCategory.VALIDATION.value
    if isinstance(error, (TargetProviderError,)):
        return FailureCategory.TARGET_PROVIDER.value
    return FailureCategory.WORLD.value


__all__ = ["BuildingSignalService"]
