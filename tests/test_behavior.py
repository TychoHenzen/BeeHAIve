from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from beehaiive.behavior import BehaviorValidationError, normalize_definition
from beehaiive.behavior_service import (
    BehaviorService,
    BehaviorServiceError,
    RecordingUnitWorld,
    TargetUnitWorld,
)
from beehaiive.persistence.errors import StateConflictError
from beehaiive.service_failures import FailureCategory, WorldActionError
from beehaiive.storage import OrchestratorStore, StoreError


def definition() -> dict[str, object]:
    return {
        "name": "Move inspected item",
        "initial_state": "grab",
        "parameters": {
            "storage": {"kind": "storage"},
            "item": {"kind": "item"},
            "signal": {"kind": "signal"},
            "factory": {"kind": "factory"},
        },
        "states": [
            {
                "id": "grab",
                "actions": [
                    {
                        "type": "grab",
                        "arguments": {"storage": "storage", "item": "item"},
                    }
                ],
            },
            {
                "id": "inspect",
                "actions": [
                    {
                        "type": "inspect_signal",
                        "arguments": {"target": "signal"},
                    }
                ],
            },
            {
                "id": "deposit",
                "actions": [
                    {
                        "type": "bring",
                        "arguments": {"target": "factory"},
                    },
                    {
                        "type": "deposit",
                        "arguments": {"target": "factory"},
                    },
                ],
            },
            {"id": "done", "actions": []},
        ],
        "transitions": [
            {"from": "grab", "to": "inspect", "condition": {"kind": "always"}},
            {
                "from": "inspect",
                "to": "deposit",
                "condition": {
                    "kind": "signal",
                    "target": "signal",
                    "equals": "ready",
                },
            },
            {"from": "deposit", "to": "done", "condition": {"kind": "always"}},
        ],
        "max_steps": 8,
    }


def bindings() -> dict[str, object]:
    return {
        "storage": {"kind": "storage", "id": "storage-a"},
        "item": {"kind": "item", "id": "item-1"},
        "signal": {"kind": "signal", "id": "signal-a"},
        "factory": {"kind": "factory", "id": "factory-a"},
    }


def test_definition_rejects_arbitrary_actions_and_unbounded_waits() -> None:
    invalid = definition()
    invalid["states"] = [
        {"id": "start", "actions": [{"type": "shell", "arguments": {}}]}
    ]
    invalid["initial_state"] = "start"
    invalid["transitions"] = []
    with pytest.raises(BehaviorValidationError, match="Unsupported behavior action"):
        normalize_definition(invalid)

    invalid = definition()
    invalid["states"] = [
        {
            "id": "start",
            "actions": [{"type": "wait", "arguments": {"seconds": 31}}],
        }
    ]
    invalid["initial_state"] = "start"
    invalid["transitions"] = []
    with pytest.raises(BehaviorValidationError, match="Wait duration"):
        normalize_definition(invalid)

    invalid = definition()
    invalid["transitions"] = [
        *definition()["transitions"],
        {"from": "grab", "to": "inspect", "condition": {"kind": "always"}},
    ]
    with pytest.raises(BehaviorValidationError, match="Duplicate transition"):
        normalize_definition(invalid)


def test_bindings_use_known_targets_and_bounded_wait_parameters(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "binding.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition())
    with pytest.raises(RuntimeError, match="unknown storage target"):
        service.bind(
            "owner:7",
            draft.behavior_id,
            {"storage": {"kind": "storage", "id": "storage-unknown"}},
        )

    wait_definition = definition()
    wait_definition["parameters"] = {
        **wait_definition["parameters"],
        "delay": {"kind": "wait"},
    }
    wait_definition["initial_state"] = "wait"
    wait_definition["states"] = [
        {
            "id": "wait",
            "actions": [{"type": "wait", "arguments": {"seconds": "delay"}}],
        },
        {"id": "done", "actions": []},
    ]
    wait_definition["transitions"] = [
        {"from": "wait", "to": "done", "condition": {"kind": "always"}}
    ]
    wait_definition["max_steps"] = 2
    wait = service.create("owner:7", wait_definition)
    with pytest.raises(RuntimeError, match="between 0 and 30"):
        service.bind(
            "owner:7",
            wait.behavior_id,
            {"delay": {"kind": "wait", "seconds": 31}},
        )
    bound = service.bind(
        "owner:7",
        wait.behavior_id,
        {**bindings(), "delay": {"kind": "wait", "seconds": 2}},
    )
    confirmed = service.confirm("owner:7", bound.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    completed = service.run("owner:7", assigned.behavior_id)
    assert completed.status == "completed"
    assert completed.execution["history"][0]["observation"]["seconds"] == 2.0
    store.close()


def test_behavior_requires_confirmation_before_assignment_or_run(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "behavior.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition())
    with pytest.raises(RuntimeError, match="Only confirmed behaviors can be assigned"):
        service.assign("owner:7", draft.behavior_id, "unit-1")
    with pytest.raises(RuntimeError, match="Only assigned behaviors can execute"):
        service.run("owner:7", draft.behavior_id)
    service.bind("owner:7", draft.behavior_id, bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    completed = service.run("owner:7", assigned.behavior_id)
    assert completed.status == "completed"
    assert [entry["action"] for entry in completed.execution["history"]] == [
        "grab",
        "inspect_signal",
        "bring",
        "deposit",
    ]
    store.close()


def test_behavior_record_survives_store_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    store = OrchestratorStore(database)
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    service.assign("owner:7", confirmed.behavior_id, "unit-1")
    store.close()

    reopened = OrchestratorStore(database)
    service_after_restart = BehaviorService(reopened, unit_world=RecordingUnitWorld())
    restored = service_after_restart.get("owner:7", draft.behavior_id)
    assert restored.status == "assigned"
    assert restored.bindings == bindings()
    assert restored.assignment == {"unit_id": "unit-1"}
    assert service_after_restart.run("owner:7", draft.behavior_id).status == "completed"
    reopened.close()


def test_target_world_executes_example_against_configured_targets(
    tmp_path: Path,
) -> None:
    targets = {
        "owner:7": {
            "storage": {"storage-a": {}},
            "item": {"item-1": {}},
            "signal": {"signal-a": {"value": "ready"}},
            "factory": {"factory-a": {}},
        }
    }
    store = OrchestratorStore(tmp_path / "target-world.sqlite3")
    service = BehaviorService(store, unit_world=TargetUnitWorld(targets))
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    completed = service.run("owner:7", assigned.behavior_id)
    assert completed.status == "completed"
    assert [entry["action"] for entry in completed.execution["history"]] == [
        "grab",
        "inspect_signal",
        "bring",
        "deposit",
    ]
    store.close()


def test_transition_matching_requires_declared_condition_kind(tmp_path: Path) -> None:
    class WrongKindWorld(RecordingUnitWorld):
        def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
            result = dict(super().execute(*args, **kwargs))
            if args[2] == "inspect_signal":
                result["kind"] = "color"
            return result

    store = OrchestratorStore(tmp_path / "condition.sqlite3")
    service = BehaviorService(store, unit_world=WrongKindWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    completed = service.run("owner:7", assigned.behavior_id)
    assert completed.status == "completed"
    assert [entry["action"] for entry in completed.execution["history"]] == [
        "grab",
        "inspect_signal",
    ]
    store.close()


def test_in_flight_action_is_idempotent_across_store_restart(tmp_path: Path) -> None:
    class CrashAfterExecutionWorld(RecordingUnitWorld):
        crash_once = True

        def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
            result = dict(super().execute(*args, **kwargs))
            if self.crash_once:
                self.crash_once = False
                raise KeyboardInterrupt("simulated process crash")
            return result

    database = tmp_path / "idempotency.sqlite3"
    world = CrashAfterExecutionWorld()
    store = OrchestratorStore(database)
    service = BehaviorService(store, unit_world=world)
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        service.run("owner:7", assigned.behavior_id)
    checkpoint = service.get("owner:7", assigned.behavior_id)
    assert checkpoint.status == "running"
    assert checkpoint.execution["in_flight"]["action"] == "grab"
    store.close()

    reopened = OrchestratorStore(database)
    restored = BehaviorService(reopened, unit_world=world).run(
        "owner:7", assigned.behavior_id
    )
    assert restored.status == "completed"
    assert world.executed_actions.count(("unit-1", "grab")) == 1
    reopened.close()


def test_unit_failure_error_is_redacted(tmp_path: Path) -> None:
    class SecretFailureWorld(RecordingUnitWorld):
        def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise WorldActionError("secret-token=unit-secret " + "x" * 2048)

    store = OrchestratorStore(tmp_path / "redaction.sqlite3")
    service = BehaviorService(store, unit_world=SecretFailureWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    failed = service.run("owner:7", assigned.behavior_id)
    assert failed.status == "failed"
    assert failed.execution["failure_class"] == "world"
    assert str(failed.execution["error"]).startswith("[redacted]")
    assert len(str(failed.execution["error"])) <= 512
    assert "unit-secret" not in str(failed.execution["error"])
    store.close()


def test_behavior_state_race_keeps_state_conflict_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OrchestratorStore(tmp_path / "state-race.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition(), bindings())

    def fail_transition(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise StateConflictError("Behavior state changed before it could be updated")

    monkeypatch.setattr(store, "update_unit_behavior", fail_transition)
    with pytest.raises(BehaviorServiceError) as raised:
        service.confirm("owner:7", draft.behavior_id)
    assert raised.value.code == "state_conflict"
    assert raised.value.category is FailureCategory.VALIDATION
    store.close()


def test_behavior_transition_persistence_failure_is_not_state_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OrchestratorStore(tmp_path / "transition-persistence.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition(), bindings())

    def fail_transition(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise StoreError("database unavailable")

    monkeypatch.setattr(store, "update_unit_behavior", fail_transition)
    with pytest.raises(BehaviorServiceError) as raised:
        service.confirm("owner:7", draft.behavior_id)
    assert raised.value.code == "persistence"
    assert raised.value.category is FailureCategory.PERSISTENCE
    store.close()


def test_malformed_observation_key_is_persisted_as_world_failure(
    tmp_path: Path,
) -> None:
    class MalformedObservationWorld(RecordingUnitWorld):
        def execute(self, *args: object, **kwargs: object) -> dict[object, object]:
            del args, kwargs
            return {1: "malformed"}

    store = OrchestratorStore(tmp_path / "malformed-observation.sqlite3")
    service = BehaviorService(store, unit_world=MalformedObservationWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")

    failed = service.run("owner:7", assigned.behavior_id)

    assert failed.status == "failed"
    assert failed.execution["failure_class"] == "world"
    assert failed.execution["error"] == "Unit returned an invalid observation key"
    assert service.get("owner:7", assigned.behavior_id).status == "failed"
    store.close()


def test_behavior_persistence_failure_is_not_recorded_as_domain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OrchestratorStore(tmp_path / "persistence.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    original_update = store.update_unit_behavior

    def fail_checkpoint(project_id: str, behavior_id: str, **kwargs: Any) -> Any:
        if "in_flight" in (kwargs.get("execution") or {}):
            raise StoreError("checkpoint unavailable")
        return original_update(project_id, behavior_id, **kwargs)

    monkeypatch.setattr(store, "update_unit_behavior", fail_checkpoint)
    with pytest.raises(BehaviorServiceError) as raised:
        service.run("owner:7", assigned.behavior_id)
    assert raised.value.code == "persistence"
    assert raised.value.category is FailureCategory.PERSISTENCE
    persisted = service.get("owner:7", assigned.behavior_id)
    assert persisted.status == "running"
    assert "failure_class" not in persisted.execution
    store.close()


def test_unexpected_behavior_defect_propagates_without_domain_failure(
    tmp_path: Path,
) -> None:
    class DefectiveWorld(RecordingUnitWorld):
        def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("programming defect")

    store = OrchestratorStore(tmp_path / "unexpected.sqlite3")
    service = BehaviorService(store, unit_world=DefectiveWorld())
    draft = service.create("owner:7", definition(), bindings())
    confirmed = service.confirm("owner:7", draft.behavior_id)
    assigned = service.assign("owner:7", confirmed.behavior_id, "unit-1")
    with pytest.raises(AssertionError, match="programming defect"):
        service.run("owner:7", assigned.behavior_id)
    assert service.get("owner:7", assigned.behavior_id).status == "running"
    store.close()
