from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.building_signal import TargetBuildingSignalWorld
from beehaiive.building_signal_service import BuildingSignalService
from beehaiive.orchestrator import Orchestrator
from beehaiive.persistence import OrchestratorStore
from beehaiive.service_failures import WorldActionError
from main import create_app
from tests.support.api_provider import ApiProvider


class FakeBuildingSignalModel:
    def generate_structured(self, _prompt: str, _schema: dict[str, object]) -> object:
        return {
            "schema_version": 1,
            "comparison": "lte",
            "quantity": 5,
            "item": "iron-plate",
            "signal": "green",
        }


class SecretFailureWorld(TargetBuildingSignalWorld):
    def inventory_count(self, project_id: str, building_id: str, item_id: str) -> int:
        del project_id, building_id, item_id
        raise WorldActionError("secret-token=inventory-secret")


def test_building_signal_api_is_scoped_and_persists_current_state(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "api.sqlite3")
    world = TargetBuildingSignalWorld(
        {
            "owner:7": {
                "building": {"smelter": {}},
                "item": {"iron-plate": {}},
                "signal": {"green": {}},
                "inventory": {"smelter": {"iron-plate": 3}},
            }
        }
    )
    signal_service = BuildingSignalService(
        store, model_client=FakeBuildingSignalModel(), world=world
    )
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            building_signal_service=signal_service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )
    headers = {"X-API-Key": "test-key"}

    assert client.get("/projects/owner:7/building-signals").status_code == 200
    generated = client.post(
        "/projects/owner:7/building-signals/generate",
        json={"prompt": "if iron plate is low"},
        headers=headers,
    )
    assert generated.status_code == 200
    assert generated.json()["rule"]["comparison"] == "lte"
    blank = client.post(
        "/projects/owner:7/building-signals/generate",
        json={"prompt": "   "},
        headers=headers,
    )
    assert blank.status_code == 422

    saved = client.post(
        "/projects/owner:7/building-signals",
        json={"rule": generated.json()["rule"]},
        headers=headers,
    )
    assert saved.status_code == 200
    rule_id = saved.json()["rule_id"]
    updated = client.put(
        f"/projects/owner:7/building-signals/{rule_id}",
        json={"rule": {**generated.json()["rule"], "quantity": 4}},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["rule"]["quantity"] == 4
    confirmed = client.post(
        f"/projects/owner:7/building-signals/{rule_id}/confirm", headers=headers
    )
    assert confirmed.status_code == 200
    assigned = client.post(
        f"/projects/owner:7/building-signals/{rule_id}/assign",
        json={"building_id": "smelter"},
        headers=headers,
    )
    assert assigned.status_code == 200
    evaluated = client.post(
        f"/projects/owner:7/building-signals/{rule_id}/evaluate", headers=headers
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["signal_state"]["active"] is True
    assert (
        client.get(
            f"/projects/owner:7/building-signals/{rule_id}", headers=headers
        ).json()["status"]
        == "assigned"
    )
    assert client.get("/building-signal-design").status_code == 200
    assert "Reload readback" in client.get("/building-signal-design").text
    assert (
        client.get("/projects/other:7/building-signals", headers=headers).status_code
        == 403
    )
    store.close()


def test_building_signal_api_reads_back_safe_state_and_failure_class(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "api-safe-state.sqlite3")
    world = SecretFailureWorld(
        {
            "owner:7": {
                "building": {"smelter": {}},
                "item": {"iron-plate": {}},
                "signal": {"green": {}},
                "inventory": {"smelter": {"iron-plate": 3}},
            }
        }
    )
    service = BuildingSignalService(
        store, model_client=FakeBuildingSignalModel(), world=world
    )
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            building_signal_service=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )
    headers = {"X-API-Key": "test-key"}
    saved = client.post(
        "/projects/owner:7/building-signals",
        json={
            "rule": {
                "schema_version": 1,
                "comparison": "lte",
                "quantity": 5,
                "item": "iron-plate",
                "signal": "green",
            }
        },
        headers=headers,
    )
    rule_id = saved.json()["rule_id"]
    client.post(
        f"/projects/owner:7/building-signals/{rule_id}/confirm", headers=headers
    )
    client.post(
        f"/projects/owner:7/building-signals/{rule_id}/assign",
        json={"building_id": "smelter"},
        headers=headers,
    )
    evaluated = client.post(
        f"/projects/owner:7/building-signals/{rule_id}/evaluate", headers=headers
    )
    assert evaluated.status_code == 200
    state = evaluated.json()["signal_state"]
    assert state["active"] is False
    assert state["failure_class"] == "world"
    assert "inventory-secret" not in evaluated.text
    readback = client.get(
        f"/projects/owner:7/building-signals/{rule_id}", headers=headers
    )
    assert readback.status_code == 200
    assert readback.json()["signal_state"] == state
    store.close()
