from pathlib import Path

import pytest

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
)
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.orchestrator.fake_graph_ql_client import (
    FakeGraphQLClient as FakeGraphQLClient,
)
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot


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
