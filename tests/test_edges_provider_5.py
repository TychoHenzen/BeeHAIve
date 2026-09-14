from __future__ import annotations

import pytest

import beehaiive.provider as provider_module
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    GitHubRateLimitError,
    ProviderError,
)
from beehaiive.storage import OrchestratorStore
from tests.support.edges.always_rate_limited_client import (
    AlwaysRateLimitedClient as AlwaysRateLimitedClient,
)
from tests.support.edges.helpers import handoff_request as _handoff_request
from tests.support.edges.helpers import project_data as _project_data
from tests.support.edges.race_client import RaceClient as RaceClient
from tests.support.edges.rate_limited_after_discovery_client import (
    RateLimitedAfterDiscoveryClient as RateLimitedAfterDiscoveryClient,
)
from tests.support.edges.static_client import StaticClient as StaticClient
from tests.support.edges.stub_provider import StubProvider as StubProvider


def test_provider_caches_discovery_and_serves_stale_snapshot_on_limit() -> None:
    with pytest.raises(ProviderError, match="cache seconds"):
        GitHubProjectProvider("owner", 7, "token", discovery_cache_seconds=-1)

    cached_client = StaticClient(_project_data())
    cached_provider = GitHubProjectProvider(
        "owner", 7, "token", client=cached_client, discovery_cache_seconds=60
    )
    first_snapshot = cached_provider.discover_project("owner:7")
    assert cached_provider.discover_project("owner:7") == first_snapshot
    assert len(cached_client.calls) == 3
    sync_store = OrchestratorStore()
    try:
        orchestrator = Orchestrator(sync_store, cached_provider)
        orchestrator.synchronize("owner:7")
        assert len(cached_client.calls) == 3
        orchestrator.synchronize("owner:7", force_refresh=True)
        assert len(cached_client.calls) == 6
    finally:
        sync_store.close()

    limited_client = RateLimitedAfterDiscoveryClient(_project_data())
    limited_provider = GitHubProjectProvider(
        "owner", 7, "token", client=limited_client, discovery_cache_seconds=0
    )
    stale_snapshot = limited_provider.discover_project("owner:7")
    assert limited_provider.discover_project("owner:7") == stale_snapshot
    assert len(limited_client.calls) == 3

    uncached_provider = GitHubProjectProvider(
        "owner", 7, "token", client=AlwaysRateLimitedClient()
    )
    with pytest.raises(GitHubRateLimitError):
        uncached_provider.discover_project("owner:7")


def test_provider_supports_organization_projects_and_holds_unmanaged_statuses() -> None:
    project_data = _project_data(
        items=[
            {
                "content": {
                    "__typename": "Issue",
                    "repository": {"nameWithOwner": "owner/api"},
                    "number": 1,
                    "title": "Done item",
                },
                "fieldValues": {
                    "nodes": [{"name": "Done", "field": {"name": "Status"}}]
                },
            },
            {
                "content": {
                    "__typename": "Issue",
                    "repository": {"nameWithOwner": "owner/api"},
                    "number": 2,
                    "title": "Blocked item",
                },
                "fieldValues": {
                    "nodes": [{"name": "Blocked", "field": {"name": "Status"}}]
                },
            },
        ]
    )
    organization_data = {"organization": project_data["user"]}
    client = StaticClient(organization_data)
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=client, owner_type="organization"
    )

    discovered = provider.discover_project("owner:7")
    pbis = discovered.repositories[0].pbis

    assert [pbi.stage for pbi in pbis] == [None, None]
    assert [pbi.planning_status for pbi in pbis] == ["Done", "Blocked"]
    assert all(not pbi.claimable for pbi in pbis)
    assert all("organization(login:" in query for query in client.calls)


@pytest.mark.parametrize(
    "mode, message",
    [
        ("ref-query-error", "reference already exists"),
        ("ref-still-missing", "reference already exists"),
        ("pr-query-error", "pull request already exists"),
        ("pr-still-missing", "pull request already exists"),
    ],
)
def test_provider_preserves_create_errors_when_recheck_fails(
    mode: str, message: str
) -> None:
    provider = GitHubProjectProvider("owner", 7, "token", client=RaceClient(mode))
    with pytest.raises(ProviderError, match=message):
        provider.create_handoff(_handoff_request())


def test_environment_provider_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubProvider()
    factory_calls = 0

    def factory(cls: type[GitHubProjectProvider]) -> StubProvider:
        nonlocal factory_calls
        factory_calls += 1
        return stub

    monkeypatch.setattr(
        provider_module.GitHubProjectProvider,
        "from_environment",
        classmethod(factory),
    )
    provider = provider_module.EnvironmentGitHubProvider()
    request = _handoff_request()
    assert provider.discover_project("owner:7").project_id == "owner:7"
    assert provider.create_handoff(request).pull_request_number == 1
    assert provider.resolve_base_branch("owner/api", None) == "main"
    assert provider.validate_handoff("owner/api", "codex/api-1", None) == "main"
    pull_request = provider.get_pull_request("owner/api", 1)
    provider.update_source_branch(pull_request, "worktree", "head", "head")
    provider.invalidate_discovery_cache()
    assert stub.cache_invalidated
    assert factory_calls == 1
