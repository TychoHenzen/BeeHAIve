from __future__ import annotations

import pytest

from beehaiive.models import (
    HandoffIntent,
    HandoffRequest,
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    ProviderError,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.edges.error_handoff_client import (
    ErrorHandoffClient as ErrorHandoffClient,
)
from tests.support.edges.helpers import handoff_request as _handoff_request
from tests.support.edges.helpers import storage_snapshot as _storage_snapshot
from tests.support.edges.storage_provider import StorageProvider as StorageProvider


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
            HandoffRequest("owner:7", "invalid", 1, "bad", "branch", None, "", "run-1")
        )
    with pytest.raises(ProviderError, match="branch name"):
        malformed.validate_handoff("owner/api", "bad branch", None)


def test_orchestrator_handoff_rejects_unknown_and_handles_completed_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    with pytest.raises(StoreError, match="Unknown run"):
        service.handoff("missing", "branch", None, "body", "missing-token")

    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)
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
        owner_id=run.owner_id,
        lease_token=run.lease_token,
        lease_expires_at=run.lease_expires_at,
    )
    monkeypatch.setattr(
        store,
        "prepare_handoff",
        lambda *args: HandoffIntent(completed, "branch", "main", "body"),
    )
    assert service.handoff(run.run_id, "branch", None, "body", lease_token) == completed
    store.close()
