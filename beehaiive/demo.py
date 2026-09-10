"""Deterministic adapters that make the local dashboard demo self-contained."""

from __future__ import annotations

from .review import (
    REQUIRED_CONCERNS,
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
    ReviewConcern,
)


class DemoReviewProvider:
    """Return a local synthetic pull request for startup adapter validation."""

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        return PullRequestTarget(pull_request_id, "demo-head")


class DemoReviewReader:
    """Pass every concern without inspecting or changing a pull request."""

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        del target
        return ReaderExecution(ReaderStatus.PASS)


def demo_review_adapters() -> tuple[
    DemoReviewProvider, dict[ReviewConcern, DemoReviewReader]
]:
    reader = DemoReviewReader()
    return DemoReviewProvider(), {concern: reader for concern in REQUIRED_CONCERNS}
