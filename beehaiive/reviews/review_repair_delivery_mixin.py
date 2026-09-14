from __future__ import annotations

import json
from typing import Any

from ..agent import redact_worker_text
from ..review import (
    FindingStatus,
    ReviewError,
    ReviewRepairAttempt,
    ReviewRepairTransitionStatus,
)
from .review_repair_validation_mixin import ReviewRepairValidationMixin

MAX_REPAIR_PROMPT_CHARS = 24_000


class ReviewRepairDeliveryMixin:
    @staticmethod
    def _push_evidence_identity(
        attempt: ReviewRepairAttempt,
    ) -> tuple[str, int, str] | None:
        evidence = attempt.push_evidence
        if (
            evidence is None
            or attempt.commit_sha is None
            or attempt.commit_sha == attempt.head_sha
            or evidence.get("expected_head") != attempt.head_sha
            or evidence.get("pushed_head") != attempt.commit_sha
        ):
            return None
        repository = evidence.get("repository")
        number = evidence.get("pull_request_number")
        branch = evidence.get("source_branch")
        if (
            not isinstance(repository, str)
            or not repository
            or type(number) is not int
            or number <= 0
            or not isinstance(branch, str)
            or not branch
        ):
            return None
        try:
            expected_repository, expected_number = (
                ReviewRepairValidationMixin.pull_request_parts(attempt.pull_request_id)
            )
        except ReviewError:
            return None
        if (
            repository.casefold() != expected_repository.casefold()
            or number != expected_number
        ):
            return None
        return repository, number, branch

    def _start_review_transition(self: Any, attempt: ReviewRepairAttempt) -> None:
        identity = self._push_evidence_identity(attempt)
        if identity is None:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED,
                "Verify the repair commit and pushed pull-request evidence before "
                "starting a fresh review cycle.",
            )
            return
        repository, number, branch = identity
        try:
            current = self.provider.get_pull_request(repository, number)
            target = self.review_provider.get_pull_request(attempt.pull_request_id)
        except Exception:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
                "Provider readback failed. Retry the fresh review cycle transition.",
            )
            return
        if (
            current.repository.casefold() != repository.casefold()
            or current.number != number
            or current.source_branch != branch
            or current.source_head != attempt.commit_sha
            or current.state != "OPEN"
            or current.merged
            or target.pull_request_id != attempt.pull_request_id
            or target.head_sha != attempt.commit_sha
            or not target.ready
        ):
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED,
                "The current pull-request identity or head no longer matches the "
                "pushed repair. Verify the head and start a fresh review cycle "
                "manually.",
            )
            return
        try:
            self.reviews.start_repair_followup_cycle(attempt.attempt_id, target)
        except Exception:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
                "The fresh review cycle could not be recorded. Retry the transition.",
            )

    def _prompt(self: Any, attempt: ReviewRepairAttempt) -> str:
        findings = [
            self.reviews.store.finding_for_id(finding_id)
            for finding_id in attempt.finding_ids
        ]
        if any(
            finding.pull_request_id != attempt.pull_request_id
            or finding.head_sha != attempt.head_sha
            or finding.stale
            or finding.status is not FindingStatus.OPEN
            or finding.publication_state.value != "published"
            or finding.duplicate_target is not None
            for finding in findings
        ):
            raise ReviewError("Selected review findings are no longer current and open")
        payload = {
            "pull_request_id": attempt.pull_request_id,
            "head_sha": attempt.head_sha,
            "selected_findings": [
                {
                    "finding_id": finding.finding_id,
                    "concern": finding.concern.value,
                    "summary": finding.summary[:400],
                    "file_path": finding.file_path,
                    "start_line": finding.start_line,
                    "end_line": finding.end_line,
                    "evidence_refs": list(finding.evidence_refs[:2]),
                }
                for finding in findings
            ],
        }
        prompt = (
            "BeeHAIve selected review-finding repair. Repair only the findings in "
            "the JSON below. Work only in this leased worktree. Do not push, access "
            "credentials, edit another checkout, read unrelated review findings, "
            "or start another agent. Make the smallest correct change, run relevant "
            "checks, stage only the repair, and create one normal commit. Return a "
            "short plain-text result.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        prompt = self._safe_text(prompt, MAX_REPAIR_PROMPT_CHARS + 1)
        if len(prompt) > MAX_REPAIR_PROMPT_CHARS:
            raise ReviewError("Selected repair context exceeds the prompt limit")
        return prompt

    def _safe_text(self: Any, value: str, limit: int) -> str:
        secrets = tuple(
            secret
            for secret in getattr(self.agent, "_secret_values", ())
            if isinstance(secret, str)
        )
        return redact_worker_text(value, secrets, max_length=limit)
