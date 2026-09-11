from __future__ import annotations

from conftest import FakeProvider
from fastapi.testclient import TestClient

from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app


def _pull_request(
    *, merged: bool = True, source_branch_state: str = "deleted"
) -> dict[str, object]:
    return {
        "number": 1,
        "url": "https://example.test/pull/1",
        "state": "merged" if merged else "closed",
        "merged": merged,
        "source_branch": "codex/one",
        "source_branch_state": source_branch_state,
    }


def _pbi(
    number: int,
    *,
    repository: str = "owner/api",
    planning_status: str | None = "Done",
    pull_requests: object = ("complete",),
    stage: Stage | None = None,
    claimable: bool = False,
) -> PbiSnapshot:
    metadata: dict[str, object] = {
        "source_url": f"https://example.test/issues/{number}"
    }
    if pull_requests == ("complete",):
        metadata["pull_requests"] = [_pull_request()]
    elif pull_requests is not None:
        metadata["pull_requests"] = pull_requests
    return PbiSnapshot(
        repository,
        number,
        f"PBI {number}",
        stage,
        planning_status,
        claimable,
        metadata,
    )


def _snapshot(*pbis: PbiSnapshot) -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", tuple(pbis)),),
    )


def test_archive_rule_and_dashboard_filter() -> None:
    snapshot = _snapshot(
        _pbi(1),
        _pbi(2, pull_requests=None),
        _pbi(3, pull_requests=[_pull_request(merged=False)]),
        _pbi(4, pull_requests=[_pull_request(source_branch_state="present")]),
        _pbi(5, pull_requests=[_pull_request(source_branch_state="unknown")]),
        _pbi(6, planning_status="In Progress"),
        _pbi(7, planning_status="completed"),
        _pbi(8, planning_status="closed"),
        _pbi(9, planning_status="merged"),
    )
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(snapshot))
    client = TestClient(
        create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    default = client.get("/projects/project-1/dashboard")
    archived = client.get("/projects/project-1/dashboard?archived=true")

    assert default.status_code == 200
    assert archived.status_code == 200
    assert [pbi["number"] for pbi in default.json()["repositories"][0]["pbis"]] == [
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
    ]
    archived_pbi = archived.json()["repositories"][0]["pbis"]
    assert [pbi["number"] for pbi in archived_pbi] == [1]
    assert archived.json()["counts"]["pbis"] == 1
    assert archived_pbi[0]["archived"] is True
    assert archived_pbi[0]["source_url"] == "https://example.test/issues/1"
    assert archived_pbi[0]["pull_requests"][0]["source_branch_state"] == "deleted"
    assert default.json()["counts"]["pbis"] == 8
    raw = client.get("/projects/project-1")
    assert raw.json()["repositories"][0]["pbis"][0]["archived"] is True
    store.close()


def test_active_and_failed_runs_are_not_archived() -> None:
    initial = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    _pbi(
                        7,
                        planning_status="In Progress",
                        stage=Stage.IMPLEMENT,
                        claimable=True,
                    ),
                ),
            ),
            RepositorySnapshot(
                "owner/web",
                (
                    _pbi(
                        8,
                        repository="owner/web",
                        planning_status="In Progress",
                        stage=Stage.IMPLEMENT,
                        claimable=True,
                    ),
                ),
            ),
        ),
    )
    complete = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot("owner/api", (_pbi(7),)),
            RepositorySnapshot("owner/web", (_pbi(8, repository="owner/web"),)),
        ),
    )
    store = OrchestratorStore()
    provider = FakeProvider(initial)
    service = Orchestrator(store, provider)
    service.synchronize("project-1")
    active = service.claim("project-1", "owner/api", "worker-active")
    failed = service.claim("project-1", "owner/web", "worker-failed")
    assert active is not None
    assert failed is not None
    service.fail(failed.run_id, "worker failed", failed.lease_token or "")

    provider.snapshot = complete
    service.synchronize("project-1")
    pbis = [
        pbi
        for repository in store.project_state("project-1")["repositories"]
        for pbi in repository["pbis"]
    ]
    assert {pbi["number"]: pbi["status"] for pbi in pbis} == {
        7: "active",
        8: "failed",
    }
    assert all(pbi["archived"] is False for pbi in pbis)
    assert all(pbi["events"] for pbi in pbis)
    store.close()


def test_sync_restores_changed_completion_and_keeps_history() -> None:
    store = OrchestratorStore()
    store.sync_project(_snapshot(_pbi(1)))
    with store._transaction() as connection:
        store._record_event(
            connection,
            "project-1",
            "owner/api",
            1,
            None,
            "archive-test",
            Stage.BACKLOG,
            Stage.BACKLOG,
            {"keep": True},
        )

    restored = _snapshot(
        _pbi(1, pull_requests=[_pull_request(source_branch_state="present")])
    )
    store.sync_project(restored)
    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]

    assert pbi["archived"] is False
    assert pbi["number"] == 1
    assert any(event["type"] == "archive-test" for event in pbi["events"])
    store.sync_project(ProjectSnapshot("project-1", "Planning", ()))
    unlinked = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert unlinked["active"] is False
    assert unlinked["archived"] is False
    assert any(event["type"] == "archive-test" for event in unlinked["events"])
    store.close()
