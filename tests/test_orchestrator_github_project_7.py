import pytest

from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    ProviderError,
    _handoff_marker,
)
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)
from tests.support.orchestrator.racing_update_handoff_graph_ql_client import (
    RacingUpdateHandoffGraphQLClient as RacingUpdateHandoffGraphQLClient,
)


@pytest.mark.parametrize(
    ("state", "is_draft", "expected_error"),
    [
        ("OPEN", False, "not a draft"),
        ("CLOSED", True, "not open"),
        ("MERGED", False, "not open"),
    ],
)
def test_github_provider_never_updates_ready_or_closed_pull_requests(
    state: str, is_draft: bool, expected_error: str
) -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Closes #1",
        run_id="run-1",
    )
    client.pull_requests.append(
        {
            "id": "pull-request-node-8",
            "number": 8,
            "url": "https://example.test/owner/api/pull/8",
            "title": "Existing title",
            "state": state,
            "isDraft": is_draft,
            "headRefName": request.branch,
            "headRefOid": client.branch_sha,
            "baseRefName": "main",
            "body": _handoff_marker(request, "main"),
        }
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    with pytest.raises(ProviderError, match=expected_error):
        provider.create_handoff(request)

    assert client.pull_request_updates == 0
    assert client.pull_request_creations == 0


def test_github_provider_recovers_from_update_race() -> None:
    client = RacingUpdateHandoffGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Closes #1",
        run_id="run-1",
    )
    provider.create_handoff(request)

    result = provider.create_handoff(
        HandoffRequest(
            project_id="owner:7",
            repository="owner/api",
            pbi_number=1,
            title="API one updated",
            branch="codex/api-1",
            base_branch=None,
            body="Closes #1",
            run_id="run-1",
        )
    )

    assert result.pull_request_number == 8
    assert client.pull_request_creations == 1
    assert client.update_attempts == 2
    assert client.pull_request_updates == 1
    assert client.pull_requests[0]["title"] == "API one updated"
