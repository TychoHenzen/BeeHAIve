from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from threading import Lock
from typing import cast
from uuid import uuid4

from ..routing import RoutingStore
from ..storage import (
    MAX_META_REVIEW_ATTEMPTS,
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    OrchestratorStore,
    StoreError,
)
from .meta_review_creation import pbi_creation_request
from .meta_review_error import MetaReviewError
from .meta_review_helpers import (
    bounded_diagnostics,
    deterministic_analyzer,
    estimate_tokens,
    mappings,
    normalize_since,
    normalize_suggestions,
    recurrence_gate,
    review_result,
    safe_record,
    safe_review_text,
)
from .meta_review_types import Analyzer, PbiCreator


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
        normalized_since = normalize_since(since)
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
                    source_records, input_token_limit, project_id
                )
                selected_records = len(records)
                suggestions = normalize_suggestions(project_id, self._analyzer(records))
                suggestions, gate_diagnostics = recurrence_gate(
                    project_id,
                    records,
                    suggestions,
                    since=normalized_since,
                    record_limit=record_limit,
                )
                missing_evidence = bounded_diagnostics(
                    [*missing_evidence, *gate_diagnostics]
                )
                run, saved = self.store.complete_meta_review(
                    review_id,
                    selected_records,
                    input_tokens,
                    missing_evidence,
                    suggestions,
                )
                return review_result(run, saved)
            except Exception as exc:
                error = safe_review_text(str(exc)) or "Meta-review failed"
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
        self,
        project_id: str,
        suggestion_id: str,
        decision: str,
        *,
        pbi_creator: PbiCreator | None = None,
    ) -> dict[str, object]:
        status = {"accept": "accepted", "reject": "rejected"}.get(decision)
        if status is None:
            raise MetaReviewError("Decision must be accept or reject")
        project_id = project_id.strip()
        suggestion_id = suggestion_id.strip()
        if status == "accepted" and pbi_creator is None:
            raise MetaReviewError("PBI creation service is unavailable")
        suggestion = self.store.decide_meta_review_suggestion(
            project_id, suggestion_id, status
        )
        result: dict[str, object] = {"suggestion": suggestion}
        if status == "accepted":
            request = pbi_creation_request(self.store, project_id, suggestion)
            key = f"meta-review:{project_id}:{suggestion_id}"
            if pbi_creator is None:  # pragma: no cover - guarded above
                raise MetaReviewError("PBI creation service is unavailable")
            result["pbi_creation"] = pbi_creator(request, key)
        return result

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
        project_id: str | None = None,
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
            if source_id != f"run:{run_id}":
                missing.append(f"{source_id}: source/run identity mismatch")
                continue
            if project_id is not None and source_record.get("project_id") != project_id:
                missing.append(f"{source_id}: Project ownership mismatch")
                continue
            if not any(
                isinstance(value, str) and value.strip()
                for value in (source_record.get("result"), source_record.get("error"))
            ):
                missing.append(f"{source_id}: completion result or error unavailable")
                continue
            raw_events = source_record.get("events")
            if not mappings(raw_events):
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
            record = safe_record(source_record, attempts)
            if project_id is not None:
                record["project_id"] = project_id
            transcript = cast(dict[str, object], record["transcript"])
            for gap in cast(list[str], transcript["gaps"]):
                missing.append(f"{record['source_id']}: transcript {gap}")
            record_tokens = estimate_tokens(record)
            if input_tokens + record_tokens > input_token_limit:
                missing.append("Input token limit reached")
                break
            records.append(record)
            input_tokens += record_tokens
        return records, bounded_diagnostics(missing), input_tokens
