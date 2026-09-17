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


def deterministic_analyzer(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Produce one traceable workflow suggestion per supported run record."""

    suggestions: list[dict[str, object]] = []
    for record in records:
        source_id = str(record.get("source_id", "")).strip()
        if not source_id:
            continue
        attempts = mappings(record.get("routing_attempts"))
        failures = sum(
            1 for attempt in attempts if attempt.get("outcome") in {"failure", "retry"}
        )
        if failures:
            key = f"routing-failures:{source_id}"
            outcome = "Add a workflow guard for repeated routing failures."
            rationale = (
                f"{source_id} recorded {failures} failed or retry routing attempts "
                "before completion."
            )
        elif not attempts:
            key = f"missing-routing-evidence:{source_id}"
            outcome = "Preserve routing evidence for completed workflow runs."
            rationale = (
                f"{source_id} completed without a durable routing attempt record."
            )
        else:
            key = f"completed-handoff:{source_id}"
            outcome = "Document the verified workflow handoff path."
            rationale = (
                f"{source_id} completed with bounded routing evidence available."
            )
        evidence_refs = [source_id]
        evidence_refs.extend(
            f"{source_id}:event:{event.get('event_id')}"
            for event in mappings(record.get("events"))[:10]
            if event.get("event_id") is not None
        )
        evidence_refs.extend(
            f"{source_id}:attempt:{attempt.get('attempt_id')}"
            for attempt in attempts[:10]
            if attempt.get("attempt_id") is not None
        )
        suggestions.append(
            {
                "suggestion_key": key,
                "proposed_outcome": outcome,
                "rationale": rationale,
                "evidence_refs": evidence_refs,
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
