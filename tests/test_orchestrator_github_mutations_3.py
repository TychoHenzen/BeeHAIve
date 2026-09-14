from pathlib import Path
from typing import Any

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    GitHubRateLimitError,
)
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot


def test_github_provider_shares_rate_limit_retry_budget_across_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedHandoffClient(HandoffGraphQLClient):
        ref_attempts = 0
        pull_request_attempts = 0

        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreateRefInput" in query:
                self.ref_attempts += 1
                if self.ref_attempts == 1:
                    raise GitHubRateLimitError(
                        "ref limit", primary=True, reset_at=1_001
                    )
            elif "CreatePullRequestInput" in query:
                self.pull_request_attempts += 1
                raise GitHubRateLimitError("PR limit", primary=True, reset_at=1_001)
            return super().execute(query, variables)

    waits: list[float] = []
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)
    store = OrchestratorStore()
    client = RateLimitedHandoffClient()
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

    with pytest.raises(GitHubRateLimitError, match="PR limit"):
        provider.create_handoff(request)

    actions = store.actions_for_project("owner:7")
    ref_actions = [
        action for action in actions if action["kind"] == "github.create_ref"
    ]
    pr_actions = [
        action for action in actions if action["kind"] == "github.create_pull_request"
    ]
    assert waits == [1, 1, 1]
    assert client.ref_attempts == 2
    assert client.pull_request_attempts == 2
    assert [action["status"] for action in ref_actions] == ["succeeded", "failed"]
    assert [action["status"] for action in pr_actions] == ["failed", "failed"]


def test_rate_limit_exhaustion_preserves_handoff_intent_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedPullRequestClient(HandoffGraphQLClient):
        attempts = 0
        allow_create = False

        def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
            if "CreatePullRequestInput" in query and not self.allow_create:
                self.attempts += 1
                raise GitHubRateLimitError("PR limit", primary=True, reset_at=1_001)
            return super().execute(query, variables)

    class GitHubHandoffProvider(FakeProvider):
        def __init__(self, client: RateLimitedPullRequestClient) -> None:
            super().__init__(snapshot())
            self.github = GitHubProjectProvider("owner", 7, "token", client=client)

        def create_handoff(self, request: HandoffRequest) -> HandoffResult:
            return self.github.create_handoff(request)

    waits: list[float] = []
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)
    database = tmp_path / "state.sqlite3"
    client = RateLimitedPullRequestClient()
    provider = GitHubHandoffProvider(client)
    first_store = OrchestratorStore(database)
    first_service = Orchestrator(first_store, provider)
    first_service.synchronize("project-1")
    run = first_service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    first_service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    with pytest.raises(GitHubRateLimitError, match="PR limit"):
        first_service.handoff(
            run.run_id, "codex/api-1", "main", "Closes #1", lease_token
        )

    intent = first_store.pending_handoff(run.run_id, lease_token)
    assert intent is not None
    assert intent.branch == "codex/api-1"
    assert client.attempts == 3
    assert waits == [1, 1, 1]
    first_store.close()

    client.allow_create = True
    second_store = OrchestratorStore(database)
    second_service = Orchestrator(second_store, provider)
    resumed = second_service.claim("project-1", "owner/api", "worker-1", lease_token)
    assert resumed is not None
    assert resumed.stage is Stage.IMPLEMENT
    assert resumed.branch == "codex/api-1"
    completed = second_service.handoff(
        run.run_id, "codex/api-1", "main", "Closes #1", lease_token
    )

    assert completed.status.value == "completed"
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1
    assert second_store.pending_handoff(run.run_id, lease_token) is None
    second_store.close()
