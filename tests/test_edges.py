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
    ProviderError,
    UrllibGraphQLClient,
    _mapping,
    _next_cursor,
    _nodes,
    _stage_from_status,
)
from beehaiive.storage import OrchestratorStore, StoreError


class StaticClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.calls.append(query)
        return self.data


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

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


def test_provider_helpers_and_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ProviderError, match="invalid object"):
        _mapping(None)
    with pytest.raises(ProviderError, match="invalid nodes"):
        _nodes({"nodes": "bad"})
    with pytest.raises(ProviderError, match="end cursor"):
        _next_cursor({"pageInfo": {"hasNextPage": True}})
    assert _stage_from_status("Todo") is Stage.REFINE
    assert _stage_from_status("In Progress") is Stage.IMPLEMENT
    assert _stage_from_status("Done") is Stage.PULL_REQUEST
    assert _stage_from_status("unknown") is Stage.BACKLOG

    for variable in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PROJECT_OWNER",
        "GITHUB_PROJECT_NUMBER",
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

    branch_client = StaticClient({"repository": {"defaultBranchRef": {"name": "main"}}})
    branch_provider = GitHubProjectProvider("owner", 7, "token", client=branch_client)
    assert branch_provider.resolve_base_branch("owner/api", None) == "main"
    assert branch_provider.resolve_base_branch("owner/api", "release") == "release"
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
            HandoffRequest("owner:7", "invalid", 1, "bad", "branch", None, "")
        )


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


def test_environment_provider_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubProvider()
    monkeypatch.setattr(
        provider_module.GitHubProjectProvider,
        "from_environment",
        classmethod(lambda cls: stub),
    )
    provider = provider_module.EnvironmentGitHubProvider()
    request = _handoff_request()
    assert provider.discover_project("owner:7").project_id == "owner:7"
    assert provider.create_handoff(request).pull_request_number == 1
    assert provider.resolve_base_branch("owner/api", None) == "main"


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
    run = service.claim("project-1", "owner/api")
    assert run is not None

    with pytest.raises(StoreError, match="Unknown run"):
        store.advance("missing", Stage.IMPLEMENT)
    with pytest.raises(StoreError, match="Unknown run"):
        store.record_handoff("missing", "branch", "url", None)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff("missing", "branch", "main", "body")
    with pytest.raises(StoreError, match="Unknown run"):
        store.fail("missing", "error")
    assert store.pending_handoff("missing") is None
    assert store.get_run("missing") is None

    assert store.advance(run.run_id, Stage.REFINE) == run
    with pytest.raises(StoreError, match="Cannot advance"):
        store.advance(run.run_id, Stage.PULL_REQUEST)
    with pytest.raises(StoreError, match="failure reason"):
        store.fail(run.run_id, "")
    failed = store.fail(run.run_id, "failed")
    assert failed.status is RunStatus.FAILED
    assert store.fail(run.run_id, "again") == failed
    assert store.advance(run.run_id, Stage.REFINE) == failed
    with pytest.raises(StoreError, match="not active"):
        store.advance(run.run_id, Stage.IMPLEMENT)
    with pytest.raises(StoreError, match="branch and pull-request"):
        store.record_handoff(run.run_id, "", "url", None)
    with pytest.raises(StoreError, match="active implementation"):
        store.record_handoff(run.run_id, "branch", "url", None)
    with pytest.raises(StoreError, match="active implementation"):
        store.prepare_handoff(run.run_id, "branch", "main", "body")
    store.close()


def test_storage_handoff_intent_edge_cases() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api")
    assert run is not None
    run = service.advance(run.run_id, Stage.IMPLEMENT)

    with pytest.raises(StoreError, match="branch is required"):
        store.prepare_handoff(run.run_id, "", "main", "body")
    with pytest.raises(StoreError, match="persisted intent"):
        store.record_handoff(run.run_id, "branch", "url", None)
    intent = store.prepare_handoff(run.run_id, "branch", "main", "body")
    with pytest.raises(StoreError, match="persisted intent"):
        store.prepare_handoff(run.run_id, "other", "main", "body")
    completed = store.record_handoff(run.run_id, "branch", "url", 1)
    assert store.record_handoff(run.run_id, "branch", "url", 1) == completed
    finished_intent = store.prepare_handoff(run.run_id, "branch", "main", "body")
    assert finished_intent.run.status is RunStatus.COMPLETED
    assert intent.branch == finished_intent.branch
    with pytest.raises(StoreError, match="completed"):
        store.fail(run.run_id, "late failure")
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
        store.prepare_handoff(run.run_id, "branch", "main", "body")
    store.close()


def test_storage_reconciles_an_active_removed_pbi() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api")
    assert run is not None
    store.sync_project(ProjectSnapshot("project-1", "Planning", ()))
    assert store.get_run(run.run_id) is not None
    assert store.get_run(run.run_id).status is RunStatus.FAILED  # type: ignore[union-attr]
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
    run = service.claim("project-1", "owner/api")
    assert run is not None
    run = service.advance(run.run_id, Stage.IMPLEMENT)

    orphan = RunState(
        "orphan",
        "missing-project",
        "owner/api",
        1,
        "orphan",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
    )
    monkeypatch.setattr(store, "_run_for_id", lambda connection, run_id: orphan)
    with pytest.raises(StoreError, match="Unknown PBI"):
        store.prepare_handoff("orphan", "branch", "main", "body")

    calls = 0

    def return_once(connection: object, run_id: str) -> RunState | None:
        nonlocal calls
        calls += 1
        return run if calls == 1 else None

    monkeypatch.setattr(store, "_run_for_id", return_once)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff(run.run_id, "branch", "main", "body")
    store.close()


def test_orchestrator_handoff_rejects_unknown_and_handles_completed_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    with pytest.raises(StoreError, match="Unknown run"):
        service.handoff("missing", "branch", None, "body")

    run = service.claim("project-1", "owner/api")
    assert run is not None
    service.advance(run.run_id, Stage.IMPLEMENT)
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
    )
    monkeypatch.setattr(
        store,
        "prepare_handoff",
        lambda *args: HandoffIntent(completed, "branch", "main", "body"),
    )
    assert service.handoff(run.run_id, "branch", None, "body") == completed
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
    } <= columns
    store.close()
