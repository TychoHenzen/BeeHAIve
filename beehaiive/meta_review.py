"""Bounded review of durable BeeHAIve completion evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from threading import Lock
from typing import cast
from uuid import uuid4

from .agent import redact_worker_text
from .routing import RoutingStore
from .storage import (
    MAX_META_REVIEW_ATTEMPTS,
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    MAX_META_REVIEW_SUGGESTIONS,
    MAX_META_REVIEW_TEXT_LENGTH,
    OrchestratorStore,
    StoreError,
)

MAX_META_REVIEW_EVIDENCE_REFS = 25


class MetaReviewError(StoreError):
    """Raised when a bounded meta-review cannot start or be reviewed."""


Analyzer = Callable[[Sequence[Mapping[str, object]]], Sequence[object]]


class MetaReviewService:
    """Select durable completed runs and persist deterministic suggestions."""

    def __init__(
        self,
        store: OrchestratorStore,
        routing_store: RoutingStore | None = None,
        analyzer: Analyzer | None = None,
    ) -> None:
        self.store = store
        self.routing_store = routing_store
        self._analyzer = analyzer or deterministic_analyzer
        self._lock = Lock()  # ponytail: global lock, per-project locks if needed

    def run(
        self,
        project_id: str,
        *,
        since: str | None = None,
        record_limit: int = MAX_META_REVIEW_RECORDS,
        input_token_limit: int = MAX_META_REVIEW_INPUT_TOKENS,
    ) -> dict[str, object]:
        project_id = project_id.strip()
        self._validate_limits(project_id, record_limit, input_token_limit)
        normalized_since = _normalize_since(since)
        if not self._lock.acquire(blocking=False):
            raise MetaReviewError("A meta-review is already running")

        review_id = f"meta-review-{uuid4()}"
        selected_records = 0
        input_tokens = 0
        missing_evidence: list[str] = []
        try:
            try:
                self.store.begin_meta_review(
                    review_id,
                    project_id,
                    record_limit,
                    input_token_limit,
                )
            except StoreError as exc:
                raise MetaReviewError(str(exc)) from exc
            try:
                source_records = self.store.completed_session_records(
                    project_id, normalized_since, record_limit
                )
                records, missing_evidence, input_tokens = self._bounded_evidence(
                    source_records, input_token_limit
                )
                selected_records = len(records)
                suggestions = _normalize_suggestions(
                    project_id, self._analyzer(records)
                )
                run, saved = self.store.complete_meta_review(
                    review_id,
                    selected_records,
                    input_tokens,
                    missing_evidence,
                    suggestions,
                )
                return _review_result(run, saved)
            except Exception as exc:
                error = _safe_text(str(exc)) or "Meta-review failed"
                with suppress(StoreError):
                    self.store.finish_meta_review(
                        review_id,
                        "failed",
                        selected_records,
                        input_tokens,
                        missing_evidence,
                        error,
                    )
                return {
                    "review_id": review_id,
                    "status": "failed",
                    "selected_records": selected_records,
                    "input_tokens": input_tokens,
                    "missing_evidence": missing_evidence,
                    "error": error,
                    "suggestions": [],
                }
        finally:
            self._lock.release()

    def suggestions(
        self, project_id: str, status: str | None = None
    ) -> list[dict[str, object]]:
        return self.store.meta_review_suggestions(project_id.strip(), status)

    def decide(
        self, project_id: str, suggestion_id: str, decision: str
    ) -> dict[str, object]:
        status = {"accept": "accepted", "reject": "rejected"}.get(decision)
        if status is None:
            raise MetaReviewError("Decision must be accept or reject")
        return self.store.decide_meta_review_suggestion(
            project_id.strip(), suggestion_id.strip(), status
        )

    @staticmethod
    def _validate_limits(
        project_id: str, record_limit: int, input_token_limit: int
    ) -> None:
        if not project_id:
            raise MetaReviewError("A project id is required")
        if not 1 <= record_limit <= MAX_META_REVIEW_RECORDS:
            raise MetaReviewError(
                f"record_limit must be between 1 and {MAX_META_REVIEW_RECORDS}"
            )
        if not 1 <= input_token_limit <= MAX_META_REVIEW_INPUT_TOKENS:
            raise MetaReviewError(
                "input_token_limit must be between 1 and "
                f"{MAX_META_REVIEW_INPUT_TOKENS}"
            )

    def _bounded_evidence(
        self,
        source_records: Sequence[object],
        input_token_limit: int,
    ) -> tuple[list[dict[str, object]], list[str], int]:
        if not source_records:
            return [], ["No completed runs found"], 0
        records: list[dict[str, object]] = []
        missing: list[str] = []
        input_tokens = 0
        for source_record in source_records:
            if not isinstance(source_record, Mapping):
                missing.append("Malformed completed session record")
                continue
            source_record = cast(Mapping[str, object], source_record)
            source_id = source_record.get("source_id")
            run_id = source_record.get("run_id")
            if not isinstance(source_id, str) or not source_id.strip():
                missing.append("Malformed completed session record")
                continue
            if not isinstance(run_id, str) or not run_id.strip():
                missing.append(f"{source_id}: run identifier unavailable")
                continue
            if not any(
                isinstance(value, str) and value.strip()
                for value in (source_record.get("result"), source_record.get("error"))
            ):
                missing.append(f"{source_id}: completion result or error unavailable")
                continue
            raw_events = source_record.get("events")
            if not _mappings(raw_events):
                missing.append(f"{source_id}: lifecycle events unavailable")
            if self.routing_store is None:
                attempts: list[dict[str, object]] = []
                missing.append(f"{source_id}: routing attempts unavailable")
            else:
                attempts = [
                    attempt.as_dict()
                    for attempt in self.routing_store.get_attempts(
                        run_id, limit=MAX_META_REVIEW_ATTEMPTS
                    )
                ]
                if not attempts:
                    missing.append(f"{source_id}: routing attempts unavailable")
            record = _safe_record(source_record, attempts)
            record_tokens = _estimate_tokens(record)
            if input_tokens + record_tokens > input_token_limit:
                missing.append("Input token limit reached")
                break
            records.append(record)
            input_tokens += record_tokens
        return records, missing, input_tokens


def deterministic_analyzer(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Produce one traceable workflow suggestion per supported run record."""

    suggestions: list[dict[str, object]] = []
    for record in records:
        source_id = str(record.get("source_id", "")).strip()
        if not source_id:
            continue
        attempts = _mappings(record.get("routing_attempts"))
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
            for event in _mappings(record.get("events"))[:10]
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


def _normalize_suggestions(
    project_id: str, raw_suggestions: Sequence[object]
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_suggestions[:MAX_META_REVIEW_SUGGESTIONS]:
        if not isinstance(raw, Mapping):
            raise MetaReviewError("Analyzer returned an invalid suggestion")
        raw = cast(Mapping[str, object], raw)
        key = _safe_text(raw.get("suggestion_key"), 160)
        outcome = _safe_text(raw.get("proposed_outcome"))
        rationale = _safe_text(raw.get("rationale"))
        raw_refs = raw.get("evidence_refs")
        refs: list[str] = []
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str):
            for ref in cast(Sequence[object], raw_refs)[:MAX_META_REVIEW_EVIDENCE_REFS]:
                safe_ref = _safe_text(ref, 160)
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


def _safe_record(
    source_record: Mapping[str, object], attempts: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    source_id = str(source_record["source_id"])
    events = [
        {
            "event_id": event.get("id"),
            "type": _safe_text(event.get("type"), 80),
            "from_stage": _safe_text(event.get("from_stage"), 80),
            "to_stage": _safe_text(event.get("to_stage"), 80),
            "created_at": _safe_text(event.get("created_at"), 80),
            "details": _safe_json(event.get("details")),
        }
        for event in _mappings(source_record.get("events"))[:20]
    ]
    safe_attempts = [
        {
            "attempt_id": attempt.get("attempt_id"),
            "round": attempt.get("round"),
            "model": _safe_text(attempt.get("model"), 80),
            "tier": _safe_text(attempt.get("tier"), 80),
            "outcome": _safe_text(attempt.get("outcome"), 80),
            "failure_context": _safe_text(attempt.get("failure_context"), 500),
            "created_at": _safe_text(attempt.get("created_at"), 80),
        }
        for attempt in attempts[:20]
    ]
    return {
        "source_id": source_id,
        "run_id": source_id.removeprefix("run:"),
        "repository": _safe_text(source_record.get("repository"), 200),
        "pbi_number": source_record.get("pbi_number"),
        "title": _safe_text(source_record.get("title"), 300),
        "status": _safe_text(source_record.get("status"), 40),
        "attempt": source_record.get("attempt"),
        "result": _safe_text(source_record.get("result"), MAX_META_REVIEW_TEXT_LENGTH),
        "error": _safe_text(source_record.get("error"), MAX_META_REVIEW_TEXT_LENGTH),
        "updated_at": _safe_text(source_record.get("updated_at"), 80),
        "events": events,
        "routing_attempts": safe_attempts,
    }


def _review_result(
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


def _normalize_since(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetaReviewError("since must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MetaReviewError("since must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _safe_text(value: object, limit: int = MAX_META_REVIEW_TEXT_LENGTH) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return redact_worker_text(text)[:limit]


def _safe_json(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return _safe_text(encoded)


def _estimate_tokens(record: Mapping[str, object]) -> int:
    return max(
        1,
        len(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)) // 4,
    )


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        dict(cast(Mapping[str, object], item))
        for item in cast(Sequence[object], value)
        if isinstance(item, Mapping)
    ]
