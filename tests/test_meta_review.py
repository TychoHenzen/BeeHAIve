import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from conftest import FakeProvider
from fastapi import HTTPException
from fastapi.testclient import TestClient

from beehaiive.meta_review import (
    MAX_META_REVIEW_EVIDENCE_REFS,
    MetaReviewError,
    MetaReviewService,
    _estimate_tokens,
    _mappings,
    _normalize_since,
    _normalize_suggestions,
    _safe_json,
    _safe_text,
    deterministic_analyzer,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelRouter, RoutingError, RoutingStore
from beehaiive.storage import (
    MAX_META_REVIEW_EVENT_DETAILS_LENGTH,
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    OrchestratorStore,
    StoreError,
    _json_list,
    _json_mapping,
)
from main import _handle_meta_review_error, create_app


@pytest.fixture
def stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()


def _seed(store: OrchestratorStore, count: int = 1) -> None:
    store.sync_project(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    tuple(
                        PbiSnapshot("owner/api", number, f"PBI {number}")
                        for number in range(1, count + 1)
                    ),
                ),
            ),
        )
    )


def _complete(
    store: OrchestratorStore,
    pbi_number: int = 1,
    result: str = "completed result",
) -> str:
    run = store.claim_next("project-1", "owner/api", f"worker-{pbi_number}")
    assert run is not None
    lease_token = run.lease_token or ""
    store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    completed = store.complete_agent_run(run.run_id, result, lease_token)
    assert completed.status.value == "completed"
    return completed.run_id


def test_empty_review_reports_supported_missing_evidence(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)

    result = MetaReviewService(store, routing).run("project-1")

    assert result["status"] == "completed"
    assert result["selected_records"] == 0
    assert result["suggestions"] == []
    assert "No completed runs found" in result["missing_evidence"]


def test_review_redacts_bounds_and_preserves_operator_decision(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store, result="token=worker-secret " + "x" * 2_000)
    captured: list[dict[str, object]] = []

    def analyzer(records: list[dict[str, object]]) -> list[dict[str, object]]:
        captured.extend(records)
        return [
            {
                "suggestion_key": "same-evidence",
                "proposed_outcome": "Document the bounded workflow.",
                "rationale": "The completed run is reviewable.",
                "evidence_refs": [records[0]["source_id"]],
            },
            {
                "suggestion_key": "same-evidence",
                "proposed_outcome": "Duplicate",
                "rationale": "Duplicate",
                "evidence_refs": [records[0]["source_id"]],
            },
        ]

    service = MetaReviewService(store, routing, analyzer)
    first = service.run("project-1")
    suggestion = first["suggestions"][0]
    assert "worker-secret" not in str(captured[0]["result"])
    assert len(str(captured[0]["result"])) <= 500
    assert len(service.suggestions("project-1")) == 1

    decided = service.decide("project-1", str(suggestion["suggestion_id"]), "accept")
    assert decided["status"] == "accepted"
    second = service.run("project-1")

    assert second["suggestions"][0]["suggestion_id"] == suggestion["suggestion_id"]
    assert service.suggestions("project-1", "accepted")[0]["status"] == "accepted"


def test_review_uses_routing_failures_and_since_bounds_records(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store, count=2)
    first_run = _complete(store, 1)
    _complete(store, 2)
    router = ModelRouter(routing)
    router.begin(first_run)
    router.record(first_run, "failure", failure_context="retry")
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
    assert any(
        "workflow guard" in str(suggestion["proposed_outcome"])
        for suggestion in result["suggestions"]
    )
    assert f"run:{first_run}: routing attempts unavailable" not in str(
        result["missing_evidence"]
    )

    bounded = service.run("project-1", input_token_limit=1)
    assert bounded["selected_records"] == 0
    assert "Input token limit reached" in bounded["missing_evidence"]


def test_review_reports_analyzer_failures(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store)

    def failing_analyzer(records: object) -> list[object]:
        raise ValueError("token=hidden")

    failed = MetaReviewService(store, routing, failing_analyzer).run("project-1")
    assert failed["status"] == "failed"
    assert "hidden" not in str(failed["error"])


def test_review_rejects_malformed_suggestions_and_evidence(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store)
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
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store)
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
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store)
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


def test_meta_review_store_boundaries_and_decision(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, _routing = stores
    _seed(store)
    run_id = _complete(store)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE events SET details_json = ? WHERE run_id = ?",
            (
                json.dumps({"value": "x" * (MAX_META_REVIEW_EVENT_DETAILS_LENGTH + 1)}),
                run_id,
            ),
        )
    records = store.completed_session_records("project-1", None, 1)
    assert records[0]["events"]
    assert records[0]["events"][0]["details"] == {}
    with pytest.raises(StoreError, match="project id"):
        store.completed_session_records("", None, 1)
    with pytest.raises(StoreError, match="record_limit"):
        store.completed_session_records("project-1", None, 0)
    with pytest.raises(StoreError, match="id and project"):
        store.begin_meta_review("", "project-1", 1, 1)
    with pytest.raises(StoreError, match="Unknown project"):
        store.begin_meta_review("review-missing", "missing", 1, 1)
    with pytest.raises(StoreError, match="Unknown meta-review"):
        store.finish_meta_review("missing", "completed", 0, 0, [])
    with pytest.raises(StoreError, match="Unknown meta-review"):
        store.save_meta_review_suggestions("missing", [])
    with pytest.raises(StoreError, match="project id"):
        store.meta_review_suggestions("", None)
    with pytest.raises(StoreError, match="Invalid suggestion status"):
        store.meta_review_suggestions("project-1", "hold")
    with pytest.raises(StoreError, match="Invalid suggestion decision"):
        store.decide_meta_review_suggestion("project-1", "missing", "pending")
    with pytest.raises(StoreError, match="Unknown meta-review suggestion"):
        store.decide_meta_review_suggestion("project-1", "missing", "accepted")

    store.begin_meta_review("review-1", "project-1", 1, 1)
    with pytest.raises(StoreError, match="already running"):
        store.begin_meta_review("review-2", "project-1", 1, 1)
    with pytest.raises(StoreError, match="Invalid meta-review status"):
        store.finish_meta_review("review-1", "running", 0, 0, [])
    saved = store.save_meta_review_suggestions(
        "review-1",
        [
            {
                "suggestion_id": "suggestion-1",
                "suggestion_key": "key-1",
                "proposed_outcome": "Outcome",
                "rationale": "Reason",
                "evidence_refs": ["run:1"],
            }
        ],
    )
    assert saved[0]["status"] == "pending"
    assert (
        store.finish_meta_review("review-1", "completed", 0, 0, ["missing"])["status"]
        == "completed"
    )
    with pytest.raises(StoreError, match="not running"):
        store.complete_meta_review("review-1", 0, 0, [], [])
    decision = store.decide_meta_review_suggestion(
        "project-1", "suggestion-1", "rejected"
    )
    assert decision["status"] == "rejected"
    assert store.meta_review_suggestions("project-1", "rejected") == [decision]


def test_meta_review_store_reclaims_stale_review_and_enforces_global_overlap(
    tmp_path: Path,
) -> None:
    database = tmp_path / "meta-review.db"
    first = OrchestratorStore(database)
    second = OrchestratorStore(database)
    try:
        _seed(first)
        first.sync_project(
            ProjectSnapshot(
                "project-2",
                "Planning 2",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)
                    ),
                ),
            )
        )
        first.begin_meta_review("stale", "project-1", 1, 1)
        with first._transaction() as connection:
            connection.execute(
                "UPDATE meta_review_runs SET created_at = ? WHERE review_id = ?",
                (
                    (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "stale",
                ),
            )
        second.begin_meta_review("fresh", "project-2", 1, 1)
        with first._lock:
            stale = first._connection.execute(
                "SELECT status, error FROM meta_review_runs WHERE review_id = ?",
                ("stale",),
            ).fetchone()
        assert stale["status"] == "failed"
        assert stale["error"] == "Meta-review lease expired"
        with pytest.raises(StoreError, match="already running"):
            first.begin_meta_review("blocked", "project-1", 1, 1)
    finally:
        first.close()
        second.close()


def test_meta_review_api_requires_key_and_never_calls_provider(
    stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = stores
    _seed(store)
    _complete(store)
    provider = FakeProvider(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)),),
        )
    )
    orchestrator = Orchestrator(store, provider, ModelRouter(routing))
    app = create_app(
        orchestrator=orchestrator,
        meta_review_service=MetaReviewService(store, routing),
        api_key="test-key",
        allowed_project_ids={"project-1"},
    )
    other_store = OrchestratorStore()
    try:
        with pytest.raises(ValueError, match="state store"):
            create_app(
                orchestrator=orchestrator,
                meta_review_service=MetaReviewService(other_store, routing),
                api_key="test-key",
                allowed_project_ids={"project-1"},
            )
    finally:
        other_store.close()

    other_routing = RoutingStore()
    try:
        with pytest.raises(ValueError, match="routing store"):
            create_app(
                orchestrator=orchestrator,
                meta_review_service=MetaReviewService(store, other_routing),
                api_key="test-key",
                allowed_project_ids={"project-1"},
            )
    finally:
        other_routing.close()

    def fail_meta_review() -> dict[str, object]:
        raise StoreError("token=hidden")

    with pytest.raises(HTTPException) as error:
        _handle_meta_review_error(fail_meta_review)
    assert error.value.status_code == 409
    assert error.value.detail == "Meta-review request could not be completed"

    with TestClient(app) as client:
        denied = client.post("/projects/project-1/meta-review")
        assert denied.status_code == 401
        triggered = client.post(
            "/projects/project-1/meta-review",
            headers={"X-API-Key": "test-key"},
            json={},
        )
        assert triggered.status_code == 200, triggered.text
        suggestion = triggered.json()["suggestions"][0]
        listed = client.get("/projects/project-1/meta-review/suggestions")
        assert listed.status_code == 200
        decided = client.post(
            f"/projects/project-1/meta-review/suggestions/{suggestion['suggestion_id']}",
            headers={"X-API-Key": "test-key"},
            json={"decision": "reject"},
        )
        assert decided.status_code == 200
        assert decided.json()["suggestion"]["status"] == "rejected"
    assert provider.discoveries == 0
    assert provider.handoffs == []


def test_meta_review_helpers_keep_json_and_time_bounds() -> None:
    assert _json_mapping('{"key":"value"}') == {"key": "value"}
    assert _json_mapping("not-json") == {}
    assert _json_list("") == []
    assert _json_list("not-json") == []
    assert _safe_text(None) == ""
    assert _safe_text(123) == "123"
    assert "secret" not in _safe_json({"value": "token=secret"})
    redacted = _safe_text(
        '{"access_token":"json-secret","client_secret":"client-secret"} '
        "Authorization: Bearer bearer-secret https://user:url-secret@example.com "
        "password=plain-secret",
        2_000,
    )
    for secret in (
        "json-secret",
        "client-secret",
        "bearer-secret",
        "url-secret",
        "plain-secret",
    ):
        assert secret not in redacted
    circular: list[object] = []
    circular.append(circular)
    assert _safe_json(circular)
    assert _mappings([{"key": "value"}, "bad", None]) == [{"key": "value"}]
    assert _mappings(None) == []
    assert _estimate_tokens({"value": "text"}) >= 1
    assert deterministic_analyzer([{}]) == []
    success = deterministic_analyzer(
        [
            {
                "source_id": "run:1",
                "routing_attempts": [{"attempt_id": 1, "outcome": "success"}],
                "events": [{"event_id": 2}],
            }
        ]
    )
    assert success[0]["suggestion_key"] == "completed-handoff:run:1"
    normalized = _normalize_suggestions(
        "project-1",
        [
            {
                "suggestion_key": "bounded",
                "proposed_outcome": "Outcome",
                "rationale": "Reason",
                "evidence_refs": [f"ref-{index}" for index in range(100)],
            }
        ],
    )
    assert len(normalized[0]["evidence_refs"]) == MAX_META_REVIEW_EVIDENCE_REFS
    assert _normalize_since(None) is None
    assert (
        _normalize_since("2026-01-01T01:00:00+01:00")
        == datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    )
