from __future__ import annotations

from beehaiive.review import (
    PullRequestTarget,
)
from tests.support.repair.fixture_provider import FixtureProvider


class FixtureReviewProvider:
    def __init__(self, provider: FixtureProvider) -> None:
        self.provider = provider

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        current = self.provider.get_pull_request("owner/repo", 1)
        return PullRequestTarget(pull_request_id, current.source_head or "")
