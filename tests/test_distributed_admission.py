from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from beehaiive.agent import AgentWorkerManager
from beehaiive.api.app import create_app
from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.persistence.admission import AdmissionError
from beehaiive.review import ReviewStore
from beehaiive.routing import RoutingStore
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider


def seed(store, project="p", repositories=("owner/a", "owner/b", "owner/c")):
    snapshot = ProjectSnapshot(
        project,
        project,
        tuple(
            RepositorySnapshot(repo, (PbiSnapshot(repo, 1, "Work", Stage.IMPLEMENT),))
            for repo in repositories
        ),
    )
    store.sync_project(snapshot)
    return snapshot


@pytest.mark.parametrize("capacity", [1, 2])
def test_concurrent_connections_share_cap(tmp_path: Path, capacity):
    path = tmp_path / "state.db"
    stores = [OrchestratorStore(path, admission_capacity=capacity) for _ in range(3)]
    seed(stores[0])
    barrier = Barrier(3)

    def claim(index):
        barrier.wait(timeout=10)
        try:
            return stores[index].claim_next("p", f"owner/{'abc'[index]}", f"w{index}")
        except AdmissionError as exc:
            assert exc.reason == "capacity_exhausted"
            return None

    with ThreadPoolExecutor(3) as pool:
        runs = list(pool.map(claim, range(3)))
    assert sum(run is not None for run in runs) == capacity
    assert stores[0].admission_state("p")["used"] == capacity
    assert (
        stores[0]._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        == capacity
    )
    for store in stores:
        store.close()


def test_cross_project_identity_and_rollback(tmp_path, monkeypatch):
    store = OrchestratorStore(tmp_path / "state.db", admission_capacity=2)
    seed(store, "p", ("Owner/A",))
    seed(store, "q", ("owner/a",))
    original = store._record_event

    def abort(*args):
        raise RuntimeError("injected rollback")

    monkeypatch.setattr(store, "_record_event", abort)
    with pytest.raises(RuntimeError, match="rollback"):
        store.claim_next("p", "Owner/A", "w1")
    assert store.admission_state("p")["used"] == 0
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM repository_generations"
        ).fetchone()[0]
        == 0
    )
    assert store._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    monkeypatch.setattr(store, "_record_event", original)
    assert store.claim_next("p", "Owner/A", "w1") is not None
    with pytest.raises(AdmissionError, match="repository_owned"):
        store.claim_next("q", "owner/a", "w2")
    assert store.admission_state("q")["reservations"] == []


def expire(store, run_id):
    for table, column in (("runs", "lease_expires_at"), ("admissions", "expires_at")):
        store._connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run_id),
        )


def test_renew_restart_takeover_and_old_release(tmp_path):
    path = tmp_path / "state.db"
    store = OrchestratorStore(path, admission_capacity=1)
    seed(store)
    run = store.claim_next("p", "owner/a", "w1")
    token = run.lease_token
    for _ in range(2):
        store.renew_lease(run.run_id, token)
        store.claim_next("p", "owner/a", "w1", token)
    assert store.admission_state("p")["used"] == 1
    store.close()
    store = OrchestratorStore(path, admission_capacity=1)
    assert store.admission_state("p")["reservations"][0]["generation"] == 1
    expire(store, run.run_id)
    replacement = store.claim_next("p", "owner/a", "w2")
    assert replacement.lease_token != token
    assert store.admission_state("p")["reservations"][0]["generation"] == 2
    with pytest.raises(StoreError):
        store.fail_agent_run_after_lease_loss(
            run.run_id, "late", expected_lease_token=token
        )
    with pytest.raises(StoreError):
        store.fail(run.run_id, "late", token)
    assert store.admission_state("p")["used"] == 1
    store.stop(run.run_id)
    store.stop(run.run_id)
    assert store.admission_state("p")["used"] == 0


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize(
    "operation",
    ["renew", "heartbeat", "advance", "event", "result", "handoff", "failure"],
)
def test_stale_mutations_are_fenced(operation, takeover):
    store = OrchestratorStore(admission_capacity=1)
    snapshot = seed(store)
    provider = FakeProvider(snapshot)
    service = Orchestrator(store, provider)
    run = store.claim_next("p", "owner/a", "w1")
    token = run.lease_token
    execution = store.claim_execution(run.run_id, token)
    store.start_agent_session(run.run_id, "w1", "task", token)
    store.ensure_task_contract(
        run.run_id,
        TaskContract("test", 1, "work", {}, (), (), tuple(TaskOutcome)),
        token,
    )
    expire(store, run.run_id)
    if takeover:
        store.claim_next("p", "owner/a", "w2")
    events_before = store._connection.execute("SELECT COUNT(*) FROM events").fetchone()[
        0
    ]
    calls = {
        "renew": lambda: store.renew_lease(run.run_id, token),
        "heartbeat": lambda: store.heartbeat_execution(run.run_id, token, execution),
        "advance": lambda: store.advance(run.run_id, Stage.IMPLEMENT, token),
        "event": lambda: store.record_agent_session_event(
            run.run_id, token, "message", "item.completed", "assistant", "late"
        ),
        "result": lambda: store.record_task_result(
            run.run_id, TaskResult(TaskOutcome.PASS, {}), token
        ),
        "handoff": lambda: service.handoff(run.run_id, "branch", None, "body", token),
        "failure": lambda: store.fail_agent_run_after_lease_loss(
            run.run_id, "late", expected_lease_token=token
        ),
    }
    with pytest.raises(StoreError):
        calls[operation]()
    assert provider.handoffs == []
    assert store.get_run(run.run_id).last_result is None
    assert store.get_run(run.run_id).task_result is None
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM agent_session_events"
        ).fetchone()[0]
        == 0
    )
    assert (
        store._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        == events_before
    )


def test_activation_conflicts_and_live_legacy_process(tmp_path):
    path = tmp_path / "state.db"
    legacy = OrchestratorStore(path)
    seed(legacy)
    a = legacy.claim_next("p", "owner/a", "w1")
    b = legacy.claim_next("p", "owner/b", "w2")
    with pytest.raises(AdmissionError, match="activation_conflict"):
        OrchestratorStore(path, admission_capacity=1)
    assert legacy.get_run(a.run_id) is not None
    legacy.stop(b.run_id)
    enabled = OrchestratorStore(path, admission_capacity=1)
    assert enabled.admission_state("p")["used"] == 1
    with pytest.raises(AdmissionError, match="authority_unavailable"):
        legacy.claim_next("p", "owner/b", "w2")
    for capacity in (None, 2):
        with pytest.raises(AdmissionError, match="authority_unavailable"):
            OrchestratorStore(path, admission_capacity=capacity)


def test_http_auth_denials_readback_and_authority_failure(monkeypatch):
    store = OrchestratorStore(admission_capacity=1)
    snapshot = seed(store)
    app = create_app(
        orchestrator=Orchestrator(store, FakeProvider(snapshot)),
        api_key="test-key",
        allowed_project_ids={"p"},
        review_store=ReviewStore(),
        routing_store=RoutingStore(),
    )
    client = TestClient(app)
    readback = "/projects/p/admission"
    path = "/projects/p/repositories/owner/a/claim"
    assert client.get(readback).status_code == 401
    assert client.post(path).status_code == 401
    headers = {"X-API-Key": "test-key", "X-Worker-ID": "token=worker-secret"}
    response = client.post(path, headers=headers)
    assert response.status_code == 200
    token = response.json()["lease_token"]
    headers["X-Worker-ID"] = "host-2"
    assert (
        client.post(path, headers=headers).json()["detail"]["reason"]
        == "repository_owned"
    )
    denied = client.post(path.replace("owner/a", "owner/b"), headers=headers)
    assert denied.status_code == 409
    assert denied.json()["detail"]["reason"] == "capacity_exhausted"
    state = client.get(readback, headers=headers)
    assert state.json()["used"] == 1
    assert state.json()["free"] == 0
    assert token not in state.text and "lease_token" not in state.text
    assert state.json()["reservations"][0]["generation"] == 1
    assert state.json()["reservations"][0]["owner_id"] == "token=[redacted]"
    assert "worker-secret" not in state.text
    assert "execution_token" not in state.text
    assert client.get("/projects/other/admission", headers=headers).status_code == 403
    store._connection.execute("UPDATE admission_config SET capacity = 2")
    unavailable = client.post(path, headers=headers)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["reason"] == "admission_authority_unavailable"


@pytest.mark.parametrize(
    "terminal", ["fail", "fail_agent", "stop", "complete", "handoff", "start_failure"]
)
def test_terminal_paths_release_slot(terminal):
    store = OrchestratorStore(admission_capacity=1)
    snapshot = seed(store)
    run = store.claim_next("p", "owner/a", "w1")
    token = run.lease_token
    if terminal == "fail":
        store.fail(run.run_id, "failed", token)
    elif terminal == "fail_agent":
        store.fail_agent_run(run.run_id, "failed", token)
    elif terminal == "stop":
        store.stop(run.run_id)
    elif terminal == "complete":
        contract = TaskContract("test", 1, "work", {}, (), (), tuple(TaskOutcome))
        store.ensure_task_contract(run.run_id, contract, token)
        store.record_task_result(run.run_id, TaskResult(TaskOutcome.PASS, {}), token)
        store.complete_agent_run(run.run_id, "done", token)
    elif terminal == "handoff":
        service = Orchestrator(store, FakeProvider(snapshot))
        service.handoff(run.run_id, "branch", None, "body", token)
    else:
        worker = AgentWorkerManager(
            Orchestrator(store, FakeProvider(snapshot)), object()
        )
        with pytest.raises(StoreError, match="Workflow service"):
            worker.start(run)
    assert store.admission_state("p")["used"] == 0
    assert store.claim_next("p", "owner/b", "w2") is not None


def test_retry_and_recovery_use_admission():
    store = OrchestratorStore(admission_capacity=1)
    snapshot = seed(store)
    service = Orchestrator(store, FakeProvider(snapshot))
    run = service.claim("p", "owner/a", "w1")
    store.fail(run.run_id, "failed", run.lease_token)
    other = service.claim("p", "owner/b", "w2")
    with pytest.raises(AdmissionError, match="capacity_exhausted"):
        service.retry("p", "owner/a", "w1", run.run_id)
    assert store.get_run(run.run_id).status.value == "failed"
    store.stop(other.run_id)
    resumed = service.retry("p", "owner/a", "w1", run.run_id)
    assert resumed.admission_generation == 2
    assert resumed.attempt == 2
    expire(store, resumed.run_id)
    recovery = service.claim("p", "owner/a", "w3", expected_run_id=run.run_id)
    assert recovery.admission_generation == 3
    assert store.admission_state("p")["used"] == 1


def test_expiry_recovery_retries_before_side_effect_then_quarantines():
    store = OrchestratorStore(admission_capacity=1)
    try:
        seed(store)
        run = store.claim_next("p", "owner/a", "w1")
        expire(store, run.run_id)
        first = store.recover_expired_admissions()
        assert first[0]["action"] == "reclaim_retry"
        replacement = store.claim_next("p", "owner/a", "w2")
        assert replacement is not None
        expire(store, replacement.run_id)
        second = store.recover_expired_admissions()
        assert second[0]["action"] == "quarantine"
        failed = store.get_run(run.run_id)
        assert failed is not None and failed.status.value == "failed"
        assert store.admission_state("p")["used"] == 0
        assert store.claim_next("p", "owner/a", "w3") is None
        store.set_pbi_claimable("p", "owner/a", 1)
        assert store.claim_next("p", "owner/a", "w3") is not None
        assert len(store.admission_state("p")["recoveries"]) == 2
    finally:
        store.close()


def test_expiry_after_persisted_task_result_quarantines() -> None:
    store = OrchestratorStore(admission_capacity=1)
    try:
        seed(store)
        run = store.claim_next("p", "owner/a", "w1")
        contract = TaskContract("test", 1, "work", {}, (), (), tuple(TaskOutcome))
        store.ensure_task_contract(run.run_id, contract, run.lease_token)
        store.record_task_result(
            run.run_id, TaskResult(TaskOutcome.PASS, {}), run.lease_token
        )
        expire(store, run.run_id)
        recovered = store.recover_expired_admissions()
        assert recovered[0]["action"] == "quarantine"
        assert store.get_run(run.run_id).status.value == "failed"
    finally:
        store.close()


def test_expiry_after_persisted_session_evidence_quarantines() -> None:
    store = OrchestratorStore(admission_capacity=1)
    try:
        seed(store)
        run = store.claim_next("p", "owner/a", "w1")
        store.start_agent_session(run.run_id, "w1", "task", run.lease_token)
        store.record_agent_session_event(
            run.run_id,
            run.lease_token,
            "progress",
            "turn.started",
            None,
            "turn.started",
        )
        expire(store, run.run_id)
        recovered = store.recover_expired_admissions()
        assert recovered[0]["action"] == "quarantine"
        assert store.get_run(run.run_id).status.value == "failed"
    finally:
        store.close()


def test_server_clock_expiry_reclaims_once(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from beehaiive.persistence import lease_store
    from beehaiive.persistence.helpers import lease_helpers

    instant = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(lease_helpers, "datetime", Clock)
    monkeypatch.setattr(lease_store, "datetime", Clock)
    store = OrchestratorStore(admission_capacity=1)
    seed(store)
    old = store.claim_next("p", "owner/a", "w1")
    instant += timedelta(seconds=301)
    with pytest.raises(StoreError, match="expired"):
        store.renew_lease(old.run_id, old.lease_token)
    assert store.admission_state("p")["used"] == 0
    new = store.claim_next("p", "owner/b", "w2")
    assert new is not None
    with pytest.raises(AdmissionError, match="capacity_exhausted"):
        store.claim_next("p", "owner/a", "w1")
    assert store.admission_state("p")["used"] == 1


def test_generation_mismatch_denies_mutation():
    store = OrchestratorStore(admission_capacity=1)
    seed(store)
    run = store.claim_next("p", "owner/a", "w1")
    store._connection.execute(
        "UPDATE repository_generations SET generation = generation + 1"
    )
    with pytest.raises(StoreError, match="admission"):
        store.renew_lease(run.run_id, run.lease_token)


def test_concurrent_projects_cannot_own_same_repository(tmp_path):
    path = tmp_path / "state.db"
    stores = [OrchestratorStore(path, admission_capacity=2) for _ in range(2)]
    seed(stores[0], "p", ("Owner/A",))
    seed(stores[0], "q", ("owner/a",))
    barrier = Barrier(2)

    def claim(index):
        barrier.wait(timeout=10)
        try:
            return stores[index].claim_next(
                ("p", "q")[index], ("Owner/A", "owner/a")[index], str(index)
            )
        except AdmissionError as exc:
            assert exc.reason == "repository_owned"
            return None

    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(claim, range(2)))
    assert sum(run is not None for run in runs) == 1
    assert stores[0].admission_state("p")["used"] == 1
    for store in stores:
        store.close()


def test_activation_rejects_existing_cross_project_writers(tmp_path):
    path = tmp_path / "state.db"
    store = OrchestratorStore(path)
    seed(store, "p", ("Owner/A",))
    seed(store, "q", ("owner/a",))
    assert store.claim_next("p", "Owner/A", "w1") is not None
    assert store.claim_next("q", "owner/a", "w2") is not None
    with pytest.raises(AdmissionError, match="activation_conflict"):
        OrchestratorStore(path, admission_capacity=2)
    assert store._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert store.admission_state("p") == {"enabled": False}


def test_shutdown_does_not_stop_replacement_worker():
    from types import SimpleNamespace

    store = OrchestratorStore(admission_capacity=1)
    snapshot = seed(store)
    service = Orchestrator(store, FakeProvider(snapshot))
    old = service.claim("p", "owner/a", "w1")
    worker = AgentWorkerManager(service, SimpleNamespace(cancel=lambda run_id: None))
    worker._threads[old.run_id] = SimpleNamespace(join=lambda **kwargs: None)
    worker._run_lease_tokens[old.run_id] = old.lease_token
    expire(store, old.run_id)
    replacement = service.claim("p", "owner/a", "w2")
    worker.shutdown()
    assert store.get_run(old.run_id).lease_token == replacement.lease_token
    assert store.admission_state("p")["used"] == 1


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_capacity_must_be_a_positive_integer(capacity):
    with pytest.raises(ValueError, match="positive integer"):
        OrchestratorStore(admission_capacity=capacity)
