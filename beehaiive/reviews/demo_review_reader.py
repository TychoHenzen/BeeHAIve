from __future__ import annotations

from ..review import PullRequestTarget, ReaderExecution, ReaderStatus


class DemoReviewReader:
    """Pass every concern without inspecting or changing a pull request."""

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        del target
        return ReaderExecution(ReaderStatus.PASS)
