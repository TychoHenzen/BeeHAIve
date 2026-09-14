from copy import deepcopy
from pathlib import Path

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot


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
