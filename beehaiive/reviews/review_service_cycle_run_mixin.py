from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import REQUIRED_CONCERNS
from .helpers import github_pull_request_evidence_ref, normalized_head_sha, require_text
from .reader_execution import ReaderExecution
from .reader_status import ReaderStatus
from .review_error import ReviewError

if TYPE_CHECKING:
    from .review_snapshot import ReviewSnapshot
from typing import Any


class ReviewServiceCycleRunMixin:
    def run_ready_review(self: Any, pull_request_id: str) -> ReviewSnapshot:
        pull_request_id = require_text(pull_request_id, "pull request id")
        expected_cycle_id = self.store.current_cycle_id(pull_request_id)
        target = self._validated_provider_target(pull_request_id)
        missing = [
            concern.value
            for concern in REQUIRED_CONCERNS
            if concern not in self.readers
        ]
        if missing:
            raise ReviewError(f"No reader is configured for {', '.join(missing)}")
        cycle = self._start_cycle(
            pull_request_id,
            target.head_sha,
            expected_cycle_id=expected_cycle_id,
            github_evidence_json=target.evidence_json,
        )
        for concern in REQUIRED_CONCERNS:
            reader = self.readers[concern]
            current_reader = next(
                item for item in cycle.readers if item.concern is concern
            )
            if current_reader.status is not ReaderStatus.PENDING:
                continue
            claim_token = self.store.claim_reader(cycle.cycle.cycle_id, concern)
            if claim_token is None:
                continue
            try:
                execution = reader.review(target)
            except Exception:
                evidence_ref = github_pull_request_evidence_ref(target.evidence_json)
                execution = ReaderExecution(
                    ReaderStatus.FAIL,
                    (f"{concern.value} reader failed",),
                    (evidence_ref,) if evidence_ref is not None else (),
                )
            cycle = self.record_reader(
                cycle.cycle.cycle_id,
                concern,
                execution.status,
                execution.findings,
                reader=concern.value,
                claim_token=claim_token,
                evidence_refs=execution.evidence_refs,
            )
        return cycle

    def start_cycle(self: Any, pull_request_id: str, head_sha: str) -> ReviewSnapshot:
        pull_request_id = require_text(pull_request_id, "pull request id")
        head_sha = normalized_head_sha(head_sha)
        expected_cycle_id = self.store.current_cycle_id(pull_request_id)
        evidence_json = None
        if self.provider is not None:
            target = self._validated_provider_target(pull_request_id)
            if target.head_sha != head_sha:
                raise ReviewError("Review head does not match the current pull request")
            evidence_json = target.evidence_json
        return self._start_cycle(
            pull_request_id,
            head_sha,
            expected_cycle_id=expected_cycle_id,
            github_evidence_json=evidence_json,
        )
