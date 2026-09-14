from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from beehaiive.meta_review import (
    MAX_META_REVIEW_EVIDENCE_REFS,
    _estimate_tokens,
    _mappings,
    _normalize_since,
    _normalize_suggestions,
    _safe_json,
    _safe_text,
    deterministic_analyzer,
)
from beehaiive.routing import RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
    _json_list,
    _json_mapping,
)


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


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
