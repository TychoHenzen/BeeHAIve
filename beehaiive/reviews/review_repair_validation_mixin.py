from __future__ import annotations

import re
from threading import Event
from typing import Any

from ..models import PullRequestSnapshot
from ..provider import ProviderError
from ..review import (
    ReviewError,
    ReviewRepairAttempt,
)
from ..workflow import (
    LeaseStatus,
    WorkflowError,
    WorkspaceLease,
    repository_identity,
)
from .repair_cancelled import RepairCancelled


class ReviewRepairValidationMixin:
    @staticmethod
    def pull_request_parts(pull_request_id: str) -> tuple[str, int]:
        repository, separator, number_text = pull_request_id.rpartition("#")
        if not separator or not re.fullmatch(r"[^/#]+/[^/#]+", repository):
            raise ReviewError("Pull-request id must use owner/repository#number")
        if not number_text.isdigit() or int(number_text) <= 0:
            raise ReviewError("Pull-request number must be positive")
        return repository, int(number_text)

    @staticmethod
    def _validate_snapshot(
        snapshot: PullRequestSnapshot,
        attempt: ReviewRepairAttempt,
        repository: str,
        number: int,
    ) -> None:
        if (
            snapshot.repository.casefold() != repository.casefold()
            or snapshot.number != number
            or snapshot.state != "OPEN"
            or snapshot.merged
            or not snapshot.source_branch
            or snapshot.source_head != attempt.head_sha
        ):
            raise ProviderError("Pull request is not open at the selected finding head")

    @staticmethod
    def _same_source_identity(
        expected: PullRequestSnapshot,
        current: PullRequestSnapshot,
        *,
        expected_source_head: str | None = None,
    ) -> bool:
        return (
            expected.repository.casefold() == current.repository.casefold()
            and expected.number == current.number
            and expected.pull_request_id == current.pull_request_id
            and expected.source_branch == current.source_branch
            and current.source_head
            == (
                expected.source_head
                if expected_source_head is None
                else expected_source_head
            )
            and current.state == "OPEN"
            and not current.merged
        )

    def _validate_active_lease(self: Any, lease: WorkspaceLease) -> None:
        current = self.workflow.store.get_lease(lease.lease_id)
        if (
            current is None
            or current.status is not LeaseStatus.ACTIVE
            or current.lease_token != lease.lease_token
        ):
            raise WorkflowError("Review repair workspace lease was lost")

    def _validate_push_remote(self: Any, repository: str, worktree: str) -> None:
        result = self.workflow.worktrees.run_git(
            "-C", worktree, "remote", "get-url", "--push", "--all", "origin"
        )
        remote_urls = (
            tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
            if result.returncode == 0
            else ()
        )
        if (
            len(remote_urls) != 1
            or remote_urls != self.workflow.worktrees.origin_push_urls
            or (repository_identity(remote_urls[0]) or "").casefold()
            != repository.casefold()
        ):
            raise WorkflowError(
                "Configured push remote does not match the review repository"
            )

    @staticmethod
    def _raise_if_cancelled(attempt: ReviewRepairAttempt, cancelled: Event) -> None:
        if cancelled.is_set() or attempt.cancellation_requested:
            raise RepairCancelled
