from __future__ import annotations

from ..review import PullRequestTarget


class DemoReviewProvider:
    """Return a local synthetic pull request for startup adapter validation."""

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        return PullRequestTarget(pull_request_id, "demo-head")
