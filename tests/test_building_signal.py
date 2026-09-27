from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from beehaiive.building_signal import (
    BUILDING_SIGNAL_RULE_SCHEMA,
    TargetBuildingSignalWorld,
    normalize_rule,
)
from beehaiive.building_signal_service import BuildingSignalService
from beehaiive.persistence import OrchestratorStore, StoreError


def rule() -> dict[str, object]:
    return {
        "schema_version": 1,
        "comparison": "lte",
        "quantity": 5,
        "item": "iron-plate",
        "signal": "green",
    }


def targets() -> dict[str, dict[str, object]]:
    return {
        "owner:7": {
            "building": {"smelter": {}},
            "item": {"iron-plate": {}},
            "signal": {"green": {}},
            "inventory": {"smelter": {"iron-plate": 3}},
        }
    }


class FakeBuildingSignalModel:
    def __init__(self, response: object | None = None) -> None:
        self.response = response or rule()
        self.prompts: list[str] = []
        self.schemas: list[dict[str, object]] = []

    def generate_structured(self, prompt: str, schema: dict[str, object]) -> object:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return self.response


def make_service(
    tmp_path: Path,
    *,
    model: Any | None = None,
    world: TargetBuildingSignalWorld | None = None,
) -> tuple[OrchestratorStore, BuildingSignalService]:
    store = OrchestratorStore(tmp_path / "building-signal.sqlite3")
    return store, BuildingSignalService(
        store, model_client=model, world=world or TargetBuildingSignalWorld(targets())
    )


def test_building_signal_lifecycle_evaluates_and_polls(tmp_path: Path) -> None:
    model = FakeBuildingSignalModel()
    world = TargetBuildingSignalWorld(targets())
    store, service = make_service(tmp_path, model=model, world=world)

    draft = service.create_from_prompt("owner:7", "if iron plate is low")
    assert draft.status == "draft"
    assert model.prompts == ["if iron plate is low"]
    assert model.schemas == [BUILDING_SIGNAL_RULE_SCHEMA]
    edited = service.update("owner:7", draft.rule_id, {**rule(), "quantity": 4})
    confirmed = service.confirm("owner:7", edited.rule_id)
    assigned = service.assign("owner:7", confirmed.rule_id, "smelter")
    assert assigned.status == "assigned"

    evaluated = service.evaluate("owner:7", assigned.rule_id)
    assert evaluated.signal_state["active"] is True
    assert evaluated.signal_state["condition_result"] is True
    assert evaluated.signal_state["inventory_count"] == 3
    assert world.signal_states[("owner:7", "smelter", "green")] is True

    world._targets["owner:7"]["inventory"] = {"smelter": {"iron-plate": 8}}
    assert service.poll(["owner:7"]) == (assigned.rule_id,)
    restored = service.get("owner:7", assigned.rule_id)
    assert restored is not None
    assert restored.signal_state["active"] is False
    assert restored.signal_state["last_error"] is None
    store.close()


def test_building_signal_schema_and_python_target_bounds_match() -> None:
    properties = BUILDING_SIGNAL_RULE_SCHEMA["properties"]
    assert isinstance(properties, dict)
    item_schema = properties["item"]
    assert isinstance(item_schema, dict)
    assert item_schema["pattern"] == r"^[A-Za-z0-9_.:-]{1,128}$"
    assert normalize_rule(rule())["item"] == "iron-plate"
    with pytest.raises(ValueError):
        normalize_rule({**rule(), "item": "iron-plate\n"})


def test_building_signal_restarts_with_assignment_and_state(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    world = TargetBuildingSignalWorld(targets())
    store = OrchestratorStore(database)
    service = BuildingSignalService(store, world=world)
    assigned = service.assign(
        "owner:7",
        service.confirm("owner:7", service.create("owner:7", rule()).rule_id).rule_id,
        "smelter",
    )
    service.evaluate("owner:7", assigned.rule_id)
    store.close()

    reopened = OrchestratorStore(database)
    restored_service = BuildingSignalService(reopened, world=world)
    restored = restored_service.get("owner:7", assigned.rule_id)
    assert restored is not None
    assert restored.status == "assigned"
    assert restored.assignment == {"building_id": "smelter"}
    assert restored.signal_state["active"] is True
    reopened.close()


@pytest.mark.parametrize(
    "error_text",
    (
        "secret-token=unit-secret",
        "secret-token: unit-secret",
        '{"secret":"unit-secret"}',
        "Authorization: Bearer unit-secret",
    ),
)
def test_building_signal_fails_closed_and_redacts_world_errors(
    tmp_path: Path, error_text: str
) -> None:
    class SecretWorld(TargetBuildingSignalWorld):
        def inventory_count(self, *_args: str) -> int:
            raise RuntimeError(error_text)

    store, service = make_service(tmp_path, world=SecretWorld(targets()))
    assigned = service.assign(
        "owner:7",
        service.confirm("owner:7", service.create("owner:7", rule()).rule_id).rule_id,
        "smelter",
    )
    failed = service.evaluate("owner:7", assigned.rule_id)
    assert failed.signal_state["active"] is False
    assert "[redacted]" in str(failed.signal_state["last_error"])
    assert "unit-secret" not in str(failed.signal_state)
    store.close()


def test_building_signal_rejects_unknown_targets_and_duplicate_buildings(
    tmp_path: Path,
) -> None:
    store, service = make_service(tmp_path)
    with pytest.raises(RuntimeError, match="unknown item target"):
        service.create("owner:7", {**rule(), "item": "unknown"})

    first = service.create("owner:7", rule())
    service.assign(
        "owner:7", service.confirm("owner:7", first.rule_id).rule_id, "smelter"
    )
    second = service.create("owner:7", {**rule(), "signal": "green"})
    confirmed = service.confirm("owner:7", second.rule_id)
    with pytest.raises(RuntimeError, match="already assigned"):
        service.assign("owner:7", confirmed.rule_id, "smelter")
    store.close()


def test_building_signal_assignment_constraint_handles_store_race(
    tmp_path: Path,
) -> None:
    store, service = make_service(tmp_path)
    first_draft = service.create("owner:7", rule())
    service.assign(
        "owner:7", service.confirm("owner:7", first_draft.rule_id).rule_id, "smelter"
    )
    second_draft = service.create("owner:7", {**rule(), "signal": "green"})
    second = service.confirm("owner:7", second_draft.rule_id)

    with pytest.raises(StoreError, match="already assigned"):
        store.update_building_signal_rule(
            "owner:7",
            second.rule_id,
            status="assigned",
            assignment={"building_id": "smelter"},
            expected_status="confirmed",
        )
    store.close()


def test_building_signal_revalidates_persisted_rules_before_evaluation(
    tmp_path: Path,
) -> None:
    world = TargetBuildingSignalWorld(targets())
    store, service = make_service(tmp_path, world=world)
    draft = service.create("owner:7", rule())
    assigned = service.assign(
        "owner:7", service.confirm("owner:7", draft.rule_id).rule_id, "smelter"
    )
    service.evaluate("owner:7", assigned.rule_id)

    store.update_building_signal_rule(
        "owner:7",
        assigned.rule_id,
        rule={**rule(), "quantity": 1_000_001},
        expected_status="assigned",
    )
    invalid = service.evaluate("owner:7", assigned.rule_id)
    assert invalid.signal_state["active"] is True
    assert "Quantity must be an integer" in str(invalid.signal_state["last_error"])

    store.update_building_signal_rule(
        "owner:7", assigned.rule_id, rule=rule(), expected_status="assigned"
    )
    world._targets["owner:7"]["item"] = {}
    stale = service.evaluate("owner:7", assigned.rule_id)
    assert stale.signal_state["active"] is True
    assert "unknown item target" in str(stale.signal_state["last_error"])
    store.close()
