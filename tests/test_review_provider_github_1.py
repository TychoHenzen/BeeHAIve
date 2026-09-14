from __future__ import annotations

import json

import pytest

from beehaiive.provider import ProviderError
from beehaiive.review import (
    REQUIRED_CONCERNS,
    FindingPublicationChannel,
    FindingPublicationState,
    ReviewCycleStatus,
    ReviewService,
    ReviewStore,
    finding_publication_marker,
)
from beehaiive.review_github import (
    GitHubReviewProvider,
    github_review_readers,
)
from tests.support.provider.publishing_graph_ql_client import (
    PublishingGraphQLClient as PublishingGraphQLClient,
)
from tests.support.provider.review_graph_ql_client import (
    ReviewGraphQLClient as ReviewGraphQLClient,
)


def test_github_review_provider_reads_and_retains_every_review_page() -> None:
    client = ReviewGraphQLClient()
    target = GitHubReviewProvider(token="unit-secret", client=client).get_pull_request(
        "owner/repo#7"
    )

    assert target.ready
    assert target.head_sha == "head-7"
    assert target.evidence_json is not None
    assert "unit-secret" not in target.evidence_json
    evidence = json.loads(target.evidence_json)
    assert evidence["pull_request"]["id"] == "PULL_REQUEST_NODE"
    assert [review["id"] for review in evidence["reviews"]] == [
        "REVIEW-BOT",
        "REVIEW-USER",
    ]
    assert evidence["reviews"][0]["author"]["__typename"] == "Bot"
    assert evidence["reviews"][1]["state"] == "CHANGES_REQUESTED"
    assert [
        request["requestedReviewer"]["__typename"]
        for request in evidence["requested_reviewers"]
    ] == ["User", "Team"]
    threads = evidence["review_threads"]
    assert [thread["id"] for thread in threads] == ["THREAD-1", "THREAD-2"]
    assert threads[0]["path"] == "src/app.py"
    assert threads[0]["startLine"] == 18
    assert threads[0]["diffSide"] == "RIGHT"
    assert threads[0]["isOutdated"] is True
    assert threads[1]["isResolved"] is True
    assert [comment["id"] for comment in threads[0]["comments"]] == [
        "COMMENT-1",
        "COMMENT-2",
    ]
    assert threads[0]["comments"][0]["body"] == "Bearer [redacted]"
    assert len(client.calls) == 5
    assert all("mutation" not in query.lower() for query, _ in client.calls)


def test_github_review_provider_publishes_anchored_finding_idempotently() -> None:
    client = PublishingGraphQLClient()
    provider = GitHubReviewProvider(token="unit-secret", client=client)
    fingerprint = "a" * 64
    marker = finding_publication_marker("owner/repo#7", "head-7", fingerprint)

    published = provider.publish_finding(
        "owner/repo#7",
        expected_head_sha="head-7",
        fingerprint=fingerprint,
        concern="security",
        summary="Validate the untrusted path.",
        file_path="src/app.py",
        start_line=18,
        end_line=20,
        remote_id=None,
    )
    retried = provider.publish_finding(
        "owner/repo#7",
        expected_head_sha="head-7",
        fingerprint=fingerprint,
        concern="security",
        summary="Validate the untrusted path.",
        file_path="src/app.py",
        start_line=18,
        end_line=20,
        remote_id=published.remote_id,
    )

    assert published.state is FindingPublicationState.PUBLISHED
    assert published.channel is FindingPublicationChannel.REVIEW_THREAD
    assert published.remote_id == "THREAD-PUBLISHED-1-1"
    assert published.remote_url.endswith("#discussion-THREAD-PUBLISHED-1-1")
    assert retried.remote_id == published.remote_id
    assert len(client.mutations) == 1
    review_input = client.mutations[0]
    assert review_input["event"] == "COMMENT"
    assert review_input["commitOID"] == "head-7"
    assert review_input["threads"] == [
        {
            "body": f"[security] Validate the untrusted path.\n\n{marker}",
            "path": "src/app.py",
            "line": 20,
            "side": "RIGHT",
            "startLine": 18,
            "startSide": "RIGHT",
        }
    ]


def test_github_review_provider_publishes_unanchored_finding_in_review_body() -> None:
    client = PublishingGraphQLClient()
    provider = GitHubReviewProvider(token="unit-secret", client=client)
    fingerprint = "b" * 64
    marker = finding_publication_marker("owner/repo#7", "head-7", fingerprint)

    published = provider.publish_finding(
        "owner/repo#7",
        expected_head_sha="head-7",
        fingerprint=fingerprint,
        concern="clean_code",
        summary="Simplify the conditional. unit-secret",
        remote_id=None,
    )

    assert published.state is FindingPublicationState.PUBLISHED
    assert published.channel is FindingPublicationChannel.REVIEW_BODY
    assert published.remote_id == "REVIEW-PUBLISHED-1"
    assert marker in client.mutations[0]["body"]
    assert "[clean_code] Simplify the conditional." in client.mutations[0]["body"]
    assert "unit-secret" not in client.mutations[0]["body"]
    assert "threads" not in client.mutations[0]


def test_github_review_provider_does_not_publish_on_a_stale_head() -> None:
    client = PublishingGraphQLClient(head_sha="head-new")
    provider = GitHubReviewProvider(token="unit-secret", client=client)

    result = provider.publish_finding(
        "owner/repo#7",
        expected_head_sha="head-7",
        fingerprint="c" * 64,
        concern="security",
        summary="Check the current head.",
        remote_id=None,
    )

    assert result.state is FindingPublicationState.STALE
    assert client.mutations == []


def test_github_provider_preserves_findings_with_missing_remote_content() -> None:
    client = PublishingGraphQLClient()
    provider = GitHubReviewProvider(token="unit-secret", client=client)

    result = provider.publish_finding(
        "owner/repo#7",
        expected_head_sha="head-7",
        fingerprint="d" * 64,
        concern="security",
        summary="Do not dismiss removed remote content.",
        remote_id="DELETED-THREAD",
        remote_url="https://github.com/owner/repo/pull/7#discussion-deleted",
        file_path="src/app.py",
        start_line=22,
        end_line=22,
    )

    assert result.state is FindingPublicationState.REMOTE_MISSING
    assert result.remote_id == "DELETED-THREAD"
    assert result.remote_url.endswith("#discussion-deleted")
    assert client.mutations == []


@pytest.mark.parametrize(
    "pull_request_id",
    (
        "repo#7",
        "owner/repo",
        "owner/repo#0",
        "owner/repo/sub#7",
        "owner/repo#2147483648",
    ),
)
def test_github_review_provider_rejects_unqualified_or_invalid_ids(
    pull_request_id: str,
) -> None:
    client = ReviewGraphQLClient()
    with pytest.raises(ProviderError):
        GitHubReviewProvider(client=client).get_pull_request(pull_request_id)
    assert client.calls == []


@pytest.mark.parametrize(
    ("state", "merged", "is_draft"),
    (("CLOSED", False, False), ("OPEN", False, True), ("CLOSED", True, False)),
)
def test_github_review_provider_does_not_read_nonready_pull_requests(
    state: str, merged: bool, is_draft: bool
) -> None:
    client = ReviewGraphQLClient(state=state, merged=merged, is_draft=is_draft)
    target = GitHubReviewProvider(client=client).get_pull_request("owner/repo#7")
    assert not target.ready
    assert target.evidence_json is None
    assert len(client.calls) == 1


def test_github_review_provider_rejects_incomplete_pagination() -> None:
    client = ReviewGraphQLClient(malformed_reviews=True)
    with pytest.raises(ProviderError, match="pagination state"):
        GitHubReviewProvider(client=client).get_pull_request("owner/repo#7")


def test_production_readers_persist_evidence_and_remain_pending_without_policy() -> (
    None
):
    client = ReviewGraphQLClient()
    provider = GitHubReviewProvider(token="unit-secret", client=client)
    store = ReviewStore()
    try:
        service = ReviewService(
            store, provider=provider, readers=github_review_readers()
        )
        first = service.run_ready_review("owner/repo#7")
        second = service.run_ready_review("owner/repo#7")

        assert first.cycle.status is ReviewCycleStatus.ACTIVE
        assert second.cycle.cycle_id == first.cycle.cycle_id
        assert len(second.readers) == len(REQUIRED_CONCERNS) == 4
        assert all(reader.status.value == "pending" for reader in second.readers)
        assert all(
            reader.evidence_refs == ("PULL_REQUEST_NODE",) for reader in second.readers
        )
        response = second.as_dict()
        assert response["cycle"]["github_evidence"]["pull_request"]["id"] == (
            "PULL_REQUEST_NODE"
        )
        assert response["findings"] == []
    finally:
        store.close()
