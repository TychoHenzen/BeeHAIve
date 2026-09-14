from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    ReviewRepairTransitionStatus,
)
from beehaiive.routing import RoutingStore
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.helpers import record_pushed_repair, repair_harness

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_repair_api_authorizes_before_recovering_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    state = OrchestratorStore(":memory:")
    routing = RoutingStore(":memory:")
    try:
        attempt_id, _ = record_pushed_repair(harness)
        monkeypatch.setattr(harness.service, "recover", lambda: None)
        with TestClient(
            create_app(
                store=state,
                routing_store=routing,
                review_service=harness.reviews,
                review_repair_service=harness.service,
                api_key="test-key",
                review_actor="intruder",
            )
        ) as client:
            denied = client.get(
                f"/reviews/repairs/{attempt_id}",
                headers={"X-API-Key": "test-key"},
            )

        assert denied.status_code == 409
        assert (
            harness.review_store.repair_attempt(attempt_id).review_transition_status
            is ReviewRepairTransitionStatus.PENDING
        )
        assert harness.reviews.snapshot("owner/repo#1").cycle.cycle_number == 1
    finally:
        state.close()
        routing.close()
        harness.close()


def test_review_repair_api_enforces_operator_scope_and_demo_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CommitAgent()
    harness = repair_harness(tmp_path, agent)
    try:
        writer_state = OrchestratorStore(":memory:")
        writer_routing = RoutingStore(":memory:")
        try:
            with TestClient(
                create_app(
                    store=writer_state,
                    routing_store=writer_routing,
                    review_service=harness.reviews,
                    review_repair_service=harness.service,
                    api_key="test-key",
                    review_actor="writer",
                )
            ) as writer_client:
                denied = writer_client.post(
                    f"/reviews/cycles/{harness.cycle_id}/repair",
                    json={"finding_ids": [harness.selected_id]},
                    headers={"X-API-Key": "test-key"},
                )
                assert denied.status_code == 409
                assert agent.calls == 0
        finally:
            writer_state.close()
            writer_routing.close()

        monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "demo")
        demo_state = OrchestratorStore(":memory:")
        demo_routing = RoutingStore(":memory:")
        try:
            with TestClient(
                create_app(
                    store=demo_state,
                    routing_store=demo_routing,
                    review_service=harness.reviews,
                    review_repair_service=harness.service,
                    api_key="test-key",
                    review_actor="operator",
                )
            ) as demo_client:
                disabled = demo_client.post(
                    f"/reviews/cycles/{harness.cycle_id}/repair",
                    json={"finding_ids": [harness.selected_id]},
                    headers={"X-API-Key": "test-key"},
                )
                assert disabled.status_code == 503
                assert agent.calls == 0
        finally:
            demo_state.close()
            demo_routing.close()
    finally:
        harness.close()
