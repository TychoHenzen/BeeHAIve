from dataclasses import replace
from typing import Any

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    GitHubRateLimitError,
    ProviderError,
    _handoff_marker,
)
from beehaiive.storage import OrchestratorStore
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)
from tests.support.orchestrator.paginated_pull_request_client import (
    PaginatedPullRequestClient as PaginatedPullRequestClient,
)
from tests.support.orchestrator.racing_handoff_graph_ql_client import (
    RacingHandoffGraphQLClient as RacingHandoffGraphQLClient,
)
from tests.support.orchestrator.wrong_target_racing_handoff_graph_ql_client import (
    WrongTargetRacingHandoffGraphQLClient as WrongTargetRacingHandoffGraphQLClient,
)


def test_github_provider_retries_secondary_pr_limits_with_identity_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedPullRequestClient(HandoffGraphQLClient):
        attempts = 0

        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreatePullRequestInput" in query:
                self.attempts += 1
                if self.attempts <= 2:
                    raise GitHubRateLimitError(
                        "secondary limit",
                        retry_after=3,
                        reset_at=9_999,
                        primary=False,
                        remaining=7,
                    )
            return super().execute(query, variables)

    waits: list[float] = []
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)
    store = OrchestratorStore()
    client = RateLimitedPullRequestClient()
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
        mutation_audit=store,
    )

    result = provider.create_handoff(request)

    actions = store.actions_for_project("owner:7")
    pr_actions = [
        action for action in actions if action["kind"] == "github.create_pull_request"
    ]
    assert result.pull_request_number == 8
    assert waits == [3, 6]
    assert [action["request"]["attempt"] for action in pr_actions] == [3, 2, 1]
    assert [action["status"] for action in pr_actions] == [
        "succeeded",
        "failed",
        "failed",
    ]
    assert client.attempts == 3
    assert client.pull_request_creations == 1


def test_github_provider_does_not_reuse_pull_request_for_another_run() -> None:
    client = HandoffGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    first_request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Closes #1",
        run_id="run-1",
    )
    second_request = replace(first_request, run_id="run-2")

    first = provider.create_handoff(first_request)
    with pytest.raises(ProviderError, match="different handoff identity"):
        provider.create_handoff(second_request)

    assert first.pull_request_number == 8
    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 0


def test_github_provider_uses_custom_base_branch_commit_and_target() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = False
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch="release",
        body="Closes #1",
        run_id="run-1",
    )

    provider.create_handoff(request)

    assert client.ref_oids == ["release-oid"]
    assert client.pull_request_bases == ["release"]


def test_github_provider_paginates_pull_requests_when_reusing_one() -> None:
    client = PaginatedPullRequestClient()
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
    client.pull_requests[-1]["body"] = _handoff_marker(request, "main")

    result = provider.create_handoff(request)

    assert result.pull_request_number == 101
    assert client.pull_request_creations == 0


def test_github_provider_recovers_from_create_races() -> None:
    client = RacingHandoffGraphQLClient()
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

    result = provider.create_handoff(request)

    assert result.branch == request.branch
    assert result.pull_request_number == 8
    assert client.ref_exists
    assert client.ref_create_attempts == 1
    assert client.pull_request_create_attempts == 1
    assert len(client.pull_requests) == 1
    ref_index = client.events.index("create-ref")
    pr_index = client.events.index("create-pull-request")
    assert client.events[ref_index + 1] == "read"
    assert client.events[pr_index + 1] == "read"


def test_github_provider_rejects_ambiguous_branch_with_unexpected_target() -> None:
    client = WrongTargetRacingHandoffGraphQLClient()
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

    with pytest.raises(ProviderError, match="intended base commit"):
        provider.create_handoff(request)

    assert client.pull_request_creations == 0


def test_github_provider_rejects_unverified_branch_target_mismatch() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    client.branch_sha = "unexpected-oid"
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

    with pytest.raises(ProviderError, match="intended base commit"):
        provider.create_handoff(request)

    assert client.ref_creations == 0
    assert client.pull_request_creations == 0
