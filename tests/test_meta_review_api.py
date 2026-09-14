from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from beehaiive.meta_review import (
    MetaReviewService,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.pbi_creation import PbiCreationValidationError
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from main import _handle_meta_review_error, create_app
from tests.conftest import FakeProvider
from tests.support.meta_review.helpers import complete_meta_review, seed_meta_review


def test_meta_review_api_requires_key_and_never_calls_provider(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    complete_meta_review(store)
    provider = FakeProvider(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)),),
        )
    )
    orchestrator = Orchestrator(store, provider, ModelRouter(routing))
    app = create_app(
        orchestrator=orchestrator,
        meta_review_service=MetaReviewService(store, routing),
        api_key="test-key",
        allowed_project_ids={"project-1"},
    )
    other_store = OrchestratorStore()
    try:
        with pytest.raises(ValueError, match="state store"):
            create_app(
                orchestrator=orchestrator,
                meta_review_service=MetaReviewService(other_store, routing),
                api_key="test-key",
                allowed_project_ids={"project-1"},
            )
    finally:
        other_store.close()

    other_routing = RoutingStore()
    try:
        with pytest.raises(ValueError, match="routing store"):
            create_app(
                orchestrator=orchestrator,
                meta_review_service=MetaReviewService(store, other_routing),
                api_key="test-key",
                allowed_project_ids={"project-1"},
            )
    finally:
        other_routing.close()

    def fail_meta_review() -> dict[str, object]:
        raise StoreError("token=hidden")

    with pytest.raises(HTTPException) as error:
        _handle_meta_review_error(fail_meta_review)
    assert error.value.status_code == 409
    assert error.value.detail == "Meta-review request could not be completed"

    def fail_pbi_creation() -> dict[str, object]:
        raise PbiCreationValidationError("Invalid accepted-suggestion PBI")

    with pytest.raises(HTTPException) as pbi_error:
        _handle_meta_review_error(fail_pbi_creation)
    assert pbi_error.value.status_code == 422
    assert pbi_error.value.detail == "Invalid accepted-suggestion PBI"

    with TestClient(app) as client:
        denied = client.post("/projects/project-1/meta-review")
        assert denied.status_code == 401
        triggered = client.post(
            "/projects/project-1/meta-review",
            headers={"X-API-Key": "test-key"},
            json={},
        )
        assert triggered.status_code == 200, triggered.text
        suggestion = triggered.json()["suggestions"][0]
        listed = client.get("/projects/project-1/meta-review/suggestions")
        assert listed.status_code == 200
        decided = client.post(
            f"/projects/project-1/meta-review/suggestions/{suggestion['suggestion_id']}",
            headers={"X-API-Key": "test-key"},
            json={"decision": "reject"},
        )
        assert decided.status_code == 200
        assert decided.json()["suggestion"]["status"] == "rejected"
    assert provider.discoveries == 0
    assert provider.handoffs == []


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
