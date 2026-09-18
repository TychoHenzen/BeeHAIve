from collections.abc import Iterator
from threading import Event, Thread

import pytest

from beehaiive.meta_review import (
    MetaReviewError,
    MetaReviewService,
    _normalize_suggestions,
)
from beehaiive.routing import ModelRouter, RoutingError, RoutingStore
from beehaiive.storage import (
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    OrchestratorStore,
)
from tests.support.meta_review.helpers import complete_meta_review, seed_meta_review


def test_empty_review_reports_supported_missing_evidence(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)

    result = MetaReviewService(store, routing).run("project-1")

    assert result["status"] == "completed"
    assert result["selected_records"] == 0
    assert result["suggestions"] == []
    assert "No completed runs found" in result["missing_evidence"]


def test_review_uses_routing_failures_and_since_bounds_records(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store, count=2)
    first_run = complete_meta_review(store, 1)
    second_run = complete_meta_review(store, 2)
    router = ModelRouter(routing)
    for run_id in (first_run, second_run):
        router.begin(run_id)
        router.record(run_id, "failure", failure_context="retry")
    assert routing.get_attempts(first_run)
    assert len(routing.get_attempts(first_run, limit=1)) == 1
    with pytest.raises(RoutingError, match="attempt limit"):
        routing.get_attempts(first_run, limit=0)
    service = MetaReviewService(store, routing)

    result = service.run(
        "project-1",
        since="2020-01-01T00:00:00+00:00",
        record_limit=2,
        input_token_limit=MAX_META_REVIEW_INPUT_TOKENS,
    )

    assert result["selected_records"] == 2
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["suggestion_key"] == (
        "meta-review:v1:owner/api:routing-failure"
    )
    assert f"run:{first_run}: routing attempts unavailable" not in str(
        result["missing_evidence"]
    )

    bounded = service.run("project-1", input_token_limit=1)
    assert bounded["selected_records"] == 0
    assert "Input token limit reached" in bounded["missing_evidence"]


def test_review_reports_analyzer_failures(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    complete_meta_review(store)

    def failing_analyzer(records: object) -> list[object]:
        raise ValueError("token=hidden")

    failed = MetaReviewService(store, routing, failing_analyzer).run("project-1")
    assert failed["status"] == "failed"
    assert "hidden" not in str(failed["error"])


def test_review_rejects_malformed_suggestions_and_evidence(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    complete_meta_review(store)
    malformed = MetaReviewService(store, routing, lambda records: [object()]).run(
        "project-1"
    )
    assert malformed["status"] == "failed"
    assert "invalid suggestion" in str(malformed["error"])

    service = MetaReviewService(store, routing)
    records, missing, tokens = service._bounded_evidence([object()], 100)
    assert records == []
    assert missing == ["Malformed completed session record"]
    assert tokens == 0
    _, missing, _ = service._bounded_evidence(
        [{"source_id": "", "run_id": "run"}, {"source_id": "run:x"}], 100
    )
    assert "Malformed completed session record" in missing
    assert "run:x: run identifier unavailable" in missing
    records, missing, _ = MetaReviewService(store)._bounded_evidence(
        [
            {
                "source_id": "run:x",
                "run_id": "x",
                "result": "completed",
                "events": [],
            }
        ],
        1_000,
    )
    assert records
    assert "run:x: lifecycle events unavailable" in missing
    assert "run:x: routing attempts unavailable" in missing
    records, missing, _ = service._bounded_evidence(
        [
            {
                "source_id": "run:incomplete",
                "run_id": "incomplete",
                "result": "",
                "error": None,
                "events": [],
            }
        ],
        1_000,
    )
    assert records == []
    assert "run:incomplete: completion result or error unavailable" in missing
    with pytest.raises(MetaReviewError, match="incomplete suggestion"):
        _normalize_suggestions("project-1", [{}])


def test_review_validates_project_and_request_inputs(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    complete_meta_review(store)
    service = MetaReviewService(store, routing)
    with pytest.raises(MetaReviewError, match="Unknown project"):
        service.run("missing")
    with pytest.raises(MetaReviewError, match="project id"):
        service.run(" ")
    with pytest.raises(MetaReviewError, match="ISO-8601"):
        service.run("project-1", since="not-a-date")
    with pytest.raises(MetaReviewError, match="timezone"):
        service.run("project-1", since="2026-01-01T00:00:00")
    with pytest.raises(MetaReviewError, match="record_limit"):
        service.run("project-1", record_limit=MAX_META_REVIEW_RECORDS + 1)
    with pytest.raises(MetaReviewError, match="input_token_limit"):
        service.run("project-1", input_token_limit=MAX_META_REVIEW_INPUT_TOKENS + 1)
    with pytest.raises(MetaReviewError, match="Decision"):
        service.decide("project-1", "missing", "hold")


def test_review_rejects_overlap_and_invalid_inputs(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    complete_meta_review(store)
    started = Event()
    release = Event()

    def blocking_analyzer(records: list[dict[str, object]]) -> list[object]:
        started.set()
        assert release.wait(2)
        return []

    service = MetaReviewService(store, routing, blocking_analyzer)
    thread_result: dict[str, object] = {}

    def run_review() -> None:
        try:
            thread_result["result"] = service.run("project-1")
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            thread_result["error"] = exc

    thread = Thread(target=run_review, daemon=True)
    thread.start()
    assert started.wait(2)
    with pytest.raises(MetaReviewError, match="already running"):
        service.run("project-1")
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert "error" not in thread_result
    assert isinstance(thread_result.get("result"), dict)


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
