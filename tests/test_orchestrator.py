import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from conftest import FakeProvider

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubOutcomeUnknownError,
    GitHubProjectProvider,
    GitHubRateLimitError,
    GraphQLClient,
    ProviderError,
    _handoff_marker,
)
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore, StoreError


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


def test_synchronize_routes_current_head_blocking_check_once() -> None:
    provider = FakeProvider(snapshot())
    routing_store = RoutingStore()
    service = Orchestrator(
        OrchestratorStore(), provider, model_router=ModelRouter(routing_store)
    )
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    active_run = service.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "")

    provider.snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "API one",
                        Stage.IMPLEMENT,
                        "In Progress",
                        True,
                        {
                            "pull_requests": [
                                {
                                    "number": 9,
                                    "state": "open",
                                    "checks": {
                                        "head_sha": "abc",
                                        "verdict": "blocking",
                                        "blocking_contexts": [
                                            {
                                                "name": "codeql",
                                                "state": "failure",
                                                "required": True,
                                                "verdict": "blocking",
                                                "url": "https://example.test/codeql",
                                            }
                                        ],
                                    },
                                }
                            ]
                        },
                    ),
                    PbiSnapshot("owner/api", 2, "API two"),
                ),
            ),
            RepositorySnapshot("owner/web", (PbiSnapshot("owner/web", 3, "Web one"),)),
        ),
    )

    service.synchronize("project-1")
    failed = service.store.get_run(active_run.run_id)
    assert failed is not None
    assert failed.status is RunStatus.FAILED
    assert failed.last_error == (
        "Pull request #9 check 'codeql' blocks head abc: failure "
        "(https://example.test/codeql)"
    )
    assert len(routing_store.get_attempts(active_run.run_id)) == 1

    service.synchronize("project-1")
    assert len(routing_store.get_attempts(active_run.run_id)) == 1
    service.store.close()
    routing_store.close()


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


def test_github_provider_maps_live_dashboard_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=FakeGraphQLClient(
            {
                "user": {
                    "projectV2": {
                        "title": "Planning",
                        "repositories": {"nodes": [{"nameWithOwner": "owner/api"}]},
                        "items": {
                            "nodes": [
                                {
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 1,
                                        "title": "API one",
                                        "repository": {"nameWithOwner": "owner/api"},
                                        "labels": {
                                            "nodes": [
                                                {"name": "bounces/2"},
                                                {"name": "escalation/terra"},
                                            ]
                                        },
                                        "subIssues": {
                                            "nodes": [
                                                {"number": "bad", "title": "Ignored"},
                                                {
                                                    "number": 2,
                                                    "title": "API child",
                                                    "state": "OPEN",
                                                    "labels": {
                                                        "nodes": [
                                                            {"name": "stage/implement"}
                                                        ]
                                                    },
                                                },
                                            ]
                                        },
                                        "comments": {
                                            "nodes": [
                                                {"body": "   "},
                                                {
                                                    "author": {},
                                                    "body": "Started review",
                                                    "createdAt": "2026-09-09T08:00:00Z",
                                                    "url": "https://example.test/comment/1",
                                                },
                                            ]
                                        },
                                        "closedByPullRequestsReferences": {
                                            "nodes": [
                                                {"number": "bad"},
                                                {
                                                    "number": 9,
                                                    "url": "https://example.test/pull/9",
                                                    "state": "CLOSED",
                                                    "merged": True,
                                                    "reviewDecision": (
                                                        "CHANGES_REQUESTED"
                                                    ),
                                                    "reviewRequests": {
                                                        "nodes": [
                                                            {"requestedReviewer": None},
                                                            {"requestedReviewer": {}},
                                                            {
                                                                "requestedReviewer": {
                                                                    "login": "tests"
                                                                }
                                                            },
                                                        ]
                                                    },
                                                    "latestReviews": {
                                                        "nodes": [
                                                            {
                                                                "author": None,
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {},
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "bot"
                                                                },
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "tests"
                                                                },
                                                                "state": (
                                                                    "CHANGES_REQUESTED"
                                                                ),
                                                                "body": (
                                                                    "Please add a test"
                                                                ),
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "security"
                                                                },
                                                                "state": "APPROVED",
                                                                "body": "Looks good",
                                                                "submittedAt": (
                                                                    "2026-09-09T08:01:00Z"
                                                                ),
                                                                "url": "https://example.test/review/1",
                                                            },
                                                        ]
                                                    },
                                                },
                                            ]
                                        },
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "In Progress",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        ),
    )

    discovered = provider.discover_project("owner:7")

    metadata = discovered.repositories[0].pbis[0].metadata
    child = metadata["subtasks"][0]  # type: ignore[index]
    assert child["id"] == "#2"  # type: ignore[index]
    assert child["title"] == "API child"  # type: ignore[index]
    assert child["labels"] == ["stage/implement"]  # type: ignore[index]
    assert child["readiness"] == "unknown"  # type: ignore[index]
    assert child["readiness_reasons"] == ["child_response_incomplete"]  # type: ignore[index]
    assert metadata["dependency_readiness"]["status"] == "unknown"  # type: ignore[index]
    assert metadata["readers"] == [
        {"id": "#9:tests", "name": "tests", "pull_request": 9, "status": "fail"},
        {"id": "#9:bot", "name": "bot", "pull_request": 9, "status": "pending"},
        {"id": "#9:security", "name": "security", "pull_request": 9, "status": "pass"},
    ]
    assert metadata["reviewers"]["#9:tests"]["status"] == "fail"  # type: ignore[index]
    assert metadata["reviewers"]["#9:security"]["status"] == "pass"  # type: ignore[index]
    assert metadata["reviewers"]["#9:bot"]["status"] == "pending"  # type: ignore[index]
    pull_request = metadata["pull_requests"][0]  # type: ignore[index]
    assert pull_request["state"] == "closed"  # type: ignore[index]
    assert pull_request["merged"] is True  # type: ignore[index]
    assert metadata["escalation"] == {
        "current": 2,
        "consecutive": 2,
        "current_tier": "terra",
    }
    assert metadata["activity"][0]["action"] == "Started review"  # type: ignore[index]


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


def test_sync_cancels_worker_for_removed_pbi() -> None:
    provider = FakeProvider(snapshot())
    store = OrchestratorStore()
    service = Orchestrator(store, provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/web", "worker-1")
    assert run is not None
    cancelled: list[str] = []
    service.register_worker_canceller(cancelled.append)
    provider.snapshot = ProjectSnapshot(
        project_id="project-1",
        name="Planning",
        repositories=(snapshot().repositories[0],),
    )

    service.synchronize("project-1")

    assert cancelled == [run.run_id]
    removed_run = store.get_run(run.run_id)
    assert removed_run is not None
    assert removed_run.status.value == "failed"
    assert removed_run.lease_token is None
    store.close()


def test_routing_recovery_errors_are_mapped() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(snapshot()))
    service.recover_routing_problem("missing", "result persistence failed")
    routing_store = RoutingStore()
    routed = Orchestrator(store, FakeProvider(snapshot()), ModelRouter(routing_store))

    with pytest.raises(StoreError, match="Unknown routing problem"):
        routed.recover_routing_problem("missing", "result persistence failed")
    routing_store.close()
    store.close()


def test_sync_replaces_removed_dashboard_metadata() -> None:
    store = OrchestratorStore()
    rich_snapshot = ProjectSnapshot(
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
                        metadata={"subtasks": [{"id": "child"}]},
                    ),
                ),
            ),
        ),
    )
    empty_snapshot = ProjectSnapshot(
        project_id="project-1",
        name="Planning",
        repositories=(
            RepositorySnapshot(
                "owner/api",
                (PbiSnapshot("owner/api", 1, "API one"),),
            ),
        ),
    )

    store.sync_project(rich_snapshot)
    store.sync_project(empty_snapshot)

    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]
    assert pbi["metadata"] == {}  # type: ignore[index]
    store.close()


def test_sync_replays_readiness_metadata_across_store_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "readiness.db"
    metadata: dict[str, object] = {
        "subtasks": [
            {
                "id": "#2",
                "number": 2,
                "readiness": "ready",
                "readiness_reasons": [],
                "blocked_by": [],
            }
        ],
        "dependency_readiness": {
            "status": "ready",
            "counts": {
                "ready": 1,
                "incomplete": 0,
                "blocked": 0,
                "rejected": 0,
                "completed": 0,
                "unknown": 0,
            },
            "reasons": [],
            "observed_at": "2026-09-14T08:00:00+00:00",
        },
    }
    project = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (PbiSnapshot("owner/api", 1, "Parent PBI", metadata=metadata),),
            ),
        ),
    )
    store = OrchestratorStore(database)

    store.sync_project(project)
    first_pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]
    first_metadata = deepcopy(first_pbi["metadata"])  # type: ignore[index]
    first_events = deepcopy(first_pbi["events"])  # type: ignore[index]
    store.sync_project(project)
    replayed_pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]

    assert replayed_pbi["metadata"] == first_metadata  # type: ignore[index]
    assert replayed_pbi["events"] == first_events  # type: ignore[index]
    store.close()

    restarted = OrchestratorStore(database)
    restored_pbi = restarted.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]
    assert restored_pbi["metadata"] == first_metadata  # type: ignore[index]
    assert restored_pbi["events"] == first_events  # type: ignore[index]
    restarted.close()


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
        first_service.handoff(
            run.run_id,
            "codex/api-1",
            None,
            "Closes #1",
            lease_token,
            head_sha="a" * 40,
            verification_evidence='{"tests":12}',
        )
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
    assert provider.handoffs[0].head_sha == "a" * 40
    assert provider.handoffs[1].head_sha == "a" * 40
    assert provider.handoffs[1].verification_evidence == '{"tests":12}'
    assert "Pushed head: " + "a" * 40 in (completed.last_result or "")
    assert '{"tests":12}' in (completed.last_result or "")
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


def _readiness_connection(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }


def _readiness_issue_content(
    number: int,
    title: str,
    state: str,
    state_reason: str | None,
    subtasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "__typename": "Issue",
        "number": number,
        "title": title,
        "state": state,
        "stateReason": state_reason,
        "url": f"https://example.test/owner/api/issues/{number}",
        "repository": {"nameWithOwner": "owner/api"},
        "labels": _readiness_connection([]),
        "subIssues": _readiness_connection(subtasks or []),
        "comments": _readiness_connection([]),
        "closedByPullRequestsReferences": _readiness_connection([]),
    }


class ReadinessGraphQLClient(FakeGraphQLClient):
    def __init__(
        self,
        children: list[dict[str, Any]],
        *,
        dependency_pages: dict[int, list[list[dict[str, Any]]]] | None = None,
        rest_statuses: dict[int, int] | None = None,
        extra_subissues: list[dict[str, Any]] | None = None,
    ) -> None:
        child_nodes = [
            {
                "number": child["number"],
                "title": child["title"],
                "state": child["issue_state"],
                "stateReason": child["state_reason"],
                "labels": _readiness_connection([]),
            }
            for child in children
        ]
        child_nodes.extend(extra_subissues or [])
        items = [
            {
                "content": _readiness_issue_content(
                    1, "Parent PBI", "OPEN", None, child_nodes
                ),
                "fieldValues": {
                    "nodes": [{"name": "In Progress", "field": {"name": "Status"}}]
                },
            }
        ]
        for child in children:
            project_status = child["project_status"]
            values = (
                [{"name": project_status, "field": {"name": "Status"}}]
                if isinstance(project_status, str)
                else []
            )
            items.append(
                {
                    "content": _readiness_issue_content(
                        child["number"],
                        child["title"],
                        child["issue_state"],
                        child["state_reason"],
                    ),
                    "fieldValues": {"nodes": values},
                }
            )
        super().__init__(
            {
                "user": {
                    "projectV2": {
                        "title": "Planning",
                        "repositories": {
                            "nodes": [{"nameWithOwner": "owner/api"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "items": {
                            "nodes": items,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        )
        self.dependency_pages = dependency_pages or {}
        self.rest_statuses = rest_statuses or {}
        self.rest_calls: list[tuple[str, str]] = []
        self.queries: list[str] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.queries.append(query)
        return super().execute(query, variables)

    def request_rest(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        self.rest_calls.append((method, path))
        issue_number = int(path.split("/issues/", 1)[1].split("/", 1)[0])
        page = int(path.rsplit("page=", 1)[1])
        pages = self.dependency_pages.get(issue_number, [[]])
        response = pages[page - 1] if page <= len(pages) else []
        return self.rest_statuses.get(issue_number, 200), response


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


class NestedMetadataGraphQLClient:
    def __init__(self) -> None:
        self.repositories = [{"nameWithOwner": "owner/api"}]
        self.subtasks = [
            {
                "number": number,
                "title": f"Child {number}",
                "state": "OPEN",
                "labels": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": number == 1, "endCursor": "labels-2"},
                },
            }
            for number in range(1, 101)
        ]
        self.comments = [
            {
                "author": {"login": "writer"},
                "body": f"Comment {number}",
                "createdAt": f"2026-09-09T08:{number:02d}:00Z",
                "url": f"https://example.test/comment/{number}",
            }
            for number in range(1, 102)
        ]
        self.pull_requests = [
            {
                "number": number,
                "url": f"https://example.test/pull/{number}",
                "reviewDecision": "APPROVED",
                "reviewRequests": {
                    "nodes": (
                        [{"requestedReviewer": {"login": "reviewer-1"}}]
                        if number == 1
                        else []
                    ),
                    "pageInfo": {
                        "hasNextPage": number == 1,
                        "endCursor": "requests-1" if number == 1 else None,
                    },
                },
                "latestReviews": {
                    "nodes": (
                        [{"author": {"login": "reviewer-1"}, "state": "COMMENTED"}]
                        if number == 1
                        else []
                    ),
                    "pageInfo": {
                        "hasNextPage": number == 1,
                        "endCursor": "reviews-1" if number == 1 else None,
                    },
                },
            }
            for number in range(1, 102)
        ]

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        cursor = variables.get("cursor")
        if "projectV2" in query and "items" in query:
            issue = {
                "__typename": "Issue",
                "number": 1,
                "title": "Paged issue",
                "repository": {"nameWithOwner": "owner/api"},
                "labels": {
                    "nodes": [{"name": "escalation/terra"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "labels-1"},
                },
                "subIssues": {
                    "nodes": self.subtasks,
                    "pageInfo": {"hasNextPage": True, "endCursor": "subissues-1"},
                },
                "comments": {
                    "nodes": self.comments[:100],
                    "pageInfo": {"hasNextPage": True, "endCursor": "comments-1"},
                },
                "closedByPullRequestsReferences": {
                    "nodes": self.pull_requests[:100],
                    "pageInfo": {"hasNextPage": True, "endCursor": "pulls-1"},
                },
            }
            return {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "content": issue,
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Backlog",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "repositories" in query:
            nodes = self.repositories if cursor is None else []
            return {
                "user": {
                    "projectV2": {
                        "repositories": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "closedByPullRequestsReferences" in query:
            nodes = self.pull_requests[100:] if cursor == "pulls-1" else []
            return {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "subIssues" in query:
            return {
                "repository": {
                    "issue": {
                        "subIssues": {
                            "nodes": [
                                {
                                    "number": 101,
                                    "title": "Child 101",
                                    "state": "OPEN",
                                    "labels": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "comments" in query:
            return {
                "repository": {
                    "issue": {
                        "comments": {
                            "nodes": [self.comments[100]],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "reviewRequests" in query:
            return {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": {
                            "nodes": [{"requestedReviewer": {"login": "reviewer-101"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "latestReviews" in query:
            return {
                "repository": {
                    "pullRequest": {
                        "latestReviews": {
                            "nodes": [
                                {
                                    "author": {"login": "reviewer-101"},
                                    "state": "APPROVED",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "labels" in query:
            labels = (
                [{"name": "bounces/2"}]
                if variables.get("number") == 1 and cursor == "labels-1"
                else [{"name": "stage/implement"}]
                if variables.get("number") == 1 and cursor == "labels-2"
                else []
            )
            return {
                "repository": {
                    "issue": {
                        "labels": {
                            "nodes": labels,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        return {"user": {"projectV2": {"title": "Paged Planning"}}}


class CheckGraphQLClient:
    def __init__(
        self,
        *,
        observed_head: str | None = "head-1",
        rollup_oid: str | None = None,
        page_observed_head: str | None = None,
        page_rollup_oid: str | None = None,
        observed_state: str = "OPEN",
        error: str | None = None,
    ) -> None:
        self.cursors: list[object] = []
        self.observed_head = observed_head
        self.rollup_oid = rollup_oid
        self.page_observed_head = page_observed_head
        self.page_rollup_oid = page_rollup_oid
        self.observed_state = observed_state
        self.error = error

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "statusCheckRollup" in query:
            if self.error is not None:
                raise ProviderError(self.error)
            cursor = variables.get("cursor")
            self.cursors.append(cursor)
            page_head = (
                self.page_observed_head
                if cursor is not None and self.page_observed_head is not None
                else self.observed_head
            )
            page_rollup_oid = (
                self.page_rollup_oid
                if cursor is not None and self.page_rollup_oid is not None
                else self.rollup_oid
            )
            contexts = (
                [
                    {
                        "__typename": "CheckRun",
                        "name": "python-tests",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                        "detailsUrl": "https://example.test/python-tests",
                        "completedAt": "2026-09-11T08:01:00Z",
                    }
                ]
                if cursor is None
                else [
                    {
                        "__typename": "StatusContext",
                        "context": "legacy-status",
                        "state": "SUCCESS",
                        "isRequired": False,
                        "targetUrl": "https://example.test/legacy-status",
                        "updatedAt": "2026-09-11T08:02:00Z",
                    }
                ]
            )
            return {
                "repository": {
                    "pullRequest": {
                        "state": self.observed_state,
                        "headRef": (
                            {"target": {"oid": page_head}}
                            if page_head is not None
                            else None
                        ),
                        "statusCheckRollup": {
                            "commit": {"oid": page_rollup_oid or page_head},
                            "state": "SUCCESS",
                            "contexts": {
                                "nodes": contexts,
                                "pageInfo": {
                                    "hasNextPage": cursor is None,
                                    "endCursor": "checks-1" if cursor is None else None,
                                },
                            },
                        },
                    }
                }
            }
        return {
            "repository": {
                "issue": {
                    "labels": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "subIssues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "comments": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "closedByPullRequestsReferences": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
                "pullRequest": {
                    "reviewRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "latestReviews": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            }
        }


class HandoffGraphQLClient:
    def __init__(self) -> None:
        self.ref_exists = False
        self.branch_sha = "base-oid"
        self.pull_requests: list[dict[str, object]] = []
        self.ref_creations = 0
        self.ref_oids: list[object] = []
        self.pull_request_creations = 0
        self.pull_request_updates = 0
        self.pull_request_bases: list[object] = []
        self.created_pull_request_inputs: list[dict[str, object]] = []
        self.updated_pull_request_inputs: list[dict[str, object]] = []

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
            self.branch_sha = str(variables["input"]["oid"])
            return {
                "createRef": {
                    "ref": {
                        "name": variables["input"]["name"],
                        "target": {"oid": self.branch_sha},
                    }
                }
            }
        if "CreatePullRequestInput" in query:
            self.pull_request_creations += 1
            self.pull_request_bases.append(variables["input"]["baseRefName"])
            self.created_pull_request_inputs.append(variables["input"])
            pull_request = {
                "id": "pull-request-node-8",
                "number": 8,
                "url": "https://example.test/owner/api/pull/8",
                "title": variables["input"]["title"],
                "state": "OPEN",
                "isDraft": variables["input"]["draft"],
                "headRefName": variables["input"]["headRefName"],
                "headRefOid": self.branch_sha,
                "baseRefName": variables["input"]["baseRefName"],
                "body": variables["input"]["body"],
            }
            self.pull_requests.append(pull_request)
            return {"createPullRequest": {"pullRequest": pull_request}}
        if "UpdatePullRequestInput" in query:
            self.pull_request_updates += 1
            self.updated_pull_request_inputs.append(variables["input"])
            pull_request = next(
                item
                for item in self.pull_requests
                if item.get("id") == variables["input"]["pullRequestId"]
            )
            pull_request["title"] = variables["input"]["title"]
            pull_request["body"] = variables["input"]["body"]
            return {"updatePullRequest": {"pullRequest": pull_request}}
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": "base-oid"},
                },
                "ref": {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": self.branch_sha},
                }
                if self.ref_exists
                else None,
                "pullRequests": {
                    "nodes": self.pull_requests,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }


class ConcurrentProviderHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self._state_lock = Lock()
        self._initial_reads = Barrier(2)
        self.repository_reads = 0
        self.ref_create_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequestCursor" in variables:
            with self._state_lock:
                self.repository_reads += 1
                initial_read = self.repository_reads <= 2
                response = deepcopy(super().execute(query, variables))
            if initial_read:
                self._initial_reads.wait(timeout=5)
            return response
        if "CreateRefInput" in query:
            with self._state_lock:
                self.ref_create_attempts += 1
                if self.ref_exists:
                    raise ProviderError("reference already exists")
                return super().execute(query, variables)
        if "CreatePullRequestInput" in query:
            with self._state_lock:
                if self.pull_requests:
                    raise ProviderError("pull request already exists")
                return super().execute(query, variables)
        if "UpdatePullRequestInput" in query:
            with self._state_lock:
                return super().execute(query, variables)
        return super().execute(query, variables)


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


def test_github_provider_paginates_nested_dashboard_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=NestedMetadataGraphQLClient()
    )

    discovered = provider.discover_project("owner:7")
    metadata = discovered.repositories[0].pbis[0].metadata

    assert len(metadata["subtasks"]) == 101  # type: ignore[arg-type]
    assert metadata["subtasks"][0]["labels"] == ["stage/implement"]  # type: ignore[index]
    assert len(metadata["activity"]) == 101  # type: ignore[arg-type]
    assert len(metadata["pull_requests"]) == 101  # type: ignore[arg-type]
    assert metadata["reviewers"]["#1:reviewer-101"]["status"] == "pass"  # type: ignore[index]
    assert metadata["escalation"] == {
        "current": 2,
        "consecutive": 2,
        "current_tier": "terra",
    }
    readiness = metadata["dependency_readiness"]  # type: ignore[index]
    assert readiness["status"] == "unknown"  # type: ignore[index]
    assert readiness["counts"]["unknown"] == 101  # type: ignore[index]


def test_github_provider_projects_child_facts_and_dependency_pages() -> None:
    children = [
        {
            "number": 2,
            "title": "Ready",
            "issue_state": "OPEN",
            "state_reason": "REOPENED",
            "project_status": "Todo",
        },
        {
            "number": 3,
            "title": "Incomplete",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "In Progress",
        },
        {
            "number": 4,
            "title": "Blocked",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "Todo",
        },
        {
            "number": 5,
            "title": "Rejected",
            "issue_state": "CLOSED",
            "state_reason": "NOT_PLANNED",
            "project_status": "Done",
        },
        {
            "number": 6,
            "title": "Completed",
            "issue_state": "CLOSED",
            "state_reason": "COMPLETED",
            "project_status": "Done",
        },
        {
            "number": 7,
            "title": "Unknown",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "Todo",
        },
    ]
    first_page = [
        {
            "id": 1_000 + index,
            "node_id": f"I_kwDO{index}",
            "number": 1_000 + index,
            "html_url": f"https://example.test/issues/{1_000 + index}",
            "title": f"Completed blocker {index}",
            "state": "closed",
            "state_reason": "completed",
        }
        for index in range(100)
    ]
    final_blocker = {
        "id": 1_100,
        "node_id": "I_kwDO1100",
        "number": 1_100,
        "html_url": "https://example.test/issues/1100",
        "title": "Open blocker",
        "state": "open",
        "state_reason": None,
    }
    client = ReadinessGraphQLClient(
        children,
        dependency_pages={4: [first_page, [final_blocker]]},
        rest_statuses={7: 403},
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")
    parent = discovered.repositories[0].pbis[0]
    metadata = parent.metadata
    projected = {child["number"]: child for child in metadata["subtasks"]}  # type: ignore[index]
    readiness = metadata["dependency_readiness"]  # type: ignore[assignment]

    assert {number: child["readiness"] for number, child in projected.items()} == {
        2: "ready",
        3: "incomplete",
        4: "blocked",
        5: "rejected",
        6: "completed",
        7: "unknown",
    }
    assert readiness["status"] == "unknown"  # type: ignore[index]
    assert readiness["counts"] == {  # type: ignore[index]
        "ready": 1,
        "incomplete": 1,
        "blocked": 1,
        "rejected": 1,
        "completed": 1,
        "unknown": 1,
    }
    assert metadata["issue_state"] == "OPEN"
    assert metadata["project_status"] == "In Progress"
    assert projected[2]["issue_state"] == "OPEN"
    assert projected[2]["state_reason"] == "REOPENED"
    assert projected[2]["project_status"] == "Todo"
    assert projected[2]["blocked_by"] == []
    assert projected[2]["dependency_read_complete"] is True
    assert projected[4]["readiness_reasons"] == ["blocked_by_open:#1100"]
    assert len(projected[4]["blocked_by"]) == 101
    assert projected[7]["dependency_read_error"] == "permission_denied"
    assert [
        path.rsplit("page=", 1)[1]
        for method, path in client.rest_calls
        if method == "GET" and "/issues/4/" in path
    ] == ["1", "2"]
    assert all(method == "GET" for method, _ in client.rest_calls)
    assert not any("mutation" in query.casefold() for query in client.queries)


def test_github_provider_keeps_missing_child_project_status_unknown() -> None:
    client = ReadinessGraphQLClient(
        [
            {
                "number": 2,
                "title": "Not in a Project status",
                "issue_state": "OPEN",
                "state_reason": None,
                "project_status": None,
            }
        ]
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    metadata = provider.discover_project("owner:7").repositories[0].pbis[0].metadata
    child = metadata["subtasks"][0]  # type: ignore[index]

    assert child["project_status"] is None  # type: ignore[index]
    assert child["readiness"] == "unknown"  # type: ignore[index]
    assert child["readiness_reasons"] == ["project_status_unknown"]  # type: ignore[index]


def test_github_provider_does_not_drop_a_malformed_child_from_the_aggregate() -> None:
    client = ReadinessGraphQLClient(
        [
            {
                "number": 2,
                "title": "Ready-looking child",
                "issue_state": "OPEN",
                "state_reason": None,
                "project_status": "Todo",
            }
        ],
        extra_subissues=[
            {
                "number": 8,
                "title": None,
                "state": "OPEN",
                "stateReason": None,
                "labels": _readiness_connection([]),
            }
        ],
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    metadata = provider.discover_project("owner:7").repositories[0].pbis[0].metadata

    assert metadata["subtasks"][0]["readiness"] == "ready"  # type: ignore[index]
    assert metadata["dependency_readiness"]["status"] == "unknown"  # type: ignore[index]
    assert "child_response_incomplete" in metadata["dependency_readiness"]["reasons"]  # type: ignore[index]


def test_github_provider_paginates_current_pull_request_checks() -> None:
    client = CheckGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    issue = {
        "repository": {"nameWithOwner": "owner/api"},
        "number": 1,
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "subIssues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "closedByPullRequestsReferences": {
            "nodes": [
                {
                    "number": 9,
                    "state": "OPEN",
                    "headRef": {"target": {"oid": "head-1"}},
                    "reviewRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "latestReviews": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }

    completed = provider._complete_issue_metadata(issue)
    metadata = provider_module._dashboard_metadata(completed)
    checks = metadata["pull_requests"][0]["checks"]  # type: ignore[index]

    assert client.cursors == [None, "checks-1"]
    assert checks["head_sha"] == "head-1"  # type: ignore[index]
    assert checks["verdict"] == "passing"  # type: ignore[index]
    assert [context["kind"] for context in checks["contexts"]] == [  # type: ignore[index]
        "check_run",
        "status_context",
    ]
    assert metadata["checks"]["verdict"] == "passing"  # type: ignore[index]


def test_github_provider_requires_current_head_evidence() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=CheckGraphQLClient(observed_head=None)
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")
    missing_head = provider._complete_pull_request_checks("owner", "api", 9, None)

    assert checks["head_sha"] is None
    assert checks["verdict"] == "unproven"
    assert missing_head["verdict"] == "unproven"


def test_github_provider_rejects_head_change_during_check_pagination() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(page_observed_head="head-2"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["head_sha"] == "head-2"
    assert checks["verdict"] == "unproven"


def test_github_provider_rejects_pull_request_closed_during_check_poll() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(observed_state="MERGED"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["verdict"] == "unproven"


def test_github_provider_check_error_stays_unproven() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(error="GitHub GraphQL rate limit exceeded"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["verdict"] == "unproven"
    assert checks["head_sha"] is None
    assert checks["error"] == "GitHub GraphQL rate limit exceeded"


def test_github_provider_does_not_merge_active_pull_request_check_verdicts() -> None:
    metadata = provider_module._dashboard_metadata(
        {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": 9,
                        "state": "OPEN",
                        "checks": {
                            "head_sha": "head-9",
                            "verdict": "passing",
                            "contexts": [],
                        },
                    },
                    {
                        "number": 10,
                        "state": "OPEN",
                        "checks": {
                            "head_sha": "head-10",
                            "verdict": "unproven",
                            "contexts": [],
                        },
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    )

    checks = metadata["checks"]
    assert checks["verdict"] == "unproven"  # type: ignore[index]
    assert [
        check["head_sha"]
        for check in checks["pull_requests"]  # type: ignore[index]
    ] == ["head-9", "head-10"]
    assert [
        check["number"]
        for check in checks["pull_requests"]  # type: ignore[index]
    ] == [9, 10]


def test_github_provider_marks_no_active_pull_request_unproven() -> None:
    metadata = provider_module._dashboard_metadata(
        {"closedByPullRequestsReferences": {"nodes": []}}
    )

    assert metadata["checks"] == {"verdict": "unproven", "pull_requests": []}


def test_github_provider_ignores_incomplete_issue_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=NestedMetadataGraphQLClient()
    )
    issue = {"repository": {}, "number": 1}

    assert provider._complete_issue_metadata(issue) is issue


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
    assert client.pull_request_updates == 1
    assert client.pull_request_bases == ["main"]


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


class PaginatedPullRequestClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.ref_exists = True
        self.pull_requests = [
            {
                "id": f"old-node-{index}",
                "number": index,
                "url": f"https://example.test/owner/api/pull/{index}",
                "title": f"Old {index}",
                "state": "OPEN",
                "isDraft": True,
                "headRefName": f"old-{index}",
                "headRefOid": "base-oid",
                "baseRefName": "main",
                "body": "old body",
            }
            for index in range(100)
        ]
        self.pull_requests.append(
            {
                "id": "pull-request-node-101",
                "number": 101,
                "url": "https://example.test/owner/api/pull/101",
                "title": "API one",
                "state": "OPEN",
                "isDraft": True,
                "headRefName": "codex/api-1",
                "headRefOid": "base-oid",
                "baseRefName": "main",
                "body": "old body",
            }
        )

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if (
            "CreateRefInput" in query
            or "CreatePullRequestInput" in query
            or "UpdatePullRequestInput" in query
        ):
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
                "ref": {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": "base-oid"},
                },
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
        self.events: list[str] = []
        self.ref_create_attempts = 0
        self.pull_request_create_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequestCursor" in variables:
            self.events.append("read")
        if "CreateRefInput" in query:
            self.events.append("create-ref")
            self.ref_create_attempts += 1
            if self.fail_ref_once:
                self.fail_ref_once = False
                self.ref_exists = True
                self.ref_creations += 1
                raise ProviderError("reference creation timed out after commit")
        if "CreatePullRequestInput" in query:
            self.events.append("create-pull-request")
            self.pull_request_create_attempts += 1
            if self.fail_pull_request_once:
                self.fail_pull_request_once = False
                self.pull_request_creations += 1
                self.pull_requests.append(
                    {
                        "id": "pull-request-node-8",
                        "number": 8,
                        "url": "https://example.test/owner/api/pull/8",
                        "title": variables["input"]["title"],
                        "state": "OPEN",
                        "isDraft": variables["input"]["draft"],
                        "headRefName": variables["input"]["headRefName"],
                        "headRefOid": self.branch_sha,
                        "baseRefName": variables["input"]["baseRefName"],
                        "body": variables["input"]["body"],
                    }
                )
                raise ProviderError("pull request creation timed out after commit")
        if "UpdatePullRequestInput" in query:
            self.events.append("update-pull-request")
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


class WrongTargetRacingHandoffGraphQLClient(RacingHandoffGraphQLClient):
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "CreateRefInput" in query and self.fail_ref_once:
            self.fail_ref_once = False
            self.ref_exists = True
            self.branch_sha = "unexpected-oid"
            raise ProviderError("reference creation timed out")
        return super().execute(query, variables)


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


def test_github_provider_creates_and_updates_verified_draft() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    client.branch_sha = "a" * 40
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
        head_sha="a" * 40,
        verification_evidence='{"outcome":"pass"}',
    )

    created = provider.create_handoff(request)
    updated = provider.create_handoff(
        HandoffRequest(
            project_id="owner:7",
            repository="owner/api",
            pbi_number=1,
            title="API one updated",
            branch="codex/api-1",
            base_branch=None,
            body="Implementation summary",
            run_id="run-1",
            head_sha="a" * 40,
            verification_evidence='{"outcome":"pass","tests":12}',
        )
    )

    assert created == updated
    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 1
    assert client.created_pull_request_inputs[0]["draft"] is True
    assert (
        client.updated_pull_request_inputs[0]["pullRequestId"] == "pull-request-node-8"
    )
    pull_request = client.pull_requests[0]
    assert pull_request["title"] == "API one updated"
    body = str(pull_request["body"])
    assert "https://github.com/owner/api/issues/1" in body
    assert "run-1" in body
    assert "a" * 40 in body
    assert '{"outcome":"pass","tests":12}' in body
    assert _handoff_marker(request, "main") in body


def test_github_provider_does_not_reuse_pull_request_for_changed_body_intent() -> None:
    client = HandoffGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
    )
    provider.create_handoff(request)

    with pytest.raises(ProviderError, match="different handoff identity"):
        provider.create_handoff(replace(request, body="Changed implementation summary"))

    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 0


def test_github_provider_upgrades_exact_legacy_handoff_marker() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
    )
    legacy_marker = provider_module._legacy_handoff_marker(request)
    legacy_body = provider_module._handoff_body(request.body, legacy_marker, request)
    client.pull_requests.append(
        {
            "id": "pull-request-node-8",
            "number": 8,
            "url": "https://example.test/owner/api/pull/8",
            "title": request.title,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": request.branch,
            "headRefOid": client.branch_sha,
            "baseRefName": "main",
            "body": legacy_body,
        }
    )

    result = provider.create_handoff(request)

    assert result.pull_request_number == 8
    assert client.pull_request_creations == 0
    assert client.pull_request_updates == 1
    assert _handoff_marker(request, "main") in client.pull_requests[0]["body"]


def test_github_provider_does_not_upgrade_legacy_pr_for_changed_body_intent() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    original = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Original implementation summary",
        run_id="run-1",
    )
    legacy_marker = provider_module._legacy_handoff_marker(original)
    client.pull_requests.append(
        {
            "id": "pull-request-node-8",
            "number": 8,
            "url": "https://example.test/owner/api/pull/8",
            "title": original.title,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": original.branch,
            "headRefOid": client.branch_sha,
            "baseRefName": "main",
            "body": provider_module._handoff_body(
                original.body, legacy_marker, original
            ),
        }
    )

    with pytest.raises(ProviderError, match="different handoff identity"):
        provider.create_handoff(
            replace(original, body="Changed implementation summary")
        )

    assert client.pull_request_updates == 0
    assert client.pull_request_creations == 0


def test_github_provider_adds_verified_metadata_when_body_has_marker() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    client.branch_sha = "a" * 40
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
        head_sha="a" * 40,
        verification_evidence='{"outcome":"pass"}',
    )
    request = replace(request, body=_handoff_marker(request, "main"))

    provider.create_handoff(request)

    body = str(client.created_pull_request_inputs[0]["body"])
    assert "https://github.com/owner/api/issues/1" in body
    assert "Run: `run-1`" in body
    assert "Pushed head: `" + "a" * 40 + "`" in body
    assert '{"outcome":"pass"}' in body
    assert body.count(_handoff_marker(request, "main")) == 1


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


class RacingUpdateHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_update_once = True
        self.update_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "UpdatePullRequestInput" in query:
            self.update_attempts += 1
            if self.fail_update_once:
                self.fail_update_once = False
                raise ProviderError("pull request changed during update")
        return super().execute(query, variables)


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
