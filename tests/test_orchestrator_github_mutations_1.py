from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.orchestrator.concurrent_handoff_provider import (
    ConcurrentHandoffProvider as ConcurrentHandoffProvider,
)
from tests.support.orchestrator.crash_after_external_handoff_provider import (
    CrashAfterExternalHandoffProvider as CrashAfterExternalHandoffProvider,
)
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot
from tests.support.orchestrator.shared_idempotent_handoff_provider import (
    SharedIdempotentHandoffProvider as SharedIdempotentHandoffProvider,
)


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
