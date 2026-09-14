from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot


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
