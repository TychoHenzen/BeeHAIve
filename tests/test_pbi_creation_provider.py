from __future__ import annotations

import pytest

from beehaiive.pbi_creation import (
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationScopeError,
    PbiCreationService,
    PbiCreationValidationError,
)
from beehaiive.provider import GitHubProjectProvider
from beehaiive.storage import OrchestratorStore
from tests.support.fake_creation_graph_q_l_client import FakeCreationGraphQLClient


def make_request(
    body: str = "A body", labels: tuple[str, ...] = ()
) -> PbiCreationRequest:
    return PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body=body,
        labels=labels,
    )


def test_github_provider_creates_issue_and_confirms_backlog_membership() -> None:
    request = PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body="A body",
        labels=("enhancement",),
    )
    client = FakeCreationGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    target = provider.prepare_pbi_creation(request)
    checkpoints: list[PbiCreationProgress] = []

    result = provider.create_pbi(
        request, target, PbiCreationProgress(), checkpoints.append
    )

    assert result.issue_number == 37
    assert result.labels == ("enhancement",)
    assert result.project_status == "Backlog"
    assert client.rest_calls == [
        (
            "POST",
            "/repos/owner/repo/issues",
            {
                "title": "Create a PBI",
                "body": "A body",
                "labels": ["enhancement"],
            },
        )
    ]
    assert len(client.mutations) == 2
    assert "addProjectV2ItemById" in client.mutations[0][0]
    assert "updateProjectV2ItemFieldValue" in client.mutations[1][0]
    assert checkpoints[-1].completed_steps == (
        "issue_created",
        "labels_applied",
        "project_added",
        "status_backlog",
    )


def test_github_provider_rejects_unknown_label_before_issue_creation() -> None:
    request = PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body="A body",
        labels=("missing",),
    )
    client = FakeCreationGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    with pytest.raises(PbiCreationValidationError, match="labels do not exist"):
        provider.prepare_pbi_creation(request)

    assert client.rest_calls == []
    assert client.mutations == []


def test_github_provider_rejects_unlinked_repository_before_issue_creation() -> None:
    client = FakeCreationGraphQLClient(linked_repositories=())
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    with pytest.raises(PbiCreationScopeError, match="not linked"):
        provider.prepare_pbi_creation(make_request())

    assert client.rest_calls == []
    assert client.mutations == []


@pytest.mark.parametrize("failed_step", ("labels", "project", "backlog"))
def test_github_provider_retry_reconciles_after_remote_write(
    failed_step: str,
) -> None:
    store = OrchestratorStore()
    client = FakeCreationGraphQLClient(fail_after_step=failed_step)
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    service = PbiCreationService(store, provider)
    request = make_request(labels=("enhancement",))

    incomplete = service.create(request, f"retry-{failed_step}")
    completed = service.create(request, f"retry-{failed_step}")
    replay = service.create(request, f"retry-{failed_step}")

    assert incomplete["status"] == "incomplete"
    assert incomplete["issue"] == {
        "id": "issue-node-id",
        "number": 37,
        "url": "https://example.test/issues/37",
    }
    assert "private-provider-detail" not in str(incomplete)
    assert completed["status"] == "complete"
    assert replay == completed
    assert len(client.rest_calls) == 1
    assert sum("addProjectV2ItemById" in query for query, _ in client.mutations) == 1
    assert (
        sum("updateProjectV2ItemFieldValue" in query for query, _ in client.mutations)
        == 1
    )
    store.close()
