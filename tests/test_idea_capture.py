from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

from fastapi.testclient import TestClient

import main as main_module
from beehaiive.autonomous import IDEA_CAPTURE_STEP, AutonomousLifecycleService
from beehaiive.models import PbiSnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_idea_capture_claim_is_atomic_across_store_connections(tmp_path: Path) -> None:
    database = tmp_path / "orchestrator.sqlite"
    stores = [OrchestratorStore(database), OrchestratorStore(database)]
    barrier = Barrier(2)

    def claim(store: OrchestratorStore) -> tuple[dict[str, object], bool]:
        barrier.wait()
        return store.begin_idea_capture(
            "project-1",
            "idea-key",
            {"action": "capture_idea", "idea": "Add a queue"},
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, stores))
        assert sorted(claimed for _, claimed in results) == [False, True]
        assert len({action["id"] for action, _ in results}) == 1
    finally:
        for store in stores:
            store.close()


def test_idea_capture_releases_proven_failure_but_retains_unknown_outcome() -> None:
    store = OrchestratorStore()
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "retry me"}
        )
        assert claimed
        store.finish_action(str(action["id"]), "failed", error="not started")
        store.release_idea_capture(str(action["id"]))
        retry, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "retry me"}
        )
        assert claimed and retry["id"] != action["id"]

        store.finish_action(
            str(retry["id"]),
            "failed",
            {"status": "outcome_unknown"},
            "reconcile before retry",
        )
        store.mark_idea_capture_outcome_unknown(str(retry["id"]))
        replay, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "retry me"}
        )
        assert not claimed
        assert replay["id"] == retry["id"]
        assert replay["result"] == {"status": "outcome_unknown"}
    finally:
        store.close()


def test_expired_idea_capture_lease_becomes_non_retryable() -> None:
    store = OrchestratorStore()
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "orphaned"}
        )
        assert claimed
        store.mark_idea_capture_started(str(action["id"]))
        with store._transaction() as connection:
            connection.execute(
                "UPDATE idea_captures SET lease_expires_at = ? WHERE action_id = ?",
                ("2000-01-01T00:00:00+00:00", action["id"]),
            )
        replay, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "orphaned"}
        )
        assert not claimed
        assert replay["id"] == action["id"]
        assert replay["status"] == "failed"
        assert replay["result"] == {
            "error": (
                "Idea capture lease expired; outcome is unknown and must be "
                "reconciled before retry."
            ),
            "operator_action": "Reconcile before retry.",
            "status": "outcome_unknown",
        }
    finally:
        store.close()


def test_dashboard_workers_share_one_capture_claim(tmp_path: Path) -> None:
    class CaptureExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def execute(self, step, _context, _handover):
            assert step is IDEA_CAPTURE_STEP
            with self.lock:
                self.calls += 1
            time.sleep(1.5)
            return {
                "status": "succeeded",
                "summary": "Created Backlog issue #8.",
                "handover": {"issue_number": 8, "project_status": "Backlog"},
            }

    database = tmp_path / "shared.sqlite"
    stores = [
        OrchestratorStore(database, lease_seconds=1),
        OrchestratorStore(database, lease_seconds=1),
    ]
    executor = CaptureExecutor()
    services = []
    clients = []
    snapshot = dashboard_snapshot(
        extra_pbis=(PbiSnapshot("owner/api", 8, "Captured", planning_status="Backlog"),)
    )
    try:
        for store in stores:
            orchestrator = Orchestrator(store, FakeProvider(snapshot))
            service = AutonomousLifecycleService(orchestrator, executor)
            services.append(service)
            clients.append(
                TestClient(
                    main_module.create_app(
                        orchestrator=orchestrator,
                        api_key="test-key",
                        allowed_project_ids={"project-1"},
                        autonomous_service=service,
                    )
                )
            )

        def submit(client: TestClient):
            return client.post(
                "/projects/project-1/actions",
                headers={"X-API-Key": "test-key"},
                json={"action": "capture_idea", "approved": True, "idea": "shared"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, clients))
        assert all(response.status_code == 200 for response in responses)
        action_ids = {response.json()["action"]["id"] for response in responses}
        assert len(action_ids) == 1

        time.sleep(1.1)
        duplicate = clients[1].post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={"action": "capture_idea", "approved": True, "idea": "shared"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["action"]["id"] in action_ids
        assert executor.calls == 1
        deadline = time.monotonic() + 3
        recorded = None
        while time.monotonic() < deadline:
            actions = (
                clients[0]
                .get("/projects/project-1/actions", headers={"X-API-Key": "test-key"})
                .json()["actions"]
            )
            recorded = next(action for action in actions if action["id"] in action_ids)
            if recorded["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert recorded is not None
        assert recorded["status"] == "succeeded"
    finally:
        for store in stores:
            store.close()


def test_prelaunch_executor_failure_releases_claim(monkeypatch) -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service = AutonomousLifecycleService(orchestrator, object())
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "retryable"}
        )
        assert claimed

        def fail_executor(*_args, **_kwargs):
            raise RuntimeError("executor could not start")

        monkeypatch.setattr(service, "_idea_executor", fail_executor)
        started = service.capture_idea("project-1", "retryable", str(action["id"]))
        deadline = time.monotonic() + 3
        while service.status(str(started["run_id"]))["status"] == "running":
            if time.monotonic() >= deadline:
                raise AssertionError("pre-launch failure did not finish")
            time.sleep(0.01)
        retry, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "retryable"}
        )
        assert claimed
        assert retry["id"] != action["id"]
    finally:
        store.close()


def test_reclaimed_claim_fences_the_stale_worker() -> None:
    store = OrchestratorStore()
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "stale", "idea_key": "idea-key"}
        )
        assert claimed
        store.release_idea_capture(str(action["id"]))
        retry, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "stale", "idea_key": "idea-key"}
        )
        assert claimed and retry["id"] != action["id"]
        assert store.mark_idea_capture_started(str(action["id"])) is False
    finally:
        store.close()


def test_codex_launch_failure_releases_claim() -> None:
    class LaunchFailExecutor:
        def execute(self, *_args, **_kwargs):
            raise FileNotFoundError("Skill file not found: missing")

    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service = AutonomousLifecycleService(orchestrator, LaunchFailExecutor())
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "launch", "idea_key": "idea-key"}
        )
        assert claimed
        started = service.capture_idea("project-1", "launch", str(action["id"]))
        deadline = time.monotonic() + 3
        while service.status(str(started["run_id"]))["status"] == "running":
            if time.monotonic() >= deadline:
                raise AssertionError("launch failure did not finish")
            time.sleep(0.01)
        retry, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "launch", "idea_key": "idea-key"}
        )
        assert claimed
        assert retry["id"] != action["id"]
    finally:
        store.close()


def test_executor_failure_replays_explicit_unknown_outcome() -> None:
    class FailingExecutor:
        def execute(self, *_args, **_kwargs):
            return {"status": "blocked", "summary": "provider outcome unknown"}

    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service = AutonomousLifecycleService(orchestrator, FailingExecutor())
    try:
        action, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "unknown"}
        )
        assert claimed
        started = service.capture_idea("project-1", "unknown", str(action["id"]))
        deadline = time.monotonic() + 3
        while service.status(str(started["run_id"]))["status"] == "running":
            if time.monotonic() >= deadline:
                raise AssertionError("unknown outcome did not finish")
            time.sleep(0.01)
        recorded = next(
            item
            for item in store.actions_for_project("project-1")
            if item["id"] == action["id"]
        )
        assert recorded["result"]["status"] == "outcome_unknown"
        replay, claimed = store.begin_idea_capture(
            "project-1", "idea-key", {"idea": "unknown"}
        )
        assert not claimed
        assert replay["id"] == action["id"]
    finally:
        store.close()
