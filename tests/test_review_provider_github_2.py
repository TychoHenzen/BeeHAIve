from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    ReviewStore,
)
from beehaiive.review_github import (
    GitHubReviewProvider,
    github_review_readers,
)
from main import create_app
from tests.support.provider.review_graph_ql_client import (
    ReviewGraphQLClient as ReviewGraphQLClient,
)


def test_production_adapters_work_through_review_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.setenv("BEEHAIIVE_REVIEW_ACTOR", "operator")
    store = ReviewStore()
    pull_request_id = "owner/repo#7"
    try:
        app = create_app(
            review_store=store,
            review_provider=GitHubReviewProvider(client=ReviewGraphQLClient()),
            review_readers=github_review_readers(),
            api_key="review-api-key",
            require_review_adapters=True,
        )
        with TestClient(app) as client:
            ready = client.post(
                "/reviews/ready",
                json={"pull_request_id": pull_request_id},
                headers={"X-API-Key": "review-api-key"},
            )
            assert ready.status_code == 200, ready.text
            assert all(
                reader["status"] == "pending" for reader in ready.json()["readers"]
            )

            snapshot = client.get(
                f"/reviews/pull-requests/{quote(pull_request_id, safe='')}",
                headers={"X-API-Key": "review-api-key"},
            )
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["cycle"]["pull_request_id"] == pull_request_id
            assert snapshot.json()["cycle"]["github_evidence"]["pull_request"][
                "id"
            ] == ("PULL_REQUEST_NODE")
    finally:
        store.close()
