from typing import Any

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    GitHubRateLimitError,
    _handoff_marker,
)
from beehaiive.storage import OrchestratorStore
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)


def test_github_provider_audits_redacted_mutations_and_recovers_pending_attempts() -> (
    None
):
    store = OrchestratorStore()
    client = HandoffGraphQLClient()
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

    provider.create_handoff(request)

    actions = store.actions_for_project("owner:7")
    actions_by_kind = {str(action["kind"]): action for action in actions}
    assert set(actions_by_kind) == {
        "github.create_ref",
        "github.create_pull_request",
    }
    assert all(action["status"] == "succeeded" for action in actions)
    ref_request = actions_by_kind["github.create_ref"]["request"]
    pr_request = actions_by_kind["github.create_pull_request"]["request"]
    assert isinstance(ref_request, dict)
    assert isinstance(pr_request, dict)
    assert ref_request["operation_key"] == _handoff_marker(request, "main")
    assert ref_request["attempt"] == pr_request["attempt"] == 1
    assert set(ref_request) == {"operation_key", "attempt", "target"}
    assert "title" not in ref_request and "title" not in pr_request
    assert "body" not in ref_request and "body" not in pr_request
    assert "API one" not in repr(actions) and "Closes #1" not in repr(actions)

    ref_action = store.begin_handoff_mutation(
        request,
        "create_ref",
        _handoff_marker(request, "main"),
        {"branch": request.branch, "base_branch": "main", "base_sha": "base-oid"},
    )
    pr_action = store.begin_handoff_mutation(
        request,
        "create_pull_request",
        _handoff_marker(request, "main"),
        {"branch": request.branch, "base_branch": "main"},
    )
    ref_creations = client.ref_creations
    pr_creations = client.pull_request_creations

    provider.create_handoff(request)

    recovered = {
        str(action["id"]): action for action in store.actions_for_project("owner:7")
    }
    assert recovered[ref_action]["status"] == "succeeded"
    assert recovered[pr_action]["status"] == "succeeded"
    assert client.ref_creations == ref_creations
    assert client.pull_request_creations == pr_creations


def test_github_provider_retries_primary_rate_limit_after_safe_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedRefClient(HandoffGraphQLClient):
        fail_ref_once = True

        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreateRefInput" in query and self.fail_ref_once:
                self.fail_ref_once = False
                raise GitHubRateLimitError(
                    "private provider detail",
                    primary=True,
                    retry_after=15,
                    reset_at=1_040,
                )
            return super().execute(query, variables)

    waits: list[float] = []
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)
    store = OrchestratorStore()
    client = RateLimitedRefClient()
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
    ref_actions = [
        action for action in actions if action["kind"] == "github.create_ref"
    ]
    assert result.pull_request_number == 8
    assert waits == [40]
    assert [action["request"]["attempt"] for action in ref_actions] == [2, 1]
    assert [action["status"] for action in ref_actions] == ["succeeded", "failed"]
    failed = next(action for action in ref_actions if action["status"] == "failed")
    assert failed["error"] == "GitHubRateLimitError"
    assert failed["result"]["reconciliation"] == "readback_absent"
    assert failed["result"]["rate_limit"] == {
        "classification": "primary",
        "reset_at": 1_040,
        "retry_after": 15,
    }
    assert "private provider detail" not in repr(failed)
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1


def test_github_provider_reuses_artifacts_created_before_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AppliedThenLimitedClient(HandoffGraphQLClient):
        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreateRefInput" in query or "CreatePullRequestInput" in query:
                super().execute(query, variables)
                raise GitHubRateLimitError(
                    "rate limit after mutation",
                    primary=True,
                    reset_at=1_040,
                )
            return super().execute(query, variables)

    waits: list[float] = []
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)
    client = AppliedThenLimitedClient()
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

    assert result.pull_request_number == 8
    assert waits == [40, 40]
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1
