from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubOutcomeUnknownError,
    GitHubProjectProvider,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.orchestrator.concurrent_provider_handoff_graph_ql_client import (
    ConcurrentProviderHandoffGraphQLClient as ConcurrentProviderHandoffGraphQLClient,
)
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)


def test_github_provider_reconciles_timed_out_pr_before_retrying_create() -> None:
    class LatePullRequestClient(HandoffGraphQLClient):
        hidden_reads = 0

        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreatePullRequestInput" in query:
                super().execute(query, variables)
                self.hidden_reads = 2
                raise GitHubOutcomeUnknownError("request outcome is unknown")
            response = super().execute(query, variables)
            if "pullRequestCursor" in variables and self.hidden_reads:
                self.hidden_reads -= 1
                repository = response.get("repository")
                if isinstance(repository, dict):
                    pull_requests = repository.get("pullRequests")
                    if isinstance(pull_requests, dict):
                        pull_requests["nodes"] = []
            return response

    store = OrchestratorStore()
    client = LatePullRequestClient()
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

    with pytest.raises(GitHubOutcomeUnknownError):
        provider.create_handoff(request)

    pr_action = next(
        action
        for action in store.actions_for_project("owner:7")
        if action["kind"] == "github.create_pull_request"
    )
    assert pr_action["status"] == "uncertain"
    assert pr_action["result"]["reconciliation"] == "readback_absent"

    with pytest.raises(StoreError, match="remains unresolved"):
        provider.create_handoff(request)

    still_uncertain = next(
        action
        for action in store.actions_for_project("owner:7")
        if action["id"] == pr_action["id"]
    )
    assert still_uncertain["status"] == "uncertain"
    assert client.pull_request_creations == 1

    provider.create_handoff(request)

    reconciled = next(
        action
        for action in store.actions_for_project("owner:7")
        if action["id"] == pr_action["id"]
    )
    assert reconciled["status"] == "succeeded"
    assert reconciled["result"]["reconciliation"] == "present"
    assert client.pull_request_creations == 1


def test_github_provider_concurrent_calls_converge_on_one_artifact() -> None:
    client = ConcurrentProviderHandoffGraphQLClient()
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.create_handoff, request)
        second = executor.submit(provider.create_handoff, request)
        results = (first.result(), second.result())

    assert results[0] == results[1]
    assert results[0].branch == request.branch
    assert client.repository_reads >= 2
    assert client.ref_create_attempts == 2
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1
    assert len(client.pull_requests) == 1
