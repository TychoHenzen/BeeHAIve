from __future__ import annotations

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
    PullRequestSnapshot,
    Stage,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    ProviderError,
    _mapping,
    _next_cursor,
    _nodes,
    _owner_query,
    _stage_from_status,
)
from beehaiive.storage import OrchestratorStore, _lease_is_active
from tests.support.edges.helpers import handoff_request as _handoff_request
from tests.support.edges.helpers import project_data as _project_data
from tests.support.edges.helpers import pull_request_data as _pull_request_data
from tests.support.edges.static_client import StaticClient as StaticClient


def test_provider_helpers_and_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ProviderError, match="invalid object"):
        _mapping(None)
    with pytest.raises(ProviderError, match="invalid nodes"):
        _nodes({"nodes": "bad"})
    with pytest.raises(ProviderError, match="end cursor"):
        _next_cursor({"pageInfo": {"hasNextPage": True}})
    with pytest.raises(ProviderError, match="owner type"):
        _owner_query("query", "team")
    assert _stage_from_status("Todo") is Stage.REFINE
    assert _stage_from_status("In Progress") is Stage.IMPLEMENT
    assert _stage_from_status("Done") is None
    assert _stage_from_status("unknown") is None

    marker = provider_module._handoff_marker(_handoff_request())
    assert provider_module._handoff_body(marker, marker) == marker
    with pytest.raises(ProviderError, match="identity"):
        provider_module._handoff_marker(
            HandoffRequest(
                "owner:7", "owner/api", 1, "API one", "branch", None, "", " "
            )
        )

    with pytest.raises(ValueError, match="lease_seconds"):
        OrchestratorStore(lease_seconds=0)
    assert not _lease_is_active(None)
    assert not _lease_is_active("not-a-timestamp")

    for variable in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PROJECT_OWNER",
        "GITHUB_PROJECT_NUMBER",
        "GITHUB_PROJECT_OWNER_TYPE",
        provider_module.DISCOVERY_CACHE_SECONDS_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ProviderError, match="Set GITHUB_TOKEN"):
        GitHubProjectProvider.from_environment()
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "owner")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "not-an-integer")
    with pytest.raises(ProviderError, match="must be an integer"):
        GitHubProjectProvider.from_environment()
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    configured = GitHubProjectProvider.from_environment()
    assert configured.project_id == "owner:7"
    assert configured._discovery_cache_seconds == 600
    monkeypatch.setenv(provider_module.DISCOVERY_CACHE_SECONDS_ENV, "0")
    assert GitHubProjectProvider.from_environment()._discovery_cache_seconds == 0
    monkeypatch.setenv("GITHUB_PROJECT_OWNER_TYPE", "organization")
    organization_configured = GitHubProjectProvider.from_environment()
    assert organization_configured.owner_type == "organization"

    monkeypatch.setenv("GITHUB_PROJECT_OWNER_TYPE", "team")
    with pytest.raises(ProviderError, match="owner type"):
        GitHubProjectProvider.from_environment()
    monkeypatch.setenv(provider_module.DISCOVERY_CACHE_SECONDS_ENV, "not-a-number")
    with pytest.raises(ProviderError, match="finite non-negative"):
        GitHubProjectProvider.from_environment()
    monkeypatch.setenv(provider_module.DISCOVERY_CACHE_SECONDS_ENV, "nan")
    with pytest.raises(ProviderError, match="finite non-negative"):
        GitHubProjectProvider.from_environment()


def test_provider_discovery_and_base_branch_validation() -> None:
    invalid_items = [
        {"content": None},
        {"content": {"__typename": "PullRequest"}},
        {
            "content": {
                "__typename": "Issue",
                "repository": {},
                "number": "not-an-integer",
                "title": None,
            }
        },
    ]
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=StaticClient(_project_data(items=invalid_items))
    )
    discovered = provider.discover_project("owner:7")
    assert discovered.repositories[0].pbis == ()

    with pytest.raises(ProviderError, match="configured"):
        provider.discover_project("owner:8")

    missing_title = GitHubProjectProvider(
        "owner", 7, "token", client=StaticClient(_project_data(title=None))
    )
    with pytest.raises(ProviderError, match="title"):
        missing_title.discover_project("owner:7")

    branch_client = StaticClient(
        {
            "repository": {
                "defaultBranchRef": {"name": "main"},
                "baseRef": {
                    "name": "release",
                    "target": {"oid": "release-oid"},
                },
            }
        }
    )
    branch_provider = GitHubProjectProvider("owner", 7, "token", client=branch_client)
    assert branch_provider.resolve_base_branch("owner/api", None) == "main"
    assert branch_provider.resolve_base_branch("owner/api", "release") == "release"
    assert (
        branch_provider.validate_handoff("owner/api", "codex/api-1", "release")
        == "release"
    )
    with pytest.raises(ProviderError, match="owner/name"):
        branch_provider.resolve_base_branch("invalid", None)

    missing_branch = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=StaticClient({"repository": {"defaultBranchRef": {}}}),
    )
    with pytest.raises(ProviderError, match="default branch"):
        missing_branch.resolve_base_branch("owner/api", None)


@pytest.mark.parametrize(
    ("mergeable", "merge_state", "expected"),
    [
        ("CONFLICTING", "DIRTY", "conflicting"),
        ("MERGEABLE", "CLEAN", "clean"),
        ("UNKNOWN", "UNKNOWN", "unknown"),
    ],
)
def test_provider_returns_pull_request_merge_evidence(
    mergeable: str, merge_state: str, expected: str
) -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=StaticClient(
            _pull_request_data(mergeable=mergeable, merge_state=merge_state)
        ),
    )

    snapshot = provider.get_pull_request("owner/api", 1)

    assert isinstance(snapshot, PullRequestSnapshot)
    assert snapshot.source_head == "source-head"
    assert snapshot.target_branch == "main"
    assert snapshot.target_head == "target-head"
    assert snapshot.conflict_state == expected


def test_provider_pull_request_evidence_fails_closed() -> None:
    contradictory = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=StaticClient(_pull_request_data(source_ref_name="different")),
    ).get_pull_request("owner/api", 1)
    assert contradictory.conflict_state == "unknown"
    assert contradictory.evidence_error

    missing_head = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=StaticClient(_pull_request_data(source_head=None)),
    ).get_pull_request("owner/api", 1)
    assert missing_head.conflict_state == "unknown"
    assert missing_head.evidence_error

    malformed = _pull_request_data()
    malformed["repository"]["pullRequest"]["id"] = None  # type: ignore[index]
    with pytest.raises(ProviderError, match="incomplete"):
        GitHubProjectProvider(
            "owner", 7, "token", client=StaticClient(malformed)
        ).get_pull_request("owner/api", 1)

    missing = GitHubProjectProvider(
        "owner", 7, "token", client=StaticClient(_pull_request_data(pull_request=None))
    )
    with pytest.raises(ProviderError, match="not found"):
        missing.get_pull_request("owner/api", 1)
    with pytest.raises(ProviderError, match="positive"):
        missing.get_pull_request("owner/api", 0)
    with pytest.raises(ProviderError, match="wrong"):
        GitHubProjectProvider(
            "owner", 7, "token", client=StaticClient(_pull_request_data())
        ).get_pull_request("owner/api", 2)
