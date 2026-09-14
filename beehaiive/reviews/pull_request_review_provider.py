from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .pull_request_target import PullRequestTarget


class PullRequestReviewProvider(Protocol):
    """Provider that returns the current head before review or merge."""

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget: ...
