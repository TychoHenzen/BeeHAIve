from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .pull_request_target import PullRequestTarget
    from .reader_execution import ReaderExecution


class ReviewReader(Protocol):
    """Specialized reader invoked for one pull-request concern."""

    def review(self, target: PullRequestTarget) -> ReaderExecution: ...
