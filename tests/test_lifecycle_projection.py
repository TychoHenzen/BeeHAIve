from dataclasses import replace
from pathlib import Path

from beehaiive.lifecycle_contract import LifecycleState
from beehaiive.lifecycle_projection import derive_lifecycle_projection
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.orchestrator.helpers import pull_request_snapshot


def _pbi(
    *,
    stage: Stage | None = Stage.BACKLOG,
    planning_status: str | None = None,
    metadata: dict[str, object] | None = None,
) -> PbiSnapshot:
    return PbiSnapshot(
        "owner/api",
        1,
        "API one",
        stage,
        planning_status,
        True,
        metadata or {},
    )


def _run(status: RunStatus, stage: Stage = Stage.IMPLEMENT) -> RunState:
    return RunState(
        "run-1",
        "project-1",
        "owner/api",
        1,
        "API one",
        stage,
        status,
        1,
        lease_token="lease-1",
        lease_expires_at="2099-01-01T00:00:00+00:00",
    )


def test_projection_uses_available_facts_and_fails_closed() -> None:
    assert (
        derive_lifecycle_projection(_pbi(stage=Stage.BACKLOG)).state
        is LifecycleState.PLANNING
    )
    assert (
        derive_lifecycle_projection(_pbi(stage=None, planning_status="Todo")).state
        is LifecycleState.READY
    )
    assert (
        derive_lifecycle_projection(_pbi(stage=Stage.REFINE)).state
        is LifecycleState.REFINEMENT
    )
    assert (
        derive_lifecycle_projection(_pbi(), _run(RunStatus.ACTIVE)).state
        is LifecycleState.IMPLEMENTATION
    )
    assert (
        derive_lifecycle_projection(_pbi(), _run(RunStatus.AWAITING_OPERATOR)).state
        is LifecycleState.QUESTION
    )
    assert (
        derive_lifecycle_projection(_pbi(), _run(RunStatus.FAILED)).state
        is LifecycleState.FAILED
    )
    assert (
        derive_lifecycle_projection(
            _pbi(
                stage=Stage.PULL_REQUEST,
                planning_status="Done",
                metadata={"pull_requests": [{"merged": True}]},
            )
        ).state
        is LifecycleState.COMPLETED
    )
    assert (
        derive_lifecycle_projection(
            _pbi(stage=Stage.BACKLOG, planning_status="Done")
        ).state
        is LifecycleState.UNKNOWN
    )
    assert (
        derive_lifecycle_projection(
            _pbi(metadata={"checks": {"verdict": "blocking"}})
        ).state
        is LifecycleState.BLOCKED
    )
    assert (
        derive_lifecycle_projection(
            _pbi(
                metadata={
                    "pull_requests": [{"state": "open"}],
                    "checks": {"verdict": "pending"},
                }
            )
        ).state
        is LifecycleState.CHECKS
    )
    assert (
        derive_lifecycle_projection(
            _pbi(
                metadata={
                    "pull_requests": [{"state": "open"}],
                    "checks": {"verdict": "passing"},
                }
            ),
            optional_sources={
                "review": {
                    "status": "active",
                    "source_id": "cycle-1",
                    "source_version": "2",
                }
            },
        ).state
        is LifecycleState.REVIEW
    )
    assert (
        derive_lifecycle_projection(
            _pbi(),
            optional_sources={
                "workflow": {
                    "status": "blocked",
                    "source_id": "workflow-1",
                    "source_version": "1",
                }
            },
        ).state
        is LifecycleState.BLOCKED
    )
    for workflow_status in ("awaiting_approval", "awaiting_clarification"):
        assert (
            derive_lifecycle_projection(
                _pbi(),
                optional_sources={
                    "workflow": {
                        "status": workflow_status,
                        "source_id": "workflow-1",
                        "source_version": "1",
                    }
                },
            ).state
            is LifecycleState.QUESTION
        )
    assert (
        derive_lifecycle_projection(
            _pbi(),
            optional_sources={
                "routing": {
                    "status": "human_handoff",
                    "source_id": "routing-1",
                    "source_version": "1",
                }
            },
        ).state
        is LifecycleState.HUMAN_ACTION_REQUIRED
    )
    assert (
        derive_lifecycle_projection(
            _pbi(
                metadata={
                    "pull_requests": [{"state": "open"}],
                    "checks": {"verdict": "passing"},
                }
            )
        ).state
        is LifecycleState.UNKNOWN
    )
    assert (
        derive_lifecycle_projection(_pbi(metadata={"review_head_changed": True})).state
        is LifecycleState.IMPLEMENTATION
    )
    assert (
        derive_lifecycle_projection(_pbi(metadata={"stale_approval": True})).state
        is LifecycleState.BLOCKED
    )
    assert (
        derive_lifecycle_projection(
            _pbi(
                planning_status="Done",
                metadata={"pull_requests": [{"state": "open", "merged": True}]},
            )
        ).state
        is LifecycleState.UNKNOWN
    )
    assert (
        derive_lifecycle_projection(_pbi(stage=Stage.IMPLEMENT)).state
        is LifecycleState.UNKNOWN
    )
    assert (
        derive_lifecycle_projection(_pbi(stage=Stage.PULL_REQUEST)).state
        is LifecycleState.UNKNOWN
    )
    expired = replace(
        _run(RunStatus.ACTIVE),
        lease_expires_at="2000-01-01T00:00:00+00:00",
    )
    assert derive_lifecycle_projection(_pbi(), expired).state is LifecycleState.UNKNOWN
    assert derive_lifecycle_projection(_pbi(stage=None)).state is LifecycleState.UNKNOWN


def test_projection_preserves_optional_source_unknowns_and_is_deterministic() -> None:
    first = derive_lifecycle_projection(_pbi(stage=Stage.REFINE))
    second = derive_lifecycle_projection(_pbi(stage=Stage.REFINE))
    assert first.source_version == second.source_version
    assert first.facts["optional_sources"] == {
        "review": {"status": "unknown"},
        "workflow": {"status": "unknown"},
        "routing": {"status": "unknown"},
    }
    with_sources = derive_lifecycle_projection(
        _pbi(stage=Stage.REFINE),
        optional_sources={
            "review": {
                "source_id": "cycle-1",
                "source_version": "2",
                "status": "active",
                "secret": "must not persist",
            }
        },
    )
    assert with_sources.facts["optional_sources"]["review"] == {
        "source_id": "cycle-1",
        "source_version": "2",
        "status": "active",
    }
    assert with_sources.source_version != first.source_version


def test_synchronization_persists_canonical_state_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    provider = FakeProvider(pull_request_snapshot())
    store = OrchestratorStore(database)
    service = Orchestrator(store, provider)

    service.synchronize("project-1")
    first = store.canonical_lifecycle_for("project-1", "owner/api", 1)
    evidence = store.lifecycle_evidence_for("project-1", "owner/api", 1)
    assert first is not None and first["state"] == "planning"
    first_version = str(first["source_version"])
    assert len(evidence) == 1

    conflict = store.project_canonical_lifecycle(
        pull_request_snapshot(),
        expected_source_versions={("owner/api", 1): "stale-version"},
    )
    assert conflict[0]["state"] == "unknown"
    assert (
        store.canonical_lifecycle_for("project-1", "owner/api", 1)["state"] == "unknown"
    )  # type: ignore[index]
    assert (
        store.lifecycle_evidence_for("project-1", "owner/api", 1)[-1]["event_type"]
        == "canonical_conflict"
    )

    service.synchronize("project-1")
    assert len(store.lifecycle_evidence_for("project-1", "owner/api", 1)) == 2
    assert (
        store.canonical_lifecycle_for("project-1", "owner/api", 1)["source_version"]
        == first_version
    )  # type: ignore[index]

    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None and run.lease_token is not None
    service.advance(run.run_id, Stage.IMPLEMENT, run.lease_token)

    provider.snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (_pbi(stage=Stage.IMPLEMENT, planning_status="In Progress"),),
            ),
        ),
    )
    service.synchronize("project-1")
    changed = store.canonical_lifecycle_for("project-1", "owner/api", 1)
    assert changed is not None and changed["state"] == "implementation"
    assert len(store.lifecycle_evidence_for("project-1", "owner/api", 1)) == 3
    store.close()

    reopened = OrchestratorStore(database)
    persisted = reopened.canonical_lifecycle_for("project-1", "owner/api", 1)
    assert persisted is not None and persisted["state"] == "implementation"
    assert len(reopened.lifecycle_evidence_for("project-1", "owner/api", 1)) == 3
    Orchestrator(reopened, provider).synchronize("project-1")
    assert len(reopened.lifecycle_evidence_for("project-1", "owner/api", 1)) == 3
    reopened.close()


def test_transition_evidence_retention_is_bounded(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    base = ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (_pbi(),)),),
    )
    store.sync_project(base)
    for marker in range(105):
        store.project_canonical_lifecycle(
            ProjectSnapshot(
                "project-1",
                "Planning",
                (
                    RepositorySnapshot(
                        "owner/api",
                        (_pbi(metadata={"marker": marker}),),
                    ),
                ),
            )
        )
    evidence = store.lifecycle_evidence_for("project-1", "owner/api", 1)
    assert len(evidence) == 100
    assert len({item["replay_id"] for item in evidence}) == 100
    store.close()


def test_initial_concurrent_synchronization_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    first_store = OrchestratorStore(database)
    second_store = OrchestratorStore(database)
    first_snapshot = pull_request_snapshot()
    second_snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (_pbi(stage=Stage.REFINE),)),),
    )
    first_expected = first_store.canonical_source_versions("project-1")
    second_expected = second_store.canonical_source_versions("project-1")
    first_store.sync_project(first_snapshot)
    second_store.sync_project(second_snapshot)
    first_store.project_canonical_lifecycle(
        first_snapshot, expected_source_versions=first_expected
    )
    conflict = second_store.project_canonical_lifecycle(
        second_snapshot, expected_source_versions=second_expected
    )
    assert conflict[0]["state"] == "unknown"
    assert conflict[0]["reason_code"] == "conflict"
    assert (
        second_store.lifecycle_evidence_for("project-1", "owner/api", 1)[-1][
            "event_type"
        ]
        == "canonical_conflict"
    )
    first_store.close()
    second_store.close()


def test_completed_canonical_state_never_regresses(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    completed = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    _pbi(
                        stage=Stage.PULL_REQUEST,
                        planning_status="Done",
                        metadata={"pull_requests": [{"merged": True}]},
                    ),
                ),
            ),
        ),
    )
    store.sync_project(completed)
    store.project_canonical_lifecycle(completed)
    regression = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (_pbi(stage=Stage.IMPLEMENT, planning_status="In Progress"),),
            ),
        ),
    )
    result = store.project_canonical_lifecycle(regression)
    assert result[0]["state"] == "completed"
    assert (
        store.canonical_lifecycle_for("project-1", "owner/api", 1)["state"]
        == "completed"
    )  # type: ignore[index]
    assert (
        store.lifecycle_evidence_for("project-1", "owner/api", 1)[-1]["state_after"]
        == "completed"
    )
    store.close()
