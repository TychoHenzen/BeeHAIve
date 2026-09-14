from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from .constants import MAX_REVIEW_EVIDENCE_BYTES
from .helpers import (
    coerce_enum,
    github_pull_request_evidence_ref,
    json_object,
    normalized_head_sha,
    require_text,
)
from .pull_request_target import PullRequestTarget
from .review_action import ReviewAction
from .review_adapter_error import ReviewAdapterError
from .review_error import ReviewError
from .review_repair_status import ReviewRepairStatus

if TYPE_CHECKING:
    from .review_repair_attempt import ReviewRepairAttempt
from typing import Any


class ReviewServiceAccessMixin:
    def authorize(
        self: Any, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> None:
        pull_request_id = require_text(pull_request_id, "pull request id")
        actor = require_text(actor, "review actor", 100)
        action = coerce_enum(action, ReviewAction, "review action")
        if not self.authorizer.authorize(pull_request_id, actor, action):
            raise ReviewError("Review actor is not authorized for this action")

    def create_repair_attempt(
        self: Any, cycle_id: str, finding_ids: Iterable[str], actor: str
    ) -> tuple[ReviewRepairAttempt, bool]:
        pull_request_id = self.pull_request_id_for_cycle(cycle_id)
        self.authorize(pull_request_id, actor, ReviewAction.REPAIR)
        return self.store.create_repair_attempt(cycle_id, finding_ids, actor)

    def repair_attempt(self: Any, attempt_id: str) -> ReviewRepairAttempt:
        return self.store.repair_attempt(attempt_id)

    def cancel_repair_attempt(
        self: Any, attempt_id: str, actor: str
    ) -> ReviewRepairAttempt:
        attempt = self.store.repair_attempt(attempt_id)
        self.authorize(attempt.pull_request_id, actor, ReviewAction.REPAIR)
        if attempt.status is ReviewRepairStatus.PUSHING:
            raise ReviewError(
                "Review repair can no longer be cancelled after push starts"
            )
        requested = self.store.request_repair_cancellation(attempt_id)
        if requested.status is ReviewRepairStatus.PUSHING:
            raise ReviewError(
                "Review repair can no longer be cancelled after push starts"
            )
        return requested

    def pull_request_id_for_cycle(self: Any, cycle_id: str) -> str:
        return self.store.pull_request_id_for_cycle(cycle_id)

    def pull_request_id_for_finding(self: Any, finding_id: str) -> str:
        return self.store.pull_request_id_for_finding(finding_id)

    def _validated_provider_target(
        self: Any, pull_request_id: str
    ) -> PullRequestTarget:
        if self.provider is None:
            raise ReviewError("A pull-request provider is required for ready reviews")
        try:
            target = self.provider.get_pull_request(pull_request_id)
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewAdapterError("Pull-request provider failed") from exc
        return self._validate_provider_target(pull_request_id, target)

    def _validate_provider_target(
        self: Any, pull_request_id: str, target: PullRequestTarget
    ) -> PullRequestTarget:
        if target.pull_request_id != pull_request_id:
            raise ReviewError("Pull-request provider returned the wrong pull request")
        if not target.ready:
            raise ReviewError("Pull request is not ready for review")
        raw_evidence_json: object = getattr(target, "evidence_json", None)
        if raw_evidence_json is None:
            evidence_json = None
        elif isinstance(raw_evidence_json, str):
            evidence_json = raw_evidence_json
            if len(evidence_json.encode("utf-8")) > MAX_REVIEW_EVIDENCE_BYTES:
                raise ReviewError("GitHub review evidence exceeds the storage limit")
            json_object(evidence_json)
            if github_pull_request_evidence_ref(evidence_json) is None:
                raise ReviewError(
                    "Provider evidence omitted the GitHub pull-request id"
                )
        else:
            raise ReviewError("Provider returned invalid GitHub review evidence")
        return PullRequestTarget(
            target.pull_request_id,
            normalized_head_sha(target.head_sha, "Provider head SHA"),
            target.ready,
            evidence_json,
        )
