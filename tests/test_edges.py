from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffIntent,
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    GitHubRateLimitError,
    ProviderError,
    UrllibGraphQLClient,
    _dashboard_metadata,
    _mapping,
    _next_cursor,
    _nodes,
    _owner_query,
    _stage_from_status,
)
from beehaiive.storage import OrchestratorStore, StoreError, _lease_is_active


class StaticClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.calls.append(query)
        return self.data


class FakeResponse:
    def __init__(self, payload: object, headers: object | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def _project_data(
    *, title: object = "Planning", items: list[object] | None = None
) -> dict[str, Any]:
    return {
        "user": {
            "projectV2": {
                "title": title,
                "repositories": {
                    "nodes": [{"nameWithOwner": "owner/api"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "items": {
                    "nodes": items or [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
    }


def _handoff_request(*, base_branch: str | None = None) -> HandoffRequest:
    return HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=base_branch,
        body="Closes #1",
        run_id="run-1",
    )


def test_urllib_graphql_client_validates_transport_and_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UrllibGraphQLClient("token", "https://example.test/graphql")

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"data": {"ok": True}}),
    )
    assert client.execute("query", {}) == {"ok": True}

    for error in (
        HTTPError("https://example.test", 500, "failed", {}, None),
        URLError("offline"),
        TimeoutError("timeout"),
        OSError("reset"),
    ):

        def raise_error(
            request: object, timeout: int, error: BaseException = error
        ) -> object:
            raise error

        monkeypatch.setattr(provider_module, "urlopen", raise_error)
        with pytest.raises(ProviderError, match="request failed"):
            client.execute("query", {})

    for payload, message in (
        ([], "non-object"),
        ({"errors": ["bad"]}, "returned errors"),
        ({"errors": [{"type": "OTHER"}]}, "returned errors"),
        ({"data": []}, "did not contain data"),
    ):
        monkeypatch.setattr(
            provider_module,
            "urlopen",
            lambda request, timeout, payload=payload: FakeResponse(payload),
        )
        with pytest.raises(ProviderError, match=message):
            client.execute("query", {})

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse(b"not-json"),
    )
    with pytest.raises(ProviderError, match="invalid JSON"):
        client.execute("query", {})

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse(b"\xff"),
    )
    with pytest.raises(ProviderError, match="invalid JSON"):
        client.execute("query", {})


def test_urllib_graphql_client_honors_primary_and_secondary_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    primary_calls = 0

    def primary_response(request: object, timeout: float) -> FakeResponse:
        nonlocal primary_calls
        primary_calls += 1
        return FakeResponse(
            {
                "errors": [
                    {
                        "type": "RATE_LIMIT",
                        "code": "graphql_rate_limit",
                        "message": "API rate limit already exceeded",
                    }
                ]
            },
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4000000000"},
        )

    monkeypatch.setattr(provider_module, "urlopen", primary_response)
    with pytest.raises(GitHubRateLimitError) as primary_error:
        primary_client.execute("query", {})
    assert primary_error.value.primary
    assert primary_error.value.reset_at == 4_000_000_000
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        primary_client.execute("query", {})
    assert primary_calls == 1

    secondary_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    secondary_calls = 0

    def secondary_response(request: object, timeout: float) -> FakeResponse:
        nonlocal secondary_calls
        secondary_calls += 1
        raise HTTPError(
            "https://example.test/graphql",
            429,
            "too many requests",
            {"retry-after": "60"},
            None,
        )

    monkeypatch.setattr(provider_module, "urlopen", secondary_response)
    with pytest.raises(GitHubRateLimitError) as secondary_error:
        secondary_client.execute("query", {})
    assert not secondary_error.value.primary
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        secondary_client.execute("query", {})
    assert secondary_calls == 1

    secondary_403_client = UrllibGraphQLClient("token", "https://example.test/graphql")

    def secondary_403_response(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://example.test/graphql",
            403,
            "secondary rate limit",
            {"retry-after": "60"},
            None,
        )

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        secondary_403_response,
    )
    with pytest.raises(GitHubRateLimitError) as secondary_403_error:
        secondary_403_client.execute("query", {})
    assert not secondary_403_error.value.primary

    reset_only_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {
                "errors": [
                    {
                        "type": "RATE_LIMITED",
                        "message": "secondary rate limit",
                    }
                ]
            },
            {"x-ratelimit-reset": "4000000000"},
        ),
    )
    with pytest.raises(GitHubRateLimitError) as reset_only_error:
        reset_only_client.execute("query", {})
    assert not reset_only_error.value.primary

    fallback_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"errors": [{"type": "RATE_LIMITED"}]}),
    )
    with pytest.raises(GitHubRateLimitError) as fallback_error:
        fallback_client.execute("query", {})
    assert not fallback_error.value.primary

    exhausted_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {"data": {"ok": True}},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4000000000"},
        ),
    )
    assert exhausted_client.execute("query", {}) == {"ok": True}
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        exhausted_client.execute("query", {})

    invalid_header_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {"data": {"ok": True}},
            {"x-ratelimit-remaining": "unknown"},
        ),
    )
    assert invalid_header_client.execute("query", {}) == {"ok": True}

    class HeaderBag:
        def items(self) -> list[tuple[str, str]]:
            return [("X-RateLimit-Remaining", "10")]

    object_header_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"data": {"ok": True}}, HeaderBag()),
    )
    assert object_header_client.execute("query", {}) == {"ok": True}


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


def test_provider_preserves_archive_evidence() -> None:
    metadata = _dashboard_metadata(
        {
            "url": "https://example.test/issues/1",
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": 1,
                        "url": "https://example.test/pull/1",
                        "state": "MERGED",
                        "merged": True,
                        "headRefName": "codex/done",
                        "headRef": None,
                    },
                    {
                        "number": 2,
                        "headRefName": "codex/live",
                        "headRef": {"name": "codex/live"},
                    },
                    {
                        "number": 3,
                        "headRefName": "codex/malformed",
                        "headRef": "not-an-object",
                    },
                    {"number": 4, "headRefName": "codex/unknown"},
                ]
            },
        }
    )

    pull_requests = metadata["pull_requests"]
    assert metadata["source_url"] == "https://example.test/issues/1"
    assert [pull_request["source_branch_state"] for pull_request in pull_requests] == [
        "deleted",
        "present",
        "unknown",
        "unknown",
    ]  # type: ignore[index]
    assert pull_requests[0]["source_branch"] == "codex/done"  # type: ignore[index]


class RateLimitedAfterDiscoveryClient(StaticClient):
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if len(self.calls) >= 3:
            raise GitHubRateLimitError("limited")
        return super().execute(query, variables)


class AlwaysRateLimitedClient:
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        raise GitHubRateLimitError("limited")


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


class ErrorHandoffClient:
    def __init__(
        self,
        *,
        bad_base: bool = False,
        bad_metadata: bool = False,
        bad_pull_request: bool = False,
        bad_ref: bool = False,
        bad_ref_name: bool = False,
    ) -> None:
        self.bad_base = bad_base
        self.bad_metadata = bad_metadata
        self.bad_pull_request = bad_pull_request
        self.bad_ref = bad_ref
        self.bad_ref_name = bad_ref_name

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "baseRef:" in query:
            name = "wrong" if self.bad_base else "release"
            return {"repository": {"baseRef": {"name": name, "target": {}}}}
        if "CreatePullRequestInput" in query and self.bad_pull_request:
            return {"createPullRequest": {"pullRequest": {}}}
        if "CreateRefInput" in query and self.bad_ref:
            return {"createRef": {"ref": None}}
        if "CreateRefInput" in query and self.bad_ref_name:
            return {"createRef": {"ref": {"name": "wrong"}}}
        if "CreateRefInput" in query:
            return {"createRef": {"ref": {"name": variables["input"]["name"]}}}
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {} if self.bad_metadata else {"oid": "oid"},
                },
                "ref": None
                if self.bad_ref or self.bad_ref_name
                else {"name": variables["qualifiedBranch"]},
                "pullRequests": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }


def test_provider_rejects_invalid_handoff_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=ErrorHandoffClient(bad_base=True)
    )
    with pytest.raises(ProviderError, match="does not contain base branch"):
        provider.create_handoff(_handoff_request(base_branch="release"))

    missing_metadata = GitHubProjectProvider(
        "owner", 7, "token", client=ErrorHandoffClient(bad_metadata=True)
    )
    with pytest.raises(ProviderError, match="branch creation metadata"):
        missing_metadata.create_handoff(_handoff_request())

    missing_ref = GitHubProjectProvider(
        "owner", 7, "token", client=ErrorHandoffClient(bad_ref=True)
    )
    with pytest.raises(ProviderError, match="invalid object"):
        missing_ref.create_handoff(_handoff_request())

    wrong_ref = GitHubProjectProvider(
        "owner", 7, "token", client=ErrorHandoffClient(bad_ref_name=True)
    )
    with pytest.raises(ProviderError, match="did not confirm branch creation"):
        wrong_ref.create_handoff(_handoff_request())

    malformed = GitHubProjectProvider(
        "owner", 7, "token", client=ErrorHandoffClient(bad_pull_request=True)
    )
    with pytest.raises(ProviderError, match="pull-request record"):
        malformed.create_handoff(_handoff_request())

    with pytest.raises(ProviderError, match="owner/name"):
        malformed.create_handoff(
            HandoffRequest("owner:7", "invalid", 1, "bad", "branch", None, "", "run-1")
        )
    with pytest.raises(ProviderError, match="branch name"):
        malformed.validate_handoff("owner/api", "bad branch", None)


class RaceClient(ErrorHandoffClient):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.repository_calls = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequests" in query:
            self.repository_calls += 1
            if self.repository_calls > 1 and self.mode in {
                "ref-query-error",
                "pr-query-error",
            }:
                raise ProviderError("recheck failed")
            ref = None if self.mode.startswith("ref-") else {"name": "branch"}
            return {
                "repository": {
                    "id": "repo-id",
                    "defaultBranchRef": {
                        "name": "main",
                        "target": {"oid": "oid"},
                    },
                    "ref": ref,
                    "pullRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        if "CreateRefInput" in query:
            raise ProviderError("reference already exists")
        if "CreatePullRequestInput" in query:
            raise ProviderError("pull request already exists")
        return super().execute(query, variables)


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


class StubProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(project_id, "Planning", ())

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "main"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)


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
    assert factory_calls == 1


class StorageProvider(StubProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return self.snapshot


def _storage_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "one"),)),),
    )


def test_storage_rejects_invalid_state_operations() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    with pytest.raises(StoreError, match="worker owner"):
        store.claim_next("project-1", "owner/api", " ")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    with pytest.raises(StoreError, match="Unknown run"):
        store.advance("missing", Stage.IMPLEMENT, lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.record_handoff("missing", "branch", "url", None, lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff("missing", "branch", "main", "body", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.fail("missing", "error", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.pending_handoff("missing", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.renew_lease("missing", lease_token)
    assert store.get_run("missing") is None

    renewed = service.renew_lease(run.run_id, lease_token)
    assert renewed.run_id == run.run_id
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )
    with pytest.raises(StoreError, match="expired"):
        store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        (renewed.lease_expires_at, run.run_id),
    )

    advanced = store.advance(run.run_id, Stage.REFINE, lease_token)
    assert advanced.stage is Stage.REFINE
    with pytest.raises(StoreError, match="Cannot advance"):
        store.advance(run.run_id, Stage.PULL_REQUEST, lease_token)
    with pytest.raises(StoreError, match="failure reason"):
        store.fail(run.run_id, "", lease_token)
    failed = store.fail(run.run_id, "failed", lease_token)
    assert failed.status is RunStatus.FAILED
    assert store.fail(run.run_id, "again", lease_token) == failed
    assert store.advance(run.run_id, Stage.REFINE, lease_token) == failed
    with pytest.raises(StoreError, match="not active"):
        store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    with pytest.raises(StoreError, match="branch and pull-request"):
        store.record_handoff(run.run_id, "", "url", None, lease_token)
    with pytest.raises(StoreError, match="active implementation"):
        store.record_handoff(run.run_id, "branch", "url", None, lease_token)
    with pytest.raises(StoreError, match="active implementation"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_storage_handoff_intent_edge_cases() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    run = service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    with pytest.raises(StoreError, match="branch is required"):
        store.prepare_handoff(run.run_id, "", "main", "body", lease_token)
    with pytest.raises(StoreError, match="persisted intent"):
        store.record_handoff(run.run_id, "branch", "url", None, lease_token)
    intent = store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    with pytest.raises(StoreError, match="persisted intent"):
        store.prepare_handoff(run.run_id, "other", "main", "body", lease_token)
    completed = store.record_handoff(run.run_id, "branch", "url", 1, lease_token)
    assert (
        store.record_handoff(run.run_id, "branch", "url", 1, lease_token) == completed
    )
    finished_intent = store.prepare_handoff(
        run.run_id, "branch", "main", "body", lease_token
    )
    assert finished_intent.run.status is RunStatus.COMPLETED
    assert intent.branch == finished_intent.branch
    with pytest.raises(StoreError, match="completed"):
        store.fail(run.run_id, "late failure", lease_token)
    store._connection.execute(
        """
        UPDATE pbis SET stage = 'implement', handoff_status = 'completed'
        WHERE project_id = ? AND repository_name = ? AND number = ?
        """,
        (run.project_id, run.repository, run.pbi_number),
    )
    store._connection.execute(
        "UPDATE runs SET status = 'active' WHERE run_id = ?", (run.run_id,)
    )
    with pytest.raises(StoreError, match="already completed"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_storage_reconciles_an_active_removed_pbi() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    store.sync_project(ProjectSnapshot("project-1", "Planning", ()))
    assert store.get_run(run.run_id) is not None
    assert store.get_run(run.run_id).status is RunStatus.FAILED  # type: ignore[union-attr]
    store.close()


def test_storage_external_sync_records_existing_run_id() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None

    store.sync_project(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot(
                            "owner/api", 1, "one", Stage.IMPLEMENT, "In Progress"
                        ),
                    ),
                ),
            ),
        )
    )

    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]
    assert pbi["events"][-1]["run_id"] == run.run_id  # type: ignore[index]
    store.close()


def test_storage_rejects_mismatched_pbi_repository() -> None:
    store = OrchestratorStore()
    with pytest.raises(StoreError, match="does not match"):
        store.sync_project(
            ProjectSnapshot(
                "project-1",
                "Planning",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/web", 1, "bad"),)
                    ),
                ),
            )
        )
    store.close()


def test_storage_defensive_handoff_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    run = service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    orphan = RunState(
        "orphan",
        "missing-project",
        "owner/api",
        1,
        "orphan",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token=lease_token,
        lease_expires_at=run.lease_expires_at,
    )
    monkeypatch.setattr(store, "_run_for_id", lambda connection, run_id: orphan)
    with pytest.raises(StoreError, match="Unknown PBI"):
        store.prepare_handoff("orphan", "branch", "main", "body", lease_token)
    monkeypatch.undo()

    store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    pending_calls = 0

    def return_pending_once(connection: object, run_id: str) -> RunState | None:
        nonlocal pending_calls
        pending_calls += 1
        return run if pending_calls == 1 else None

    monkeypatch.setattr(store, "_run_for_id", return_pending_once)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store._connection.execute(
        """
        UPDATE pbis
        SET branch = NULL, handoff_base_branch = NULL, handoff_body = NULL,
            handoff_status = 'none'
        WHERE project_id = ? AND repository_name = ? AND number = ?
        """,
        (run.project_id, run.repository, run.pbi_number),
    )

    calls = 0

    def return_once(connection: object, run_id: str) -> RunState | None:
        nonlocal calls
        calls += 1
        return run if calls == 1 else None

    monkeypatch.setattr(store, "_run_for_id", return_once)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_orchestrator_handoff_rejects_unknown_and_handles_completed_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    with pytest.raises(StoreError, match="Unknown run"):
        service.handoff("missing", "branch", None, "body", "missing-token")

    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    completed = RunState(
        run.run_id,
        run.project_id,
        run.repository,
        run.pbi_number,
        run.title,
        Stage.PULL_REQUEST,
        RunStatus.COMPLETED,
        run.attempt,
        "branch",
        "https://example.test/pull/1",
        owner_id=run.owner_id,
        lease_token=run.lease_token,
        lease_expires_at=run.lease_expires_at,
    )
    monkeypatch.setattr(
        store,
        "prepare_handoff",
        lambda *args: HandoffIntent(completed, "branch", "main", "body"),
    )
    assert service.handoff(run.run_id, "branch", None, "body", lease_token) == completed
    store.close()


def test_storage_migrates_legacy_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE projects(project_id TEXT PRIMARY KEY, name TEXT, updated_at TEXT);
        CREATE TABLE repositories(
            project_id TEXT, name TEXT, PRIMARY KEY(project_id, name)
        );
        CREATE TABLE pbis(
            project_id TEXT, repository_name TEXT, number INTEGER, title TEXT,
            stage TEXT, run_id TEXT, branch TEXT, pull_request_url TEXT,
            last_error TEXT, PRIMARY KEY(project_id, repository_name, number)
        );
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, status TEXT, attempt INTEGER, last_error TEXT,
            updated_at TEXT
        );
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, run_id TEXT, event_type TEXT, from_stage TEXT,
            to_stage TEXT, details_json TEXT, created_at TEXT
        );
        CREATE TABLE handoffs(
            run_id TEXT PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, branch TEXT, pull_request_url TEXT,
            pull_request_number INTEGER, created_at TEXT
        );
        """
    )
    connection.close()

    store = OrchestratorStore(database)
    columns = {
        str(row[1]) for row in store._connection.execute("PRAGMA table_info(pbis)")
    }
    assert {
        "active",
        "handoff_base_branch",
        "handoff_body",
        "handoff_status",
        "planning_status",
        "claimable",
        "archived",
    } <= columns
    run_columns = {
        str(row[1]) for row in store._connection.execute("PRAGMA table_info(runs)")
    }
    assert {"owner_id", "lease_token", "lease_expires_at"} <= run_columns
    store.close()
