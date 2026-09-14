from beehaiive.review import (
    FindingPublicationChannel,
    FindingPublicationState,
    PublicationOutcome,
    PullRequestTarget,
    ReviewConcern,
    ReviewStore,
)
from tests.support.review.fixture_provider import FixtureProvider


class PublishingFixtureProvider(FixtureProvider):
    def __init__(
        self,
        store: ReviewStore,
        targets: dict[str, PullRequestTarget],
        *,
        fail_once: bool = False,
    ) -> None:
        super().__init__(targets)
        self.store = store
        self.fail_once = fail_once
        self.publication_state = FindingPublicationState.PUBLISHED
        self.publish_calls = 0
        self.remote_ids: list[str | None] = []
        self.in_transaction_during_publish: bool | None = None

    def publish_finding(
        self,
        pull_request_id: str,
        *,
        expected_head_sha: str,
        fingerprint: str,
        concern: ReviewConcern,
        summary: str,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        remote_id: str | None = None,
        remote_url: str | None = None,
    ) -> PublicationOutcome:
        del expected_head_sha, fingerprint, concern, summary, file_path
        del start_line, end_line, remote_url
        self.publish_calls += 1
        self.remote_ids.append(remote_id)
        self.in_transaction_during_publish = self.store._connection.in_transaction
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("secret provider response")
        assert pull_request_id in self.targets
        return PublicationOutcome(
            self.publication_state,
            FindingPublicationChannel.REVIEW_BODY,
            "REMOTE-REVIEW-1",
            "https://github.com/owner/repo/pull/1#pullrequestreview-1",
        )
