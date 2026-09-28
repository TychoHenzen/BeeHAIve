from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.behavior_service import BehaviorService, RecordingUnitWorld
from beehaiive.orchestrator import Orchestrator
from beehaiive.service_failures import TargetProviderError
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app
from tests.support.api_provider import ApiProvider
from tests.test_behavior import bindings, definition


class FakeBehaviorModel:
    def generate(self, _prompt: str) -> object:
        return definition()


class UnavailableBehaviorModel:
    def generate(self, prompt: str) -> object:
        del prompt
        from beehaiive.behavior_model import BehaviorModelError

        raise BehaviorModelError("model_unavailable", "secret-token=model-secret")


class FailingTargetWorld(RecordingUnitWorld):
    def allowed_targets(self, project_id: str) -> Mapping[str, Collection[str]]:
        del project_id
        raise TargetProviderError("secret-token=target-secret")


def test_behavior_api_exposes_bounded_failure_classification(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "api-errors.sqlite3")
    service = BehaviorService(
        store, model_client=UnavailableBehaviorModel(), unit_world=RecordingUnitWorld()
    )
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            behavior_service=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )
    response = client.post(
        "/projects/owner:7/behaviors/generate",
        json={"prompt": "move the item"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "model_unavailable",
        "failure_class": "model",
        "message": "secret-token=[redacted]",
    }
    store.close()


def test_behavior_api_target_provider_failure_is_not_invalid_domain_state(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "api-target-errors.sqlite3")
    service = BehaviorService(store, unit_world=FailingTargetWorld())
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            behavior_service=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )
    response = client.post(
        "/projects/owner:7/behaviors",
        json={"definition": definition(), "bindings": {}},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["failure_class"] == "target_provider"
    assert "target-secret" not in response.text
    store.close()


def test_behavior_api_transition_persistence_failure_is_classified(
    tmp_path: Path, monkeypatch
) -> None:
    store = OrchestratorStore(tmp_path / "api-transition-persistence.sqlite3")
    service = BehaviorService(store, unit_world=RecordingUnitWorld())
    draft = service.create("owner:7", definition(), bindings())

    def fail_transition(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StoreError("database unavailable")

    monkeypatch.setattr(store, "update_unit_behavior", fail_transition)
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            behavior_service=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )

    response = client.post(
        f"/projects/owner:7/behaviors/{draft.behavior_id}/confirm",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "persistence",
        "failure_class": "persistence",
        "message": "database unavailable",
    }
    store.close()


def test_behavior_design_api_is_explicit_and_project_scoped(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "api.sqlite3")
    service = BehaviorService(
        store,
        model_client=FakeBehaviorModel(),
        unit_world=RecordingUnitWorld(),
    )
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            behavior_service=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_actor="operator",
        )
    )
    unauthenticated = client.post(
        "/projects/owner:7/behaviors/generate", json={"prompt": "move the item"}
    )
    assert unauthenticated.status_code == 401

    headers = {"X-API-Key": "test-key"}
    generated = client.post(
        "/projects/owner:7/behaviors/generate",
        json={"prompt": "move the item"},
        headers=headers,
    )
    assert generated.status_code == 200
    blank_prompt = client.post(
        "/projects/owner:7/behaviors/generate",
        json={"prompt": "   "},
        headers=headers,
    )
    assert blank_prompt.status_code == 422
    saved = client.post(
        "/projects/owner:7/behaviors",
        json={"definition": generated.json()["definition"]},
        headers=headers,
    )
    assert saved.status_code == 200
    behavior_id = saved.json()["behavior_id"]
    not_confirmed = client.post(
        f"/projects/owner:7/behaviors/{behavior_id}/assign",
        json={"unit_id": "unit-1"},
        headers=headers,
    )
    assert not_confirmed.status_code == 409
    bound = client.put(
        f"/projects/owner:7/behaviors/{behavior_id}/bindings",
        json={"bindings": bindings()},
        headers=headers,
    )
    assert bound.status_code == 200
    confirmed = client.post(
        f"/projects/owner:7/behaviors/{behavior_id}/confirm", headers=headers
    )
    assert confirmed.status_code == 200
    assigned = client.post(
        f"/projects/owner:7/behaviors/{behavior_id}/assign",
        json={"unit_id": "unit-1"},
        headers=headers,
    )
    assert assigned.status_code == 200
    executed = client.post(
        f"/projects/owner:7/behaviors/{behavior_id}/run", headers=headers
    )
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"
    design_page = client.get("/behavior-design")
    assert design_page.status_code == 200
    assert "Reload readback" in design_page.text
    assert (
        client.get(
            f"/projects/owner:7/behaviors/{behavior_id}", headers=headers
        ).json()["execution"]["status"]
        == "completed"
    )
    assert client.get("/projects/other:7/behaviors", headers=headers).status_code == 403
    store.close()
