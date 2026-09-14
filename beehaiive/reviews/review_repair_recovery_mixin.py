from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..review import (
    ReviewRepairAttempt,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
)
from ..workflow import (
    LeaseStatus,
)


class ReviewRepairRecoveryMixin:
    def _recover_attempt(self: Any, attempt: ReviewRepairAttempt) -> None:
        if attempt.status is ReviewRepairStatus.QUEUED:
            self._start_worker(attempt.attempt_id)
            return
        if (
            attempt.status is ReviewRepairStatus.SUCCEEDED
            and attempt.review_transition_status
            in {
                ReviewRepairTransitionStatus.PENDING,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
            }
        ):
            self._start_review_transition(attempt)
            return
        if attempt.status not in {
            ReviewRepairStatus.RUNNING,
            ReviewRepairStatus.PUSHING,
        }:
            return
        with self._lock:
            if attempt.attempt_id in self._threads:
                return
        if attempt.lease_id is None:
            if self._within_worker_grace(attempt):
                return
        else:
            lease = self.workflow.store.get_lease(attempt.lease_id)
            if lease is not None and lease.status is LeaseStatus.ACTIVE:
                return
            if lease is not None and self._within_worker_grace(attempt):
                return
        if attempt.status is ReviewRepairStatus.PUSHING:
            self._recover_push(attempt)
            return
        self.reviews.store.finish_repair_attempt(
            attempt.attempt_id,
            ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
            required_action=(
                "The repair worker stopped before completion. Verify the pull-request "
                "head, then start a new review cycle before retrying."
            ),
        )

    def _within_worker_grace(self: Any, attempt: ReviewRepairAttempt) -> bool:
        grace_seconds = max(60, self.workflow.store.lease_heartbeat_seconds * 3)
        try:
            updated_at = datetime.fromisoformat(attempt.updated_at)
            return (datetime.now(UTC) - updated_at).total_seconds() < grace_seconds
        except ValueError:
            return False

    def _recover_push(self: Any, attempt: ReviewRepairAttempt) -> None:
        evidence = attempt.push_evidence
        identity = self._push_evidence_identity(attempt)
        confirmed = False
        detail: str | None = None
        if identity is not None:
            repository, number, branch = identity
            try:
                current = self.provider.get_pull_request(repository, number)
                review_target = self.review_provider.get_pull_request(
                    attempt.pull_request_id
                )
                confirmed = (
                    current.repository.casefold() == repository.casefold()
                    and current.number == number
                    and current.source_branch == branch
                    and current.source_head == attempt.commit_sha
                    and current.state == "OPEN"
                    and not current.merged
                    and review_target.pull_request_id == attempt.pull_request_id
                    and review_target.head_sha == attempt.commit_sha
                    and review_target.ready
                )
            except Exception as exc:
                detail = self._safe_text(str(exc), 500)
        if confirmed:
            completed = self.reviews.store.finish_repair_attempt(
                attempt.attempt_id,
                ReviewRepairStatus.SUCCEEDED,
                commit_sha=attempt.commit_sha,
                push_evidence=evidence,
                result="Push confirmed during restart recovery",
            )
            self._recover_attempt(completed)
            return
        required_action = (
            "The push outcome could not be confirmed after the worker stopped. "
            "Verify the pull-request head before retrying."
        )
        if detail:
            required_action = f"{required_action} Provider readback failed: {detail}"
        self.reviews.store.finish_repair_attempt(
            attempt.attempt_id,
            ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
            commit_sha=attempt.commit_sha,
            push_evidence=evidence,
            required_action=required_action,
        )
