from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from ..agent import redact_worker_text
from ..session_evidence import transcript_projection
from ..storage import (
    MAX_META_REVIEW_SUGGESTIONS,
    MAX_META_REVIEW_TEXT_LENGTH,
)
from .meta_review_error import MetaReviewError
from .meta_review_types import MAX_META_REVIEW_EVIDENCE_REFS

RECURRENCE_THRESHOLD = 2
MAX_META_REVIEW_DIAGNOSTICS = 20


def deterministic_analyzer(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Produce one traceable suggestion per recurring structured pattern."""

    patterns, _ = supported_patterns(records)
    suggestions: list[dict[str, object]] = []
    outcomes = {
        "routing-failure": "Add a workflow guard for repeated routing failures.",
        "repair-retry": "Document or automate a repair path for repeated retries.",
        "blocked-question": (
            "Improve the workflow path that repeatedly blocks on operator questions."
        ),
        "execution-failure": "Add a recovery path for repeated execution failures.",
    }
    for key, pattern in patterns.items():
        runs = cast(dict[str, list[str]], pattern["runs"])
        if len(runs) < RECURRENCE_THRESHOLD:
            continue
        family = str(pattern["family"])
        suggestions.append(
            {
                "suggestion_key": key,
                "proposed_outcome": outcomes[family],
                "rationale": (
                    f"The {family} pattern appears in {len(runs)} distinct completed "
                    "runs."
                ),
                "evidence_refs": pattern_references(pattern),
            }
        )
    return suggestions[:MAX_META_REVIEW_SUGGESTIONS]


def normalize_suggestions(
    project_id: str, raw_suggestions: Sequence[object]
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_suggestions[:MAX_META_REVIEW_SUGGESTIONS]:
        if not isinstance(raw, Mapping):
            raise MetaReviewError("Analyzer returned an invalid suggestion")
        raw = cast(Mapping[str, object], raw)
        key = safe_review_text(raw.get("suggestion_key"), 160)
        outcome = safe_review_text(raw.get("proposed_outcome"))
        rationale = safe_review_text(raw.get("rationale"))
        raw_refs = raw.get("evidence_refs")
        refs: list[str] = []
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str):
            for ref in cast(Sequence[object], raw_refs)[:MAX_META_REVIEW_EVIDENCE_REFS]:
                safe_ref = safe_review_text(ref, 160)
                if safe_ref:
                    refs.append(safe_ref)
        if not key or not outcome or not rationale or not refs:
            raise MetaReviewError("Analyzer returned an incomplete suggestion")
        digest = hashlib.sha256(f"{project_id}\0{key}".encode()).hexdigest()[:24]
        suggestion_id = f"suggestion-{digest}"
        if suggestion_id in seen:
            continue
        seen.add(suggestion_id)
        normalized.append(
            {
                "suggestion_id": suggestion_id,
                "suggestion_key": key,
                "proposed_outcome": outcome,
                "rationale": rationale,
                "evidence_refs": refs,
            }
        )
    return normalized


def recurrence_gate(
    project_id: str,
    records: Sequence[Mapping[str, object]],
    suggestions: Sequence[Mapping[str, object]],
    *,
    since: str | None,
    record_limit: int,
) -> tuple[list[dict[str, object]], list[str]]:
    patterns, diagnostics = supported_patterns(
        records, project_id, require_completed=True
    )
    qualified = {
        key: pattern
        for key, pattern in patterns.items()
        if len(cast(dict[str, list[str]], pattern["runs"])) >= RECURRENCE_THRESHOLD
    }
    for key, pattern in patterns.items():
        count = len(cast(dict[str, list[str]], pattern["runs"]))
        if count < RECURRENCE_THRESHOLD:
            diagnostics.append(
                f"{key}: insufficient support (qualifying_runs={count}, "
                f"required_runs={RECURRENCE_THRESHOLD})"
            )
    if not qualified and records:
        diagnostics.append(
            "No supported recurring pattern reached "
            f"required_runs={RECURRENCE_THRESHOLD}"
        )

    reference_keys: dict[str, set[str]] = {}
    for key, pattern in patterns.items():
        runs = cast(dict[str, list[str]], pattern["runs"])
        for references in runs.values():
            for reference in references:
                reference_keys.setdefault(reference, set()).add(key)

    gated: list[dict[str, object]] = []
    seen: set[str] = set()
    window = (
        f"since={since or 'none'}, record_limit={record_limit}, "
        f"admitted_records={len(records)}"
    )
    for suggestion in suggestions:
        raw_key = safe_review_text(suggestion.get("suggestion_key"), 160)
        candidates: set[str] = {raw_key} if raw_key in qualified else set()
        for reference in _suggestion_references(suggestion):
            matching = reference_keys.get(reference)
            if matching:
                candidates.update(matching)
        candidates.intersection_update(qualified)
        if len(candidates) != 1:
            diagnostics.append(
                "Analyzer suggestion excluded: no single qualifying structured pattern"
            )
            continue
        key = next(iter(candidates))
        if key in seen:
            continue
        pattern = qualified[key]
        runs = cast(dict[str, list[str]], pattern["runs"])
        item = dict(suggestion)
        item["suggestion_key"] = key
        item["suggestion_id"] = suggestion_id(project_id, key)
        item["evidence_refs"] = pattern_references(pattern)
        note = (
            f"Recurrence evidence: qualifying_runs={len(runs)}; "
            f"required_runs={RECURRENCE_THRESHOLD}; window={window}."
        )
        prefix = safe_review_text(
            item["rationale"], max(0, MAX_META_REVIEW_TEXT_LENGTH - len(note) - 1)
        )
        item["rationale"] = f"{prefix} {note}"[:MAX_META_REVIEW_TEXT_LENGTH]
        gated.append(item)
        seen.add(key)
    return gated[:MAX_META_REVIEW_SUGGESTIONS], bounded_diagnostics(diagnostics)


def supported_patterns(
    records: Sequence[Mapping[str, object]],
    expected_project: str | None = None,
    *,
    require_completed: bool = False,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    patterns: dict[str, dict[str, object]] = {}
    diagnostics: list[str] = []
    seen_runs: set[str] = set()
    for record in records:
        run_id = valid_record_run_id(record)
        repository = normalized_repository(record.get("repository"))
        if run_id is None or not repository:
            diagnostics.append("Malformed or unverifiable completed-run provenance")
            continue
        if require_completed and record.get("status") != "completed":
            diagnostics.append(
                f"{record.get('source_id', run_id)}: non-completed run excluded"
            )
            continue
        if (
            expected_project is not None
            and record.get("project_id") != expected_project
        ):
            diagnostics.append(
                f"{record.get('source_id', run_id)}: Project ownership mismatch"
            )
            continue
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        source_id = f"run:{run_id}"
        for family, references in (
            ("routing-failure", _attempt_references(record, run_id, "failure")),
            ("repair-retry", _attempt_references(record, run_id, "retry")),
            ("blocked-question", _event_references(record, run_id)),
            ("execution-failure", _transcript_failure_references(record, run_id)),
        ):
            if not references:
                continue
            key = pattern_key(repository, family)
            pattern = patterns.setdefault(
                key,
                {
                    "repository": repository,
                    "family": family,
                    "runs": {},
                    "context_refs": [],
                },
            )
            runs = cast(dict[str, list[str]], pattern["runs"])
            run_references = runs.setdefault(run_id, [])
            for reference in references:
                if reference not in run_references:
                    run_references.append(reference)
            context_refs = cast(list[str], pattern["context_refs"])
            for reference in (source_id, *references):
                if reference not in context_refs:
                    context_refs.append(reference)
    return patterns, bounded_diagnostics(diagnostics)


def pattern_references(pattern: Mapping[str, object]) -> list[str]:
    runs = cast(dict[str, list[str]], pattern["runs"])
    primary = [references[0] for references in runs.values() if references]
    supplementary = [
        reference for references in runs.values() for reference in references[1:]
    ]
    supplementary.extend(cast(list[str], pattern.get("context_refs", [])))
    return _unique(primary + supplementary)[:MAX_META_REVIEW_EVIDENCE_REFS]


def pattern_key(repository: str, family: str) -> str:
    return f"meta-review:v1:{repository}:{family}"


def suggestion_id(project_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{project_id}\0{key}".encode()).hexdigest()[:24]
    return f"suggestion-{digest}"


def normalized_repository(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "/".join(
        part.strip().casefold() for part in value.strip().split("/") if part.strip()
    )


def valid_record_run_id(record: Mapping[str, object]) -> str | None:
    source_id = record.get("source_id")
    run_id = record.get("run_id")
    if not isinstance(source_id, str) or not isinstance(run_id, str):
        return None
    try:
        parsed = source_run_id(source_id)
    except MetaReviewError:
        return None
    return run_id.strip() if parsed == run_id.strip() and parsed else None


def _attempt_references(
    record: Mapping[str, object], run_id: str, outcome: str
) -> list[str]:
    references: list[str] = []
    for attempt in mappings(record.get("routing_attempts")):
        attempt_id = attempt.get("attempt_id")
        if (
            attempt.get("problem_id") != run_id
            or attempt.get("outcome") != outcome
            or type(attempt_id) is not int
            or attempt_id <= 0
        ):
            continue
        reference = f"run:{run_id}:attempt:{attempt_id}"
        if reference not in references:
            references.append(reference)
    return references


def _event_references(record: Mapping[str, object], run_id: str) -> list[str]:
    references: list[str] = []
    for event in mappings(record.get("events")):
        event_id = event.get("event_id")
        if (
            event.get("type") != "operator_question_created"
            or type(event_id) is not int
            or event_id <= 0
        ):
            continue
        reference = f"run:{run_id}:event:{event_id}"
        if reference not in references:
            references.append(reference)
    return references


def _transcript_failure_references(
    record: Mapping[str, object], run_id: str
) -> list[str]:
    transcript = record.get("transcript")
    if not isinstance(transcript, Mapping):
        return []
    references: list[str] = []
    transcript_events = cast(Mapping[str, object], transcript).get("events")
    for event in mappings(transcript_events):
        sequence = event.get("sequence")
        reference = event.get("source_id")
        if (
            event.get("kind") != "progress"
            or event.get("source_type") != "turn.failed"
            or type(sequence) is not int
            or sequence <= 0
            or reference != f"run:{run_id}:transcript:{sequence}"
        ):
            continue
        if isinstance(reference, str) and reference not in references:
            references.append(reference)
    return references


def _suggestion_references(suggestion: Mapping[str, object]) -> list[str]:
    raw = suggestion.get("evidence_refs")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    return [value for value in cast(Sequence[object], raw) if isinstance(value, str)]


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def bounded_diagnostics(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))[:MAX_META_REVIEW_DIAGNOSTICS]


def safe_record(
    source_record: Mapping[str, object], attempts: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    source_id = safe_review_text(source_record["source_id"], 124)
    raw_transcript = source_record.get("transcript")
    transcript: Mapping[str, object] = (
        cast(Mapping[str, object], raw_transcript)
        if isinstance(raw_transcript, Mapping)
        else {}
    )
    events = [
        {
            "event_id": event.get("id"),
            "type": safe_review_text(event.get("type"), 80),
            "from_stage": safe_review_text(event.get("from_stage"), 80),
            "to_stage": safe_review_text(event.get("to_stage"), 80),
            "created_at": safe_review_text(event.get("created_at"), 80),
            "details": safe_json(event.get("details")),
        }
        for event in mappings(source_record.get("events"))[:20]
    ]
    safe_attempts = [
        {
            "attempt_id": attempt.get("attempt_id"),
            "problem_id": safe_review_text(attempt.get("problem_id"), 120),
            "round": attempt.get("round"),
            "model": safe_review_text(attempt.get("model"), 80),
            "tier": safe_review_text(attempt.get("tier"), 80),
            "outcome": safe_review_text(attempt.get("outcome"), 80),
            "failure_context": safe_review_text(attempt.get("failure_context"), 500),
            "created_at": safe_review_text(attempt.get("created_at"), 80),
        }
        for attempt in attempts[:20]
    ]
    return {
        "source_id": source_id,
        "run_id": source_id.removeprefix("run:"),
        "project_id": safe_review_text(source_record.get("project_id"), 120),
        "repository": safe_review_text(source_record.get("repository"), 200),
        "pbi_number": source_record.get("pbi_number"),
        "title": safe_review_text(source_record.get("title"), 300),
        "status": safe_review_text(source_record.get("status"), 40),
        "attempt": source_record.get("attempt"),
        "result": safe_review_text(
            source_record.get("result"), MAX_META_REVIEW_TEXT_LENGTH
        ),
        "error": safe_review_text(
            source_record.get("error"), MAX_META_REVIEW_TEXT_LENGTH
        ),
        "updated_at": safe_review_text(source_record.get("updated_at"), 80),
        "events": events,
        "routing_attempts": safe_attempts,
        "transcript": transcript_projection(
            source_id.removeprefix("run:"),
            transcript.get("events"),
            transcript.get("gaps"),
        ),
    }


def review_result(
    run: Mapping[str, object], suggestions: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    return {
        "review_id": run.get("review_id"),
        "status": run.get("status"),
        "selected_records": run.get("selected_records", 0),
        "input_tokens": run.get("input_tokens", 0),
        "missing_evidence": run.get("missing_evidence", []),
        "suggestions": [dict(suggestion) for suggestion in suggestions],
    }


def normalize_since(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetaReviewError("since must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MetaReviewError("since must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def safe_review_text(value: object, limit: int = MAX_META_REVIEW_TEXT_LENGTH) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return redact_worker_text(text)[:limit]


def source_run_id(reference: str) -> str | None:
    if not reference.startswith("run:"):
        return None
    parts = reference.split(":")
    if len(parts) == 2 and parts[1]:
        return parts[1]
    if (
        len(parts) == 4
        and parts[2] == "transcript"
        and parts[1]
        and parts[3].isascii()
        and parts[3].isdigit()
        and int(parts[3]) > 0
    ):
        return parts[1]
    if len(parts) == 4 and parts[2] in {"event", "attempt"} and parts[1] and parts[3]:
        return parts[1]
    raise MetaReviewError("Accepted suggestion contains an invalid run reference")


def safe_json(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return safe_review_text(encoded)


def estimate_tokens(record: Mapping[str, object]) -> int:
    return max(
        1,
        len(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)) // 4,
    )


def mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        dict(cast(Mapping[str, object], item))
        for item in cast(Sequence[object], value)
        if isinstance(item, Mapping)
    ]
