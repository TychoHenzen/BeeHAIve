from pathlib import Path

import pytest

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    ProviderError,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot
from tests.support.orchestrator.invalid_handoff_provider import (
    InvalidHandoffProvider as InvalidHandoffProvider,
)


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
