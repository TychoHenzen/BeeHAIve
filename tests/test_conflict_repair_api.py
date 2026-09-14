from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.models import ProjectSnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore
from beehaiive.workflow import (
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from main import create_app
from tests.conftest import FakeProvider as ProjectProviderDouble
from tests.support.conflict_repair.fake_provider import FakeProvider as FakeProvider
from tests.support.conflict_repair.fixture_check import FixtureCheck as FixtureCheck
from tests.support.conflict_repair.helpers import (
    make_conflict_repository,
    pull_request_snapshot,
)
from tests.support.conflict_repair.repair_agent import RepairAgent as RepairAgent

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_conflict_repair_api_returns_and_reads_durable_record(tmp_path: Path) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(
        replace(
            pull_request_snapshot(source_head, target_head),
            mergeable="MERGEABLE",
            merge_state="CLEAN",
        )
    )
    workflow_store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    repair_service = ConflictRepairService(
        workflow, provider, RepairAgent(), tmp_path / "repairs"
    )
    project_store = OrchestratorStore()
    project_provider = ProjectProviderDouble(ProjectSnapshot("project-1", "P", ()))
    routing_store = RoutingStore()
    app = create_app(
        orchestrator=Orchestrator(project_store, project_provider),
        api_key="key",
        model_router=ModelRouter(routing_store),
        routing_store=routing_store,
        workflow_service=workflow,
        conflict_repair_service=repair_service,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/workflow/conflict-repairs",
                headers={"X-API-Key": "key"},
                json={"repository": "owner/repo", "pull_request_number": 7},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "not_required"
            read = client.get(
                f"/workflow/conflict-repairs/{payload['repair_id']}",
                headers={"X-API-Key": "key"},
            )
            assert read.status_code == 200
            assert read.json()["repair_id"] == payload["repair_id"]
            missing = client.get(
                "/workflow/conflict-repairs/missing",
                headers={"X-API-Key": "key"},
            )
            assert missing.status_code == 404
        unconfigured = create_app(
            orchestrator=Orchestrator(project_store, project_provider),
            api_key="key",
            model_router=ModelRouter(routing_store),
            routing_store=routing_store,
            workflow_service=workflow,
        )
        with TestClient(unconfigured) as client:
            response = client.post(
                "/workflow/conflict-repairs",
                headers={"X-API-Key": "key"},
                json={"repository": "owner/repo", "pull_request_number": 7},
            )
            assert response.status_code == 503
            read = client.get(
                "/workflow/conflict-repairs/missing",
                headers={"X-API-Key": "key"},
            )
            assert read.status_code == 503
    finally:
        workflow_store.close()
        project_store.close()
        routing_store.close()
