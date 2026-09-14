from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from beehaiive.pbi_creation import (
    PbiCreationConflictError,
    PbiCreationRequest,
    PbiCreationService,
    PbiCreationValidationError,
)
from beehaiive.storage import OrchestratorStore
from tests.support.fake_pbi_creation_provider import FakePbiCreationProvider


def make_request(
    body: str = "A body", labels: tuple[str, ...] = ()
) -> PbiCreationRequest:
    return PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body=body,
        labels=labels,
    )


def test_same_key_replays_result_and_conflicts_on_changed_content() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    service = PbiCreationService(store, provider)
    request = make_request()

    created = service.create(request, "request-1")
    replay = service.create(request, "request-1")

    assert created["status"] == "complete"
    assert replay == created
    assert provider.issue_posts == 1
    with pytest.raises(PbiCreationConflictError):
        service.create(make_request("Changed body"), "request-1")
    store.close()


def test_known_partial_issue_resumes_after_store_restart(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = OrchestratorStore(database)
    provider = FakePbiCreationProvider()
    provider.fail_after_issue = True
    request = make_request(labels=("enhancement",))

    incomplete = PbiCreationService(store, provider).create(request, "request-2")
    assert incomplete["status"] == "incomplete"
    assert incomplete["issue"] == {
        "id": "issue-node-id",
        "number": 37,
        "url": "https://example.test/issues/37",
    }
    assert "private-provider-detail" not in str(incomplete)
    store.close()

    restarted_store = OrchestratorStore(database)
    resumed = PbiCreationService(restarted_store, provider).create(request, "request-2")

    assert resumed["status"] == "complete"
    assert provider.issue_posts == 1
    restarted_store.close()


def test_ambiguous_create_never_retries_without_a_saved_issue_identity() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.unknown_create = True
    service = PbiCreationService(store, provider)
    request = make_request()

    first = service.create(request, "request-3")
    replay = service.create(request, "request-3")

    assert first["status"] == "outcome_unknown"
    assert first["issue"] is None
    assert first["operator_action_required"] is True
    assert replay == first
    assert provider.issue_posts == 1
    assert "private-provider-detail" not in str(first)
    store.close()


def test_expired_create_marker_after_restart_becomes_outcome_unknown(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = OrchestratorStore(database)
    provider = FakePbiCreationProvider()
    provider.crash_after_create_marker = True
    request = make_request()
    with pytest.raises(SystemExit, match="simulated process stop"):
        PbiCreationService(store, provider).create(request, "request-4")

    with store._lock:
        store._connection.execute(
            "UPDATE pbi_creations SET lease_expires_at = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    store.close()

    restarted_store = OrchestratorStore(database)
    result = PbiCreationService(restarted_store, provider).create(request, "request-4")

    assert result["status"] == "outcome_unknown"
    assert result["issue"] is None
    assert provider.issue_posts == 1
    assert provider.prepare_calls == 1
    restarted_store.close()


def test_concurrent_same_key_request_does_not_start_a_second_create() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.entered = Event()
    provider.release = Event()
    service = PbiCreationService(store, provider)
    request = make_request()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def create_first() -> None:
        try:
            results.append(service.create(request, "request-5"))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=create_first)
    worker.start()
    assert provider.entered.wait(timeout=2)
    concurrent = service.create(request, "request-5")
    provider.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert concurrent["status"] == "in_progress"
    assert results[0]["status"] == "complete"
    assert provider.issue_posts == 1
    store.close()


def test_unknown_label_is_rejected_before_any_issue_write() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.reject_labels = True

    with pytest.raises(PbiCreationValidationError, match="Unknown label"):
        PbiCreationService(store, provider).create(
            make_request(labels=("missing",)), "request-6"
        )

    assert provider.issue_posts == 0
    store.close()
