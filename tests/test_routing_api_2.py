from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.quality_gates import RepositoryGateSuite
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelSpec,
    RoutingDecision,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from beehaiive.workflow import (
    DeterministicCheckRunner,
)
from main import create_app
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import (
    attach_run_workspace,
    build_routing_config,
    workflow_service_for_attempt,
)
from tests.support.routing.leased_fake_routing_model import (
    LeasedFakeRoutingModel as LeasedFakeRoutingModel,
)
from tests.support.routing.passing_quality_check import (
    PassingQualityCheck as _PassingQualityCheck,
)
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


@pytest.mark.parametrize("gate_mode", ["allowed", "blocked", "unbound", "unconfigured"])
def test_run_attempt_api_enforces_quality_gates(tmp_path: Path, gate_mode: str) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    model: FakeRoutingModel = LeasedFakeRoutingModel()
    if gate_mode == "unbound":
        model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    workflow_service = None
    workflow_store = None
    if gate_mode in {"allowed", "unbound"}:
        workflow_service, workflow_store = workflow_service_for_attempt(
            tmp_path, DeterministicCheckRunner([_PassingQualityCheck()])
        )
    elif gate_mode == "blocked":
        workflow_service, workflow_store = workflow_service_for_attempt(
            tmp_path, RepositoryGateSuite()
        )
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
            workflow_service=workflow_service,
        )
    )
    headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}
    client.post("/projects/owner:7/sync", headers=headers)
    claim = client.post(
        "/projects/owner:7/repositories/owner/api/claim", headers=headers
    )
    run_id = claim.json()["run_id"]
    if workflow_store is not None:
        attach_run_workspace(workflow_store, tmp_path, run_id)
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim.json()["lease_token"],
    }
    client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )

    attempted = client.post(f"/runs/{run_id}/attempt", headers=lease_headers)

    if gate_mode == "allowed":
        assert isinstance(model, LeasedFakeRoutingModel)
        assert attempted.status_code == 200
        assert attempted.json()["routing"]["attempt"]["model"] == "cheap-writer"
        assert attempted.json()["routing"]["attempt"]["total_tokens"] == 10
        assert attempted.json()["routing"]["state"]["status"] == "resolved"
        assert model.models == ["cheap-writer"]
        assert model.workspace_path is None
        assert model.release_calls == 1
        diagnostics_url = f"/workflow/runs/{run_id}/quality-gates"
        assert client.get(diagnostics_url).status_code == 401
        assert (
            client.get(
                diagnostics_url,
                headers={"X-API-Key": "test-key", "X-Lease-Token": "invalid"},
            ).status_code
            == 403
        )
        diagnostics = client.get(diagnostics_url, headers=lease_headers)
        assert diagnostics.status_code == 200
        gate = diagnostics.json()["quality_gates"]["model_call"]
        assert gate["allowed"] is True
        assert "argv" in gate["checks"][0]
        assert "stdout" in gate["checks"][0]
        dashboard = client.get("/projects/owner:7/dashboard").json()
        pbi = next(
            pbi
            for repository in dashboard["repositories"]
            for pbi in repository["pbis"]
            if pbi["run_id"] == run_id
        )
        summary = pbi["quality_gates"]["model_call"]
        assert summary["checks"][0]["status"] == "passed"
        assert "stdout" not in summary["checks"][0]
    elif gate_mode == "blocked":
        assert workflow_service is not None
        assert workflow_store is not None
        assert attempted.status_code == 409
        gate = attempted.json()["detail"]
        assert gate["allowed"] is False
        assert gate["checks"][0]["status"] == "configuration_missing"
        assert model.models == []
        lease = workflow_service.workspace_for_run(run_id)
        assert lease is not None
        persisted = workflow_store.latest_gate(lease.lease_id, "model_call")
        assert persisted is not None and persisted.as_dict() == gate
    elif gate_mode == "unbound":
        assert attempted.status_code == 503
        assert model.models == []
    else:
        assert attempted.status_code == 503
        assert model.models == []

    if workflow_store is not None:
        workflow_store.close()
    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_requires_executor_and_implementation_stage() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, RoutingProvider())
    with pytest.raises(StoreError, match="router and executor"):
        service.run_implementation_attempt("missing", "missing")
    store.close()

    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    with pytest.raises(StoreError, match="implementation run"):
        service.run_implementation_attempt(run.run_id, run.lease_token or "")

    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_surfaces_router_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    def reject_execution(*args: object, **kwargs: object) -> object:
        raise RoutingError("routing execution rejected")

    monkeypatch.setattr(router, "execute", reject_execution)
    with pytest.raises(StoreError, match="routing execution rejected"):
        service.run_implementation_attempt(run.run_id, token)

    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_does_not_handoff_when_task_result_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())

    class QuestionModel:
        def execute(
            self, _spec: ModelSpec, _decision: RoutingDecision
        ) -> ModelExecution:
            return ModelExecution(
                AttemptOutcome.SUCCESS,
                task_result=TaskResult(
                    TaskOutcome.QUESTION, {}, question="Which branch?"
                ),
            )

    model = QuestionModel()
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    def reject_result(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StoreError("result storage unavailable")

    monkeypatch.setattr(orchestrator_store, "record_task_result", reject_result)
    with pytest.raises(StoreError, match="result storage unavailable"):
        service.run_implementation_attempt(run.run_id, token)
    assert router.snapshot(run.run_id).state.status is RoutingStatus.ACTIVE
    assert router.snapshot(run.run_id).attempts == ()

    routing_store.close()
    orchestrator_store.close()
