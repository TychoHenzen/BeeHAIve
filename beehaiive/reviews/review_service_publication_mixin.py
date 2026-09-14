from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .finding_publication_state import FindingPublicationState
from .finding_publisher import FindingPublisher
from .finding_status import FindingStatus
from .helpers import current_timestamp, normalized_head_sha, require_text
from .merge_handoff import MergeHandoff
from .publication_outcome import PublicationOutcome
from .review_cycle_status import ReviewCycleStatus
from .review_error import ReviewError

if TYPE_CHECKING:
    from .review_snapshot import ReviewSnapshot
from typing import Any


class ReviewServicePublicationMixin:
    def publish_finding(self: Any, finding_id: str) -> ReviewSnapshot:
        finding_id = require_text(finding_id, "finding id")
        initial = self.store.finding_for_id(finding_id)
        if initial.status is FindingStatus.RESOLVED:
            raise ReviewError("Resolved findings cannot be published")
        claim = self.store.claim_finding_publication(finding_id)
        if claim is None:
            return self.store.current_snapshot(initial.pull_request_id)
        claim_token, finding = claim
        if finding.duplicate_target is not None:
            duplicate = self.store.finding_for_id(finding.duplicate_target)
            if (
                duplicate.pull_request_id == finding.pull_request_id
                and duplicate.publication_state
                in {
                    FindingPublicationState.PUBLISHED,
                    FindingPublicationState.DUPLICATE,
                }
                and duplicate.remote_id is not None
            ):
                outcome = PublicationOutcome(
                    FindingPublicationState.DUPLICATE,
                    duplicate.publication_channel,
                    duplicate.remote_id,
                    duplicate.remote_url,
                )
            else:
                outcome = PublicationOutcome(
                    FindingPublicationState.RETRYABLE,
                    retry_evidence={"reason": "duplicate_target_not_published"},
                )
        elif self.provider is None or not hasattr(self.provider, "publish_finding"):
            outcome = PublicationOutcome(
                FindingPublicationState.RETRYABLE,
                retry_evidence={"error_type": "FindingPublisherUnavailable"},
            )
        else:
            try:
                outcome = cast(FindingPublisher, self.provider).publish_finding(
                    finding.pull_request_id,
                    expected_head_sha=finding.head_sha,
                    fingerprint=finding.fingerprint,
                    concern=finding.concern,
                    summary=finding.summary,
                    file_path=finding.file_path,
                    start_line=finding.start_line,
                    end_line=finding.end_line,
                    remote_id=finding.remote_id,
                    remote_url=finding.remote_url,
                )
            except Exception as exc:
                outcome = PublicationOutcome(
                    FindingPublicationState.RETRYABLE,
                    finding.publication_channel,
                    finding.remote_id,
                    finding.remote_url,
                    {"error_type": type(exc).__name__},
                )
        self.store.finish_finding_publication(finding_id, claim_token, outcome)
        return self.store.current_snapshot(finding.pull_request_id)

    def approve_for_merge(
        self: Any, cycle_id: str, reason: str, actor: str = "operator"
    ) -> ReviewSnapshot:
        actor = require_text(actor, "approval actor", 100)
        reason = require_text(reason, "approval reason", 1_000)
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Human approval belongs to a stale review cycle")
            connection.execute(
                """
                    UPDATE review_cycles
                    SET status = ?, human_approval = 1, required_action = NULL,
                        approval_actor = ?, approval_reason = ?, approval_at = ?,
                        updated_at = ?
                    WHERE cycle_id = ?
                    """,
                (
                    ReviewCycleStatus.HUMAN_APPROVED.value,
                    actor,
                    reason,
                    current_timestamp(),
                    current_timestamp(),
                    cycle_id,
                ),
            )
        return self.store.snapshot(cycle_id)

    def merge_handoff(self: Any, pull_request_id: str, head_sha: str) -> MergeHandoff:
        pull_request_id = require_text(pull_request_id, "pull request id")
        head_sha = normalized_head_sha(head_sha)
        if self.provider is None:
            raise ReviewError("A pull-request provider is required for merge handoff")
        if self.store.current_cycle_id(pull_request_id) is None:
            raise ReviewError(f"No review cycle exists for {pull_request_id}")
        target = self._validated_provider_target(pull_request_id)
        with self.store.transaction() as connection:
            current = self.store.current_cycle_row(connection, pull_request_id)
            if current is None:
                raise ReviewError(f"No review cycle exists for {pull_request_id}")
            if (
                target.pull_request_id != pull_request_id
                or not target.ready
                or target.head_sha != head_sha
                or str(current["head_sha"]) != target.head_sha
            ):
                raise ReviewError(
                    "The pull request head has no current review authorization"
                )
            snapshot = self.store.snapshot_in_connection(
                connection, str(current["cycle_id"])
            )
            if not snapshot.merge_allowed:
                raise ReviewError(
                    snapshot.cycle.required_action
                    or "All required review readers must pass before merge handoff"
                )
            return MergeHandoff(
                pull_request_id,
                snapshot.cycle.cycle_id,
                head_sha,
                snapshot.cycle.human_approval,
                snapshot.cycle.approval_actor,
                snapshot.cycle.approval_reason,
                snapshot.cycle.approval_at,
            )
