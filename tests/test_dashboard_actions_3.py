import pytest
from fastapi.testclient import TestClient

import main as main_module
from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    DEFAULT_ACTION_LIMIT,
    OrchestratorStore,
    StoreError,
    _json_mapping,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.failing_dashboard_provider import (
    FailingDashboardProvider as FailingDashboardProvider,
)
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_action_failure_can_return_no_existing_state() -> None:
    service = Orchestrator(
        OrchestratorStore(), FailingDashboardProvider(dashboard_snapshot())
    )
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={"action": "start", "approved": True},
    )

    assert response.status_code == 200
    assert response.json()["action"]["status"] == "failed"
    assert response.json()["state"] is None
    assert main_module._dashboard_pbi(service, "project-1", "owner/api", 1) is None

    invalid_target = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": "missing",
        },
    )
    assert invalid_target.status_code == 403


def test_action_store_records_lifecycle_and_validates_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()

    with pytest.raises(StoreError, match="stop reason"):
        store.stop("missing", "")
    with pytest.raises(StoreError, match="Unknown run"):
        store.stop("missing")
    with pytest.raises(StoreError, match="action project"):
        store.begin_action("", "", {})

    pending = store.begin_action("project-1", "approve", {"approved": True})
    assert pending["status"] == "pending"
    assert store.actions_for_project("project-1") == [pending]

    with pytest.raises(StoreError, match="Invalid action status"):
        store.finish_action(str(pending["id"]), "pending")
    with pytest.raises(StoreError, match="Unknown action"):
        store.finish_action("missing", "succeeded")
    with pytest.raises(StoreError, match="action_limit"):
        store.actions_for_project("project-1", 0)

    completed = store.finish_action(str(pending["id"]), "succeeded", {"approved": True})
    assert completed["status"] == "succeeded"
    assert completed["result"] == {"approved": True}

    real_connection = store._connection

    class EmptyCursor:
        def fetchone(self) -> None:
            return None

    class MissingActionRowConnection:
        def execute(self, statement: str, parameters: object = ()) -> object:
            if "SELECT * FROM actions WHERE action_id" in statement:
                return EmptyCursor()
            return real_connection.execute(statement, parameters)

        def __getattr__(self, name: str) -> object:
            return getattr(real_connection, name)

    monkeypatch.setattr(
        store,
        "_connection",
        MissingActionRowConnection(),
    )
    with pytest.raises(StoreError, match="Could not create action"):
        store.begin_action("project-1", "approve", {"approved": True})

    assert _json_mapping("") == {}
    assert _json_mapping("not-json") == {}
    assert _json_mapping("[]") == {}


def test_github_mutation_audit_is_visible_through_bounded_actions_api() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )
    oldest_id = str(store.begin_action("project-1", "operator", {"sequence": 0})["id"])
    for number in range(DEFAULT_ACTION_LIMIT - 2):
        store.begin_action("project-1", "operator", {"sequence": number + 1})
    request = HandoffRequest(
        project_id="project-1",
        repository="owner/api",
        pbi_number=1,
        title="private audit title",
        branch="codex/audit",
        base_branch="main",
        body="private audit body",
        run_id="run-audit",
    )
    audit_id = store.begin_handoff_mutation(
        request,
        "create_ref",
        "stable-operation-key",
        {"branch": request.branch, "base_branch": "main", "base_sha": "base-sha"},
    )
    store.finish_handoff_mutation(
        audit_id,
        "succeeded",
        {"reconciliation": "mutation_response", "branch": request.branch},
    )
    newest_id = str(
        store.begin_action("project-1", "operator", {"sequence": "newest"})["id"]
    )
    foreign_id = str(
        store.begin_action("other-project", "operator", {"sequence": "foreign"})["id"]
    )

    response = client.get(
        "/projects/project-1/actions", headers={"X-API-Key": "test-key"}
    )

    actions = response.json()["actions"]
    action_ids = {str(action["id"]) for action in actions}
    audit_action = next(action for action in actions if action["id"] == audit_id)
    assert response.status_code == 200
    assert len(actions) == DEFAULT_ACTION_LIMIT
    assert audit_id in action_ids and newest_id in action_ids
    assert oldest_id not in action_ids and foreign_id not in action_ids
    assert audit_action["request"]["operation_key"] == "stable-operation-key"
    assert audit_action["result"]["reconciliation"] == "mutation_response"
    assert "private audit title" not in repr(actions)
    assert "private audit body" not in repr(actions)
