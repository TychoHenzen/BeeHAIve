from fastapi.testclient import TestClient

import main as main_module
from beehaiive.agent import redact_worker_text
from beehaiive.dashboard.values import safe_dashboard_value
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.persistence.constants import DEFAULT_ACTION_LIMIT
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_exposes_one_supported_action_inventory() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    expected = sorted(main_module.DASHBOARD_ACTIONS)
    assert (
        client.get("/projects/project-1/actions").json()["supported_actions"]
        == expected
    )
    assert (
        client.get("/projects/project-1/dashboard").json()["supported_actions"]
        == expected
    )
    action_contract = client.get("/projects/project-1/actions").json()
    assert (
        action_contract["supported_action_owners"]
        == main_module.DASHBOARD_ACTION_OWNERS
    )
    assert (
        action_contract["supported_action_readback"]
        == main_module.DASHBOARD_ACTION_READBACK
    )
    synchronized = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={"action": "synchronize", "approved": True},
    )
    assert synchronized.status_code == 200
    assert synchronized.json()["action"]["status"] == "succeeded"
    service.store.close()


def test_dashboard_requeue_makes_a_blocked_autonomous_pbi_claimable() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    action = service.store.begin_action(
        "project-1",
        "autonomous_start",
        {"repository": "owner/api", "pbi_number": 1},
        "owner/api",
        1,
        "auto-1",
    )
    service.store.finish_action(
        str(action["id"]),
        "failed",
        {"status": "blocked"},
        "worker timed out",
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
        json={
            "action": "requeue",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": "auto-1",
        },
    )

    assert response.status_code == 200
    pbi = response.json()["state"]["repositories"][0]["pbis"][0]
    assert pbi["claimable"] is True
    assert pbi["status"] == "idle"
    assert pbi["autonomous_status"] is None
    service.store.close()


def test_dashboard_requeue_survives_provider_refresh_and_action_history_limit() -> None:
    provider = FakeProvider(dashboard_snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.stop(run.run_id, "Stopped by operator")
    provider.snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "API one",
                        stage=None,
                        planning_status="Done",
                        claimable=False,
                        metadata={"issue_state": "CLOSED"},
                    ),
                ),
            ),
        ),
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
        json={
            "action": "requeue",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
        },
    )
    assert response.json()["action"]["status"] == "succeeded"

    for action_number in range(DEFAULT_ACTION_LIMIT):
        action = service.store.begin_action(
            "project-1", "synchronize", {"number": action_number}
        )
        service.store.finish_action(str(action["id"]), "succeeded")

    refreshed = client.get("/projects/project-1/dashboard")
    pbi = refreshed.json()["repositories"][0]["pbis"][0]
    assert pbi["status"] == "idle"
    assert pbi["claimable"] is True
    assert pbi["run_id"] is None
    assert pbi["workflow_queue"]["id"] == "refinement"
    persisted = service.store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert persisted["claimable"] is True
    service.store.close()


def test_dashboard_rejects_unknown_and_unscoped_actions() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )
    auth = {"X-API-Key": "test-key"}

    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={"action": "unknown", "approved": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "start",
                "approved": True,
                "repository": "owner/api",
                "worker_id": " ",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "stop",
                "approved": True,
                "run_id": "run-1",
                "reason": " ",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "retry",
                "approved": True,
                "repository": "owner/api",
                "pbi_number": True,
                "run_id": "run-1",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "answer_question",
                "approved": True,
                "repository": "owner/api",
                "pbi_number": 1,
                "run_id": "run-1",
                "question_id": "question-1",
                "revision": True,
                "answer": "answer",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={"action": "synchronize", "approved": "yes"},
        ).status_code
        == 422
    )
    malformed_secret = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "synchronize",
            "approved": True,
            "api_key=secret-token": "secret-token",
        },
    )
    assert malformed_secret.status_code == 422
    assert "secret-token" not in malformed_secret.text
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "advance",
                "approved": True,
                "repository": "owner/api",
                "pbi_number": True,
                "run_id": "run-1",
                "target": "implement",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "start",
                "approved": True,
                "repository": "owner/api",
                "worker_id": "x" * 201,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={"action": "claim", "approved": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": "synchronize",
                "approved": True,
                "repository": "owner/api",
            },
        ).status_code
        == 422
    )
    service.store.close()


def test_dashboard_operator_actions_require_a_live_run_lease() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.store._connection.execute(
        "UPDATE runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE run_id = ?",
        (run.run_id,),
    )
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )
    auth = {"X-API-Key": "test-key"}
    for action in ("approve", "clarify"):
        response = client.post(
            "/projects/project-1/actions",
            headers=auth,
            json={
                "action": action,
                "approved": True,
                "repository": "owner/api",
                "pbi_number": 1,
                "run_id": run.run_id,
                **({"clarification": "late"} if action == "clarify" else {}),
            },
        )
        assert response.status_code == 403
    event_types = {
        event["type"]
        for event in service.store.project_state("project-1")["repositories"][0][
            "pbis"
        ][0]["events"]
    }
    assert not {"operator_approve", "operator_clarify"} & event_types
    service.store.close()


def test_dashboard_answers_redact_configured_secrets_before_persistence() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    question = service.store.await_operator(
        run.run_id,
        run.lease_token or "",
        kind="question",
        question="Which branch?",
        evidence={},
    )
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="secret-token",
            allowed_project_ids={"project-1"},
        )
    )
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "secret-token"},
        json={
            "action": "answer_question",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
            "question_id": question["question_id"],
            "revision": question["revision"],
            "answer": "api_key=secret-token",
        },
    )
    assert response.status_code == 200
    assert "secret-token" not in repr(response.json())
    stored = service.store.operator_question_for_run(run.run_id)
    assert stored is not None
    assert "secret-token" not in repr(stored)
    service.store.close()


def test_dashboard_action_text_is_bounded_and_redacted() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="secret-token",
            allowed_project_ids={"project-1"},
        )
    )
    auth = {"X-API-Key": "secret-token"}
    oversized = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
            "clarification": "x" * 1_001,
        },
    )
    assert oversized.status_code == 422
    redacted = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
            "clarification": "api_key=secret-token",
        },
    )
    assert redacted.status_code == 200
    assert "secret-token" not in repr(redacted.json())
    oversized_stop = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "stop",
            "approved": True,
            "run_id": run.run_id,
            "reason": "x" * 501,
        },
    )
    assert oversized_stop.status_code == 422
    service.store.close()


def test_dashboard_projection_bounds_synced_provider_values() -> None:
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "API one",
                        metadata={
                            "provider_note": "secret-token",
                            "provider_blob": "x" * 10_000,
                            "api_key=secret-token": "safe key",
                            "activity": [
                                {"message": "access_token=TOP"},
                                {"message": "client_secret=HIDDEN"},
                                {"message": "private_key=KEY"},
                            ],
                        },
                    ),
                ),
            ),
        ),
    )
    service = Orchestrator(OrchestratorStore(), FakeProvider(snapshot))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="secret-token",
            allowed_project_ids={"project-1"},
        )
    )
    dashboard = client.get("/projects/project-1/dashboard")
    sync = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "secret-token"},
        json={"action": "synchronize", "approved": True},
    )
    assert dashboard.status_code == 200
    assert sync.status_code == 200
    assert "secret-token" not in repr(dashboard.json())
    assert "secret-token" not in repr(sync.json())
    assert "x" * 4_001 not in repr(dashboard.json())
    assert all(
        value not in repr(dashboard.json()) for value in ("TOP", "HIDDEN", "KEY")
    )
    service.store.close()


def test_dashboard_preserves_schema_with_short_configured_secrets() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="result",
            allowed_project_ids={"project-1"},
        )
    )
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "result"},
        json={"action": "synchronize", "approved": True},
    )
    dashboard = client.get("/projects/project-1/dashboard")
    assert response.status_code == 200
    assert "result" in response.json()
    assert dashboard.json()["repositories"][0]["name"] == "owner/api"
    service.store.close()


def test_dashboard_rejects_mutations_for_inactive_pbis() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.stop(run.run_id, "finished")
    service.store._connection.execute(
        "UPDATE pbis SET active = 0 WHERE project_id = ? AND repository_name = ? "
        "AND number = ?",
        ("project-1", "owner/api", 1),
    )

    class UnexpectedWorker:
        def commit_and_push(self, _run_id: str):
            raise AssertionError("inactive PBI reached delivery")

    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            agent_worker=UnexpectedWorker(),
        )
    )
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "deliver",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
        },
    )
    assert response.status_code == 403
    service.store.close()


def test_dashboard_action_readback_survives_projection_failure() -> None:
    class FailingProjectionProvider(FakeProvider):
        def discover_project(self, project_id: str):
            if self.discoveries >= 1:
                raise RuntimeError("projection unavailable")
            return super().discover_project(project_id)

    service = Orchestrator(
        OrchestratorStore(), FailingProjectionProvider(dashboard_snapshot())
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
        json={"action": "synchronize", "approved": True},
    )
    assert response.status_code == 200
    assert response.json()["action"]["status"] == "succeeded"
    assert response.json()["state"] is None
    service.store.close()


def test_dashboard_refresh_errors_are_bounded_and_redacted() -> None:
    class FailingProvider(FakeProvider):
        def discover_project(self, project_id: str):
            del project_id
            raise RuntimeError("secret-token " + "x" * 10_000)

    service = Orchestrator(OrchestratorStore(), FailingProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="secret-token",
            allowed_project_ids={"project-1"},
        )
    )
    response = client.get("/projects/project-1/dashboard")
    assert response.status_code == 502
    assert "secret-token" not in response.text
    assert len(response.json()["detail"]) <= 4_000
    service.store.close()


def test_dashboard_keeps_the_latest_bounded_state_events() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    for sequence in range(120):
        service.store.record_operator_action(
            run.run_id,
            "approve",
            {"sequence": sequence},
        )
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="type",
            allowed_project_ids={"project-1"},
        )
    )
    response = client.get("/projects/project-1/dashboard?event_limit=500")
    events = response.json()["repositories"][0]["pbis"][0]["events"]
    sequences = [
        event["details"]["sequence"]
        for event in events
        if "sequence" in event.get("details", {})
    ]
    assert sequences == list(range(20, 120))
    service.store.close()


def test_dashboard_redaction_covers_prefixed_and_array_credentials() -> None:
    values = (
        "GITHUB_TOKEN=TOP",
        '{"client_secret":["TOP","TAIL"]}',
        "private_key=KEY",
        'clientSecretValue = [\n  "TOP",\n  {"safe": "TAIL"}\n]',
        "private_key=-----BEGIN PRIVATE KEY----- ABC DEF",
    )
    for value in values:
        redacted = redact_worker_text(value)
        assert all(secret not in redacted for secret in ("TOP", "TAIL", "KEY"))

    projected = safe_dashboard_value(
        {
            "task_result": {"evidence": {"api_key": "TOP"}},
            "apiKeys": ["TOP"],
            "githubToken": "TOP",
            "credentials": {"safe": "TAIL"},
        }
    )
    assert "TOP" not in repr(projected)
    assert "TAIL" not in repr(projected)
