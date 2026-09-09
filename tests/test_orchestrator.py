import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    GraphQLClient,
    ProviderError,
    _handoff_marker,
)
from beehaiive.storage import OrchestratorStore, StoreError


class FakeProvider:
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot
        self.handoffs: list[HandoffRequest] = []

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        assert project_id == self.snapshot.project_id
        return self.snapshot

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.handoffs.append(request)
        return HandoffResult(
            branch=request.branch,
            pull_request_url=f"https://example.test/{request.repository}/pull/1",
            pull_request_number=1,
        )

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)


class InvalidHandoffProvider(FakeProvider):
    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        raise ProviderError("invalid handoff metadata")


class ConcurrentHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.started = Event()
        self.release = Event()

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        return super().create_handoff(request)


class DisposableRepositoryProvider(FakeProvider):
    def __init__(self, repository_path: Path, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.repository_path = repository_path
        self.pull_request_records: list[dict[str, str]] = []

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        subprocess.run(
            ["git", "branch", request.branch],
            cwd=self.repository_path,
            check=True,
            capture_output=True,
            text=True,
        )
        pull_request_url = "https://example.test/owner/api/pull/1"
        self.pull_request_records.append(
            {"branch": request.branch, "url": pull_request_url}
        )
        return HandoffResult(request.branch, pull_request_url, 1)


class CrashAfterExternalHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.external_artifacts: dict[str, HandoffResult] = {}
        self.fail_once = True
        self.resolved_bases: list[str] = []

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        resolved = requested or ("main" if not self.resolved_bases else "develop")
        self.resolved_bases.append(resolved)
        return resolved

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.handoffs.append(request)
        result = self.external_artifacts.setdefault(
            request.branch,
            HandoffResult(
                request.branch,
                f"https://example.test/{request.repository}/pull/1",
                1,
            ),
        )
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("crashed after external handoff")
        return result


class SharedIdempotentHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.external_artifacts: dict[str, HandoffResult] = {}
        self._external_lock = Lock()
        self.first_started = Event()
        self.second_started = Event()
        self.release_first = Event()

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        with self._external_lock:
            first_call = not self.handoffs
            self.handoffs.append(request)
            result = self.external_artifacts.setdefault(
                request.branch,
                HandoffResult(
                    request.branch,
                    f"https://example.test/{request.repository}/pull/1",
                    1,
                ),
            )
            if first_call:
                self.first_started.set()
            else:
                self.second_started.set()
        if first_call:
            assert self.release_first.wait(timeout=2)
        return result


def snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id="project-1",
        name="Planning",
        repositories=(
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot("owner/api", 1, "API one"),
                    PbiSnapshot("owner/api", 2, "API two"),
                ),
            ),
            RepositorySnapshot(
                "owner/web",
                (PbiSnapshot("owner/web", 3, "Web one"),),
            ),
        ),
    )


def test_repositories_have_independent_writer_claims() -> None:
    provider = FakeProvider(snapshot())
    store = OrchestratorStore()
    service = Orchestrator(store, provider)

    state = service.synchronize("project-1")
    assert [repo["name"] for repo in state["repositories"]] == [
        "owner/api",
        "owner/web",
    ]

    api_run = service.claim("project-1", "owner/api", "worker-1")
    assert api_run is not None
    assert api_run.stage is Stage.REFINE
    renewed = service.claim("project-1", "owner/api", "worker-1", api_run.lease_token)
    assert renewed is not None
    assert renewed.run_id == api_run.run_id
    assert renewed.lease_token == api_run.lease_token

    web_run = service.claim("project-1", "owner/web", "worker-1")
    assert web_run is not None
    assert web_run.run_id != api_run.run_id

    api_state = service.store.project_state("project-1")["repositories"][0]
    assert api_state["writer"]["run_id"] == api_run.run_id  # type: ignore[index]


def test_durable_lease_blocks_duplicate_workers_and_fences_takeover(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lease.sqlite3"
    provider = FakeProvider(snapshot())
    first_store = OrchestratorStore(database)
    first_service = Orchestrator(first_store, provider)
    first_service.synchronize("project-1")
    first_run = first_service.claim("project-1", "owner/api", "worker-1")
    assert first_run is not None
    first_token = first_run.lease_token or ""

    second_store = OrchestratorStore(database)
    second_service = Orchestrator(second_store, provider)
    assert second_service.claim("project-1", "owner/api", "worker-2") is None

    first_store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", first_run.run_id),
    )
    reclaimed = second_service.claim("project-1", "owner/api", "worker-2")

    assert reclaimed is not None
    assert reclaimed.owner_id == "worker-2"
    assert reclaimed.lease_token != first_token
    with pytest.raises(StoreError, match="lease token"):
        first_service.advance(first_run.run_id, Stage.IMPLEMENT, first_token)

    advanced = second_service.advance(
        reclaimed.run_id, Stage.IMPLEMENT, reclaimed.lease_token or ""
    )
    assert advanced.stage is Stage.IMPLEMENT
    first_store.close()
    second_store.close()


def test_github_api_double_drives_two_repository_queues() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=FakeGraphQLClient(
            {
                "user": {
                    "projectV2": {
                        "title": "Planning",
                        "repositories": {
                            "nodes": [
                                {"nameWithOwner": "owner/api"},
                                {"nameWithOwner": "owner/web"},
                            ]
                        },
                        "items": {
                            "nodes": [
                                {
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 1,
                                        "title": "API one",
                                        "repository": {"nameWithOwner": "owner/api"},
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Backlog",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                },
                                {
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 2,
                                        "title": "API two",
                                        "repository": {"nameWithOwner": "owner/api"},
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Backlog",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                },
                                {
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 3,
                                        "title": "Web one",
                                        "repository": {"nameWithOwner": "owner/web"},
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Backlog",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                }
            }
        ),
    )
    service = Orchestrator(OrchestratorStore(), provider)

    service.synchronize("owner:7")
    api_run = service.claim("owner:7", "owner/api", "worker-1")
    web_run = service.claim("owner:7", "owner/web", "worker-1")

    assert api_run is not None
    assert web_run is not None
    assert api_run.run_id != web_run.run_id
    renewed = service.claim("owner:7", "owner/api", "worker-1", api_run.lease_token)
    assert renewed is not None
    assert renewed.run_id == api_run.run_id
    assert renewed.lease_token == api_run.lease_token


def test_failure_releases_only_the_failed_repository_writer() -> None:
    provider = FakeProvider(snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    api_run = service.claim("project-1", "owner/api", "worker-1")
    web_run = service.claim("project-1", "owner/web", "worker-1")
    assert api_run is not None and web_run is not None

    api_token = api_run.lease_token or ""
    failed = service.fail(api_run.run_id, "provider unavailable", api_token)
    assert failed.status.value == "failed"
    renewed = service.claim("project-1", "owner/web", "worker-1", web_run.lease_token)
    assert renewed is not None
    assert renewed.run_id == web_run.run_id
    assert renewed.lease_token == web_run.lease_token
    retried = service.claim("project-1", "owner/api", "worker-1")
    assert retried is not None
    assert retried.run_id == api_run.run_id
    assert retried.attempt == 2


def test_project_state_limits_event_history() -> None:
    provider = FakeProvider(snapshot())
    store = OrchestratorStore()
    service = Orchestrator(store, provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    state = store.project_state("project-1", event_limit=1)
    events = state["repositories"][0]["pbis"][0]["events"]  # type: ignore[index]

    assert state["event_limit"] == 1
    assert len(events) == 1  # type: ignore[arg-type]
    assert events[0]["to_stage"] == Stage.IMPLEMENT.value  # type: ignore[index]
    with pytest.raises(StoreError, match="event_limit"):
        store.project_state("project-1", event_limit=0)
    store.close()


def test_sync_reconciles_removed_repositories_without_deleting_history() -> None:
    provider = FakeProvider(snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")

    service.store.sync_project(
        ProjectSnapshot(
            project_id="project-1",
            name="Planning",
            repositories=(snapshot().repositories[0],),
        )
    )

    state = service.store.project_state("project-1")
    removed = next(
        repository
        for repository in state["repositories"]
        if repository["name"] == "owner/web"
    )
    assert removed["active"] is False
    assert removed["pbis"][0]["title"] == "Web one"  # type: ignore[index]
    assert service.claim("project-1", "owner/web", "worker-1") is None


def test_sync_keeps_external_done_from_completing_a_local_run() -> None:
    provider = FakeProvider(snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    provider.snapshot = ProjectSnapshot(
        project_id="project-1",
        name="Planning",
        repositories=(
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "API one",
                        None,
                        "Done",
                        False,
                    ),
                ),
            ),
        ),
    )
    service.synchronize("project-1")

    api_repository = service.store.project_state("project-1")["repositories"][0]
    pbi_rows = {pbi["number"]: pbi for pbi in api_repository["pbis"]}  # type: ignore[index]
    assert pbi_rows[1]["stage"] == Stage.IMPLEMENT.value
    assert pbi_rows[1]["status"] == "active"
    assert pbi_rows[1]["active"] is True
    assert pbi_rows[1]["planning_status"] == "Done"
    assert pbi_rows[1]["claimable"] is False
    assert service.claim("project-1", "owner/api", "worker-1", lease_token) is not None

    web_repository = next(
        repository
        for repository in service.store.project_state("project-1")["repositories"]
        if repository["name"] == "owner/web"
    )
    assert web_repository["active"] is False


def test_invalid_handoff_does_not_call_provider() -> None:
    provider = FakeProvider(snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    with pytest.raises(StoreError, match="Only an active implementation run"):
        service.handoff(run.run_id, "codex/refine", "master", "Closes #1", lease_token)

    assert provider.handoffs == []


def test_invalid_handoff_metadata_is_not_persisted() -> None:
    provider = InvalidHandoffProvider(snapshot())
    store = OrchestratorStore()
    service = Orchestrator(store, provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    with pytest.raises(ProviderError, match="invalid handoff metadata"):
        service.handoff(run.run_id, "codex/api-1", "master", "Closes #1", token)

    assert store.pending_handoff(run.run_id, token) is None
    store.close()


def test_handoff_intent_survives_external_crash_and_reuses_artifact(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    provider = CrashAfterExternalHandoffProvider(snapshot())
    first_store = OrchestratorStore(database)
    first_service = Orchestrator(first_store, provider)
    first_service.synchronize("project-1")
    run = first_service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    first_service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    with pytest.raises(RuntimeError, match="crashed after external handoff"):
        first_service.handoff(run.run_id, "codex/api-1", None, "Closes #1", lease_token)
    first_store.close()

    second_store = OrchestratorStore(database)
    second_service = Orchestrator(second_store, provider)
    resumed = second_service.claim("project-1", "owner/api", "worker-1", lease_token)
    assert resumed is not None
    assert resumed.stage is Stage.IMPLEMENT
    assert resumed.branch == "codex/api-1"

    with pytest.raises(StoreError, match="does not match the persisted intent"):
        second_service.handoff(
            run.run_id, "codex/api-2", None, "Closes #1", lease_token
        )
    completed = second_service.handoff(
        run.run_id,
        "codex/api-1",
        None,
        "Closes #1",
        lease_token,
    )

    assert completed.status.value == "completed"
    assert len(provider.external_artifacts) == 1
    assert len(provider.handoffs) == 2
    assert provider.resolved_bases == ["main"]
    assert all(handoff.base_branch == "main" for handoff in provider.handoffs)
    second_store.close()


def test_two_service_instances_share_idempotent_handoff_artifact(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    provider = SharedIdempotentHandoffProvider(snapshot())
    first_store = OrchestratorStore(database)
    first_service = Orchestrator(first_store, provider)
    first_service.synchronize("project-1")
    run = first_service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    first_service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    second_store = OrchestratorStore(database)
    second_service = Orchestrator(second_store, provider)
    resumed = second_service.claim("project-1", "owner/api", "worker-1", lease_token)
    assert resumed is not None
    assert resumed.run_id == run.run_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first_service.handoff,
            run.run_id,
            "codex/api-1",
            None,
            "Closes #1",
            lease_token,
        )
        assert provider.first_started.wait(timeout=2)
        second_future = executor.submit(
            second_service.handoff,
            run.run_id,
            "codex/api-1",
            None,
            "Closes #1",
            lease_token,
        )
        assert provider.second_started.wait(timeout=2)
        provider.release_first.set()
        results = [first_future.result(), second_future.result()]

    assert all(result.status.value == "completed" for result in results)
    assert len(provider.external_artifacts) == 1
    assert len(provider.handoffs) == 2
    first_store.close()
    second_store.close()


def test_concurrent_handoffs_create_one_external_artifact() -> None:
    provider = ConcurrentHandoffProvider(snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.handoff,
            run.run_id,
            "codex/api-1",
            "master",
            "Closes #1",
            lease_token,
        )
        assert provider.started.wait(timeout=2)
        second = executor.submit(
            service.handoff,
            run.run_id,
            "codex/api-1",
            "master",
            "Closes #1",
            lease_token,
        )
        provider.release.set()
    assert first.result().status.value == "completed"
    assert second.result().status.value == "completed"

    assert len(provider.handoffs) == 1
    assert service._handoff_locks._entries == {}


def test_handoff_records_branch_and_pull_request_in_disposable_repository(
    tmp_path: Path,
) -> None:
    repository_path = tmp_path / "disposable-repository"
    repository_path.mkdir()
    subprocess.run(
        ["git", "init", str(repository_path)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "config",
            "user.email",
            "test@example.test",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    (repository_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    provider = DisposableRepositoryProvider(repository_path, snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    completed = service.handoff(
        run.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        lease_token,
    )
    branches = subprocess.run(
        ["git", "-C", str(repository_path), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert completed.branch == "codex/api-1"
    assert "codex/api-1" in branches
    assert provider.pull_request_records == [
        {
            "branch": "codex/api-1",
            "url": "https://example.test/owner/api/pull/1",
        }
    ]


def test_restart_resumes_run_and_records_idempotent_handoff(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    provider = FakeProvider(snapshot())
    first_store = OrchestratorStore(database)
    first_service = Orchestrator(first_store, provider)
    first_service.synchronize("project-1")
    run = first_service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    first_store.close()

    second_store = OrchestratorStore(database)
    second_service = Orchestrator(second_store, provider)
    resumed = second_service.claim("project-1", "owner/api", "worker-1", lease_token)
    assert resumed is not None
    assert resumed.run_id == run.run_id
    assert resumed.lease_token == run.lease_token
    second_service.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    second_store.close()

    third_store = OrchestratorStore(database)
    third_service = Orchestrator(third_store, provider)
    resumed_again = third_service.claim(
        "project-1", "owner/api", "worker-1", lease_token
    )
    assert resumed_again is not None
    assert resumed_again.run_id == run.run_id
    assert resumed_again.stage is Stage.IMPLEMENT
    with pytest.raises(StoreError, match="Cannot advance"):
        third_service.advance(run.run_id, Stage.PULL_REQUEST, lease_token)

    completed = third_service.handoff(
        run.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        lease_token,
    )
    repeated = third_service.handoff(
        run.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        lease_token,
    )
    assert completed == repeated
    assert completed.stage is Stage.PULL_REQUEST
    assert completed.status.value == "completed"
    assert len(provider.handoffs) == 1

    events = third_service.store.project_state("project-1")["repositories"][0]["pbis"][
        0
    ]["events"]  # type: ignore[index]
    assert [(event["from_stage"], event["to_stage"]) for event in events] == [  # type: ignore[index]
        ("backlog", "refine"),
        ("refine", "implement"),
        ("implement", "pull_request"),
    ]


class FakeGraphQLClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        return self.data


class PaginatedGraphQLClient:
    def __init__(self) -> None:
        self.repositories = [
            {"nameWithOwner": f"owner/repo-{index:03d}"} for index in range(101)
        ]
        self.items = [
            {
                "content": {
                    "__typename": "Issue",
                    "number": index,
                    "title": f"PBI {index}",
                    "repository": {"nameWithOwner": "owner/repo-000"},
                },
                "fieldValues": {
                    "nodes": [
                        {},
                        {"name": "Backlog", "field": {"name": "Status"}},
                    ]
                },
            }
            for index in (1, 2)
        ]

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        cursor = variables.get("cursor")
        if "repositories" in query:
            nodes = (
                self.repositories[:100] if cursor is None else self.repositories[100:]
            )
            return {
                "user": {
                    "projectV2": {
                        "repositories": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "repos-1" if cursor is None else None,
                            },
                        }
                    }
                }
            }
        if "items" in query:
            nodes = self.items[:1] if cursor is None else self.items[1:]
            return {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "items-1" if cursor is None else None,
                            },
                        }
                    }
                }
            }
        return {"user": {"projectV2": {"title": "Paged Planning"}}}


class HandoffGraphQLClient:
    def __init__(self) -> None:
        self.ref_exists = False
        self.pull_requests: list[dict[str, object]] = []
        self.ref_creations = 0
        self.ref_oids: list[object] = []
        self.pull_request_creations = 0
        self.pull_request_bases: list[object] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "baseRef:" in query:
            return {
                "repository": {
                    "baseRef": {
                        "name": "release",
                        "target": {"oid": "release-oid"},
                    }
                }
            }
        if "CreateRefInput" in query:
            self.ref_exists = True
            self.ref_creations += 1
            self.ref_oids.append(variables["input"]["oid"])
            return {"createRef": {"ref": {"name": variables["input"]["name"]}}}
        if "CreatePullRequestInput" in query:
            self.pull_request_creations += 1
            self.pull_request_bases.append(variables["input"]["baseRefName"])
            pull_request = {
                "number": 8,
                "url": "https://example.test/owner/api/pull/8",
                "headRefName": variables["input"]["headRefName"],
                "baseRefName": variables["input"]["baseRefName"],
                "body": variables["input"]["body"],
            }
            self.pull_requests.append(pull_request)
            return {"createPullRequest": {"pullRequest": pull_request}}
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": "base-oid"},
                },
                "ref": {"name": variables["qualifiedBranch"]}
                if self.ref_exists
                else None,
                "pullRequests": {
                    "nodes": self.pull_requests,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }


def test_github_provider_discovers_linked_repositories_without_items() -> None:
    client: GraphQLClient = FakeGraphQLClient(
        {
            "user": {
                "projectV2": {
                    "title": "Planning",
                    "repositories": {
                        "nodes": [
                            {"nameWithOwner": "owner/api"},
                            {"nameWithOwner": "owner/web"},
                        ]
                    },
                    "items": {
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 1,
                                    "title": "API one",
                                    "repository": {"nameWithOwner": "owner/api"},
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {},
                                        {},
                                        {
                                            "name": "Backlog",
                                            "field": {"name": "Status"},
                                        },
                                    ]
                                },
                            }
                        ]
                    },
                }
            }
        }
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")

    assert [repo.name for repo in discovered.repositories] == ["owner/api", "owner/web"]
    assert discovered.repositories[0].pbis[0].stage is Stage.BACKLOG


def test_github_provider_paginates_repositories_and_items() -> None:
    client = PaginatedGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")

    assert len(discovered.repositories) == 101
    assert [pbi.number for pbi in discovered.repositories[0].pbis] == [1, 2]


def test_github_provider_reuses_existing_branch_and_pull_request() -> None:
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
    )

    first = provider.create_handoff(request)
    second = provider.create_handoff(request)

    assert first == second
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1
    assert client.pull_request_bases == ["main"]


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
    second_request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=2,
        title="API two",
        branch="codex/api-1",
        base_branch=None,
        body="Closes #2",
        run_id="run-2",
    )

    first = provider.create_handoff(first_request)
    second = provider.create_handoff(second_request)

    assert first.pull_request_number == 8
    assert second.pull_request_number == 8
    assert client.pull_request_creations == 2


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


class PaginatedPullRequestClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.ref_exists = True
        self.pull_requests = [
            {
                "number": index,
                "url": f"https://example.test/owner/api/pull/{index}",
                "headRefName": f"old-{index}",
                "baseRefName": "main",
            }
            for index in range(100)
        ]
        self.pull_requests.append(
            {
                "number": 101,
                "url": "https://example.test/owner/api/pull/101",
                "headRefName": "codex/api-1",
                "baseRefName": "main",
            }
        )

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "CreateRefInput" in query or "CreatePullRequestInput" in query:
            return super().execute(query, variables)
        cursor = variables.get("pullRequestCursor")
        nodes = self.pull_requests[:100] if cursor is None else self.pull_requests[100:]
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": "base-oid"},
                },
                "ref": {"name": variables["qualifiedBranch"]},
                "pullRequests": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": cursor is None,
                        "endCursor": "prs-1" if cursor is None else None,
                    },
                },
            }
        }


class RacingHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_ref_once = True
        self.fail_pull_request_once = True

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "CreateRefInput" in query and self.fail_ref_once:
            self.fail_ref_once = False
            self.ref_exists = True
            raise ProviderError("reference already exists")
        if "CreatePullRequestInput" in query and self.fail_pull_request_once:
            self.fail_pull_request_once = False
            self.pull_requests.append(
                {
                    "number": 8,
                    "url": "https://example.test/owner/api/pull/8",
                    "headRefName": variables["input"]["headRefName"],
                    "baseRefName": variables["input"]["baseRefName"],
                    "body": variables["input"]["body"],
                }
            )
            raise ProviderError("pull request already exists")
        return super().execute(query, variables)


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
    client.pull_requests[-1]["body"] = _handoff_marker(request)

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

    assert result.pull_request_number == 8
    assert len(client.pull_requests) == 1
