from beehaiive.review import (
    PullRequestTarget,
    ReviewStore,
)
from tests.support.review.fixture_provider import FixtureProvider


class LockInspectingProvider(FixtureProvider):
    def __init__(
        self, store: ReviewStore, targets: dict[str, PullRequestTarget]
    ) -> None:
        super().__init__(targets)
        self.store = store
        self.in_transaction_during_provider_call: bool | None = None

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        self.in_transaction_during_provider_call = self.store._connection.in_transaction
        return super().get_pull_request(pull_request_id)
