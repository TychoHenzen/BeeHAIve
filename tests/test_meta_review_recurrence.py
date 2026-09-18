from __future__ import annotations

import json

from beehaiive.reviews.meta_review_helpers import (
    deterministic_analyzer,
    normalize_suggestions,
    recurrence_gate,
    supported_patterns,
)


def record(
    run_id: str,
    *,
    repository: str = "Owner/API",
    status: str = "completed",
    failure: bool = True,
    retry: bool = True,
    question: bool = True,
    execution_failure: bool = True,
) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    if failure:
        attempts.append({"attempt_id": 1, "problem_id": run_id, "outcome": "failure"})
    if retry:
        attempts.append({"attempt_id": 2, "problem_id": run_id, "outcome": "retry"})
    events = [{"event_id": 3, "type": "operator_question_created"}] if question else []
    transcript_events = (
        [
            {
                "source_id": f"run:{run_id}:transcript:4",
                "sequence": 4,
                "kind": "progress",
                "source_type": "turn.failed",
                "role": None,
                "text": "turn.failed",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]
        if execution_failure
        else []
    )
    return {
        "source_id": f"run:{run_id}",
        "run_id": run_id,
        "project_id": "project-1",
        "repository": repository,
        "status": status,
        "routing_attempts": attempts,
        "events": events,
        "transcript": {"events": transcript_events, "gaps": []},
    }


def test_one_run_and_duplicate_copies_count_once() -> None:
    item = record("one")
    patterns, diagnostics = supported_patterns(
        [item, json.loads(json.dumps(item))], "project-1", require_completed=True
    )

    assert diagnostics == []
    assert all(len(pattern["runs"]) == 1 for pattern in patterns.values())
    suggestions = deterministic_analyzer([item])
    assert suggestions == []


def test_two_runs_emit_all_structured_families_with_stable_references() -> None:
    records = [record("one"), record("two")]
    raw = deterministic_analyzer(records)
    normalized = normalize_suggestions("project-1", raw)
    suggestions, diagnostics = recurrence_gate(
        "project-1",
        records,
        normalized,
        since="2026-01-01T00:00:00+00:00",
        record_limit=25,
    )

    assert diagnostics == []
    assert {suggestion["suggestion_key"] for suggestion in suggestions} == {
        "meta-review:v1:owner/api:routing-failure",
        "meta-review:v1:owner/api:repair-retry",
        "meta-review:v1:owner/api:blocked-question",
        "meta-review:v1:owner/api:execution-failure",
    }
    for suggestion in suggestions:
        references = suggestion["evidence_refs"]
        assert len(references) <= 25
        assert {
            str(reference).split(":")[1]
            for reference in references
            if str(reference).startswith("run:")
        } == {
            "one",
            "two",
        }
        assert "qualifying_runs=2" in str(suggestion["rationale"])
        assert "required_runs=2" in str(suggestion["rationale"])


def test_analyzer_output_cannot_create_cross_repository_or_underthreshold_pattern() -> (
    None
):
    records = [
        record(
            "one",
            repository="owner/api",
            retry=False,
            question=False,
            execution_failure=False,
        ),
        record(
            "two",
            repository="other/api",
            failure=False,
            retry=False,
            question=False,
            execution_failure=False,
        ),
    ]
    normalized = normalize_suggestions(
        "project-1",
        [
            {
                "suggestion_key": "meta-review:v1:owner/api:routing-failure",
                "proposed_outcome": "forged",
                "rationale": "forged count=999",
                "evidence_refs": ["run:one", "run:two"],
            }
        ],
    )

    suggestions, diagnostics = recurrence_gate(
        "project-1", records, normalized, since=None, record_limit=25
    )

    assert suggestions == []
    assert any("insufficient support" in diagnostic for diagnostic in diagnostics)
    assert "forged" not in json.dumps(suggestions)


def test_malformed_and_noncompleted_provenance_never_qualify() -> None:
    records = [
        record("one", status="active"),
        {
            **record("two"),
            "source_id": "run:other",
            "routing_attempts": [
                {"attempt_id": 1, "problem_id": "two", "outcome": "failure"}
            ],
        },
        record(
            "three", failure=False, retry=False, question=False, execution_failure=False
        ),
    ]

    patterns, diagnostics = supported_patterns(
        records, "project-1", require_completed=True
    )

    assert patterns == {}
    assert diagnostics
