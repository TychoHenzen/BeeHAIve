import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    REQUIRED_CONCERNS,
    AllowListReviewAuthorizer,
    FindingPublicationChannel,
    FindingPublicationState,
    FindingStatus,
    PublicationOutcome,
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
    ReviewConcern,
    ReviewCycleStatus,
    ReviewError,
    ReviewService,
    ReviewStore,
)
from main import create_app


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()


class FixtureProvider:
    def __init__(self, targets: dict[str, PullRequestTarget]) -> None:
        self.targets = targets

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        try:
            return self.targets[pull_request_id]
        except KeyError as exc:
            raise ReviewError(f"Unknown pull request: {pull_request_id}") from exc


class ReaderDouble:
    def __init__(
        self,
        status: ReaderStatus = ReaderStatus.PASS,
        findings: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.findings = findings
        self.calls = 0

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        assert target.ready
        self.calls += 1
        return ReaderExecution(self.status, self.findings)


class BlockingReader(ReaderDouble):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return ReaderExecution(ReaderStatus.PASS)


class FailingReader:
    def review(self, target: PullRequestTarget) -> ReaderExecution:
        raise RuntimeError("reader dependency unavailable")


class ExplodingProvider:
    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        raise RuntimeError("provider dependency unavailable")


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


def _reader_doubles(
    status: ReaderStatus = ReaderStatus.PASS,
) -> dict[ReviewConcern, ReaderDouble]:
    return {concern: ReaderDouble(status) for concern in REQUIRED_CONCERNS}


def _pass_all(service: ReviewService, cycle_id: str) -> None:
    for concern in REQUIRED_CONCERNS:
        service.record_reader(cycle_id, concern, ReaderStatus.PASS)


def test_cycle_starts_with_four_pending_readers_and_is_idempotent(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)

    started = service.start_cycle("PR-1", "abc123")
    repeated = service.start_cycle("PR-1", "abc123")

    assert started.cycle.cycle_number == 1
    assert started.cycle.status is ReviewCycleStatus.ACTIVE
    assert [reader.concern for reader in started.readers] == list(REQUIRED_CONCERNS)
    assert all(reader.status is ReaderStatus.PENDING for reader in started.readers)
    assert repeated.cycle.cycle_id == started.cycle.cycle_id
    assert repeated.merge_allowed is False


def test_structured_findings_keep_identity_anchor_staleness_and_resolution_evidence(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    first_cycle = service.start_cycle("PR-STRUCTURED", "head-1")
    first_snapshot = service.add_finding(
        first_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    first = first_snapshot.findings[0]
    repeated = service.add_finding(
        first_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    assert len(repeated.findings) == 1
    assert repeated.findings[0].finding_id == first.finding_id

    next_cycle = service.start_cycle("PR-STRUCTURED", "head-2")
    second_snapshot = service.add_finding(
        next_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    old = next(
        item for item in second_snapshot.findings if item.finding_id == first.finding_id
    )
    second = next(
        item for item in second_snapshot.findings if item.finding_id != first.finding_id
    )
    resolved = service.resolve_finding(first.finding_id, "Addressed", actor="operator")
    dismissed = next(
        item for item in resolved.findings if item.finding_id == first.finding_id
    )

    assert old.stale is True
    assert second.fingerprint == first.fingerprint
    assert second.head_sha == "head-2"
    assert second.first_seen_cycle_id == first_cycle.cycle.cycle_id
    assert second.file_path == "src/app.py"
    assert (second.start_line, second.end_line) == (12, 14)
    assert dismissed.resolution_actor == "operator"
    assert dismissed.resolution_at is not None
    with pytest.raises(ReviewError, match="Resolved findings cannot be published"):
        service.publish_finding(first.finding_id)


def test_finding_publication_retries_safely_and_deduplicates(
    review_store: ReviewStore,
) -> None:
    target = PullRequestTarget("owner/repo#1", "publish-head")
    provider = PublishingFixtureProvider(
        review_store, {target.pull_request_id: target}, fail_once=True
    )
    service = ReviewService(review_store, provider=provider)
    cycle = service.start_cycle(target.pull_request_id, target.head_sha)
    first_snapshot = service.add_finding(
        cycle.cycle.cycle_id, ReviewConcern.SECURITY, "Unsafe URL redirect."
    )
    first = first_snapshot.findings[0]

    retry = service.publish_finding(first.finding_id)
    retried_finding = next(
        item for item in retry.findings if item.finding_id == first.finding_id
    )
    assert retried_finding.publication_state is FindingPublicationState.RETRYABLE
    assert retried_finding.publication_retry_evidence == {"error_type": "RuntimeError"}

    published = service.publish_finding(first.finding_id)
    duplicate = service.add_finding(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Same issue reported by another reader.",
        duplicate_target=first.finding_id,
    )
    duplicate_finding = next(
        item for item in duplicate.findings if item.duplicate_target == first.finding_id
    )
    reconciled = service.publish_finding(duplicate_finding.finding_id)
    service.publish_finding(first.finding_id)
    canonical = next(
        item for item in reconciled.findings if item.finding_id == first.finding_id
    )
    duplicate_result = next(
        item
        for item in reconciled.findings
        if item.finding_id == duplicate_finding.finding_id
    )

    assert provider.publish_calls == 3
    assert provider.remote_ids == [None, None, "REMOTE-REVIEW-1"]
    assert provider.in_transaction_during_publish is False
    assert canonical.remote_id == "REMOTE-REVIEW-1"
    assert duplicate_result.publication_state is FindingPublicationState.DUPLICATE
    assert duplicate_result.remote_id == canonical.remote_id
    assert published.merge_allowed is False

    provider.publication_state = FindingPublicationState.REMOTE_MISSING
    missing = service.publish_finding(first.finding_id)
    missing_finding = next(
        item for item in missing.findings if item.finding_id == first.finding_id
    )
    assert provider.publish_calls == 4
    assert provider.remote_ids[-1] == "REMOTE-REVIEW-1"
    assert missing_finding.status is FindingStatus.OPEN
    assert missing_finding.resolution is None
    assert missing_finding.publication_state is FindingPublicationState.REMOTE_MISSING
    assert missing_finding.remote_id == "REMOTE-REVIEW-1"


def test_review_authorization_uses_a_closed_action_policy(
    review_store: ReviewStore,
) -> None:
    authorizer = AllowListReviewAuthorizer()
    assert authorizer.authorize("PR", "reader", "unknown") is False

    store = review_store
    service = ReviewService(store)
    with pytest.raises(ReviewError, match="Unknown review action"):
        service.authorize("PR", "reader", "unknown")


def test_reader_claims_are_durable_and_expire(review_store: ReviewStore) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-CLAIM", "head-1")
    claim = store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY)
    assert claim is not None
    assert store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY) is None
    with pytest.raises(ReviewError, match="no longer valid"):
        service.record_reader(
            cycle.cycle.cycle_id,
            ReviewConcern.SECURITY,
            ReaderStatus.PASS,
            claim_token="wrong",
        )

    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE review_readers
            SET claim_expires_at = ?
            WHERE cycle_id = ? AND concern = ?
            """,
            ("not-a-timestamp", cycle.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    renewed_claim = store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY)
    assert renewed_claim is not None
    service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.PASS,
        claim_token=renewed_claim,
    )
    assert store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY) is None
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        store.claim_reader("missing", ReviewConcern.SECURITY)

    newer_cycle = service.start_cycle("PR-CLAIM", "head-2")
    with pytest.raises(ReviewError, match="stale review cycle"):
        store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE)
    service.approve_for_merge(newer_cycle.cycle.cycle_id, "Operator approved")
    with pytest.raises(ReviewError, match="no longer accepting"):
        store.claim_reader(newer_cycle.cycle.cycle_id, ReviewConcern.SECURITY)


def test_concurrent_ready_reviews_claim_each_reader_once(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-CONCURRENT": PullRequestTarget("PR-CONCURRENT", "h1")}
    )
    started = Event()
    release = Event()
    readers = _reader_doubles()
    readers[ReviewConcern.SECURITY] = BlockingReader(started, release)
    first = ReviewService(store, provider=provider, readers=readers)
    second = ReviewService(store, provider=provider, readers=readers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.run_ready_review, "PR-CONCURRENT")
        assert started.wait(timeout=2)
        second_future = executor.submit(second.run_ready_review, "PR-CONCURRENT")
        second_result = second_future.result(timeout=2)
        release.set()
        first_result = first_future.result(timeout=2)

    assert first_result.cycle.status is ReviewCycleStatus.PASSED
    assert second_result.cycle.status is ReviewCycleStatus.ACTIVE
    security = next(
        reader
        for reader in second_result.readers
        if reader.concern is ReviewConcern.SECURITY
    )
    assert security.status is ReaderStatus.PENDING
    assert all(reader.calls == 1 for reader in readers.values())


def test_reader_failures_are_persisted_as_failed_results(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-READER-FAIL": PullRequestTarget("PR-READER-FAIL", "h1")}
    )
    readers = _reader_doubles()
    readers[ReviewConcern.SECURITY] = FailingReader()
    service = ReviewService(store, provider=provider, readers=readers)

    result = service.run_ready_review("PR-READER-FAIL")

    security = next(
        reader for reader in result.readers if reader.concern is ReviewConcern.SECURITY
    )
    assert result.cycle.status is ReviewCycleStatus.FAILED
    assert security.status is ReaderStatus.FAIL
    assert "reader failed" in result.findings[0].summary


def test_ready_review_rejects_malformed_provider_head_before_persistence(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-BAD-HEAD": PullRequestTarget("PR-BAD-HEAD", " ")})
    service = ReviewService(store, provider=provider, readers=_reader_doubles())

    with pytest.raises(ReviewError, match="Provider head SHA"):
        service.run_ready_review("PR-BAD-HEAD")
    with pytest.raises(ReviewError, match="No review cycle"):
        service.snapshot("PR-BAD-HEAD")


def test_stale_review_start_cannot_replace_a_newer_cycle(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    initial = service.start_cycle("PR-CAS", "head-0")
    newer = service._start_cycle(
        "PR-CAS", "head-2", expected_cycle_id=initial.cycle.cycle_id
    )

    with pytest.raises(ReviewError, match="changed while starting"):
        service._start_cycle(
            "PR-CAS", "head-1", expected_cycle_id=initial.cycle.cycle_id
        )
    assert service.snapshot("PR-CAS").cycle.cycle_id == newer.cycle.cycle_id


def test_stale_same_head_start_returns_the_current_cycle(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    initial = service._start_cycle("PR-SAME", "head-1", expected_cycle_id=None)

    repeated = service._start_cycle("PR-SAME", "head-1", expected_cycle_id=None)

    assert repeated.cycle.cycle_id == initial.cycle.cycle_id


def test_all_specialized_readers_pass_and_persist_for_merge(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    provider = FixtureProvider({"PR-2": PullRequestTarget("PR-2", "head-1")})
    first_store = ReviewStore(database)
    try:
        first_service = ReviewService(first_store, provider=provider)
        cycle = first_service.start_cycle("PR-2", "head-1")
        _pass_all(first_service, cycle.cycle.cycle_id)

        passed = first_service.snapshot("PR-2")
        provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-2")
        with pytest.raises(ReviewError, match="no current review authorization"):
            first_service.merge_handoff("PR-2", "head-1")
        provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-1")
        handoff = first_service.merge_handoff("PR-2", "head-1")
    finally:
        first_store.close()

    second_store = ReviewStore(database)
    try:
        resumed = ReviewService(second_store).snapshot("PR-2")
        assert passed.cycle.status is ReviewCycleStatus.PASSED
        assert passed.merge_allowed is True
        assert handoff.approved_by_human is False
        assert resumed.cycle.cycle_id == cycle.cycle.cycle_id
        assert all(reader.status is ReaderStatus.PASS for reader in resumed.readers)
    finally:
        second_store.close()


def test_failed_finding_reaches_writer_and_new_cycle_can_pass(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-3": PullRequestTarget("PR-3", "head-1")})
    service = ReviewService(store, provider=provider)
    first = service.start_cycle("PR-3", "head-1")
    failed = service.record_reader(
        first.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("SQL injection in the query filter",),
        reader="security-reader",
    )

    finding = failed.findings[0]
    assert failed.cycle.status is ReviewCycleStatus.FAILED
    assert failed.readers[0].status is ReaderStatus.FAIL
    assert failed.merge_allowed is False
    assert failed.as_dict()["writer_feedback"] == [finding.as_dict()]

    for concern in REQUIRED_CONCERNS[1:]:
        failed = service.record_reader(first.cycle.cycle_id, concern, ReaderStatus.PASS)
    assert failed.cycle.status is ReviewCycleStatus.FAILED
    assert failed.merge_allowed is False

    resolved = service.resolve_finding(finding.finding_id, "Parameterized the filter")
    assert resolved.findings[0].status is FindingStatus.RESOLVED
    assert resolved.merge_allowed is False

    second = service.start_cycle("PR-3", "head-1")
    assert second.cycle.cycle_number == 2
    assert (
        service.store.snapshot(first.cycle.cycle_id).cycle.status
        is ReviewCycleStatus.SUPERSEDED
    )
    _pass_all(service, second.cycle.cycle_id)
    assert service.merge_handoff("PR-3", "head-1").cycle_id == second.cycle.cycle_id


def test_stale_results_cannot_authorize_a_new_head(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider({"PR-4": PullRequestTarget("PR-4", "head-1")})
    service = ReviewService(store, provider=provider)
    first = service.start_cycle("PR-4", "head-1")
    _pass_all(service, first.cycle.cycle_id)
    provider.targets["PR-4"] = PullRequestTarget("PR-4", "head-2")
    second = service.start_cycle("PR-4", "head-2")

    with pytest.raises(ReviewError, match="stale"):
        service.record_reader(
            first.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="no current review authorization"):
        service.merge_handoff("PR-4", "head-1")
    with pytest.raises(ReviewError, match="stale"):
        service.approve_for_merge(first.cycle.cycle_id, "Old head approval")
    with pytest.raises(ReviewError, match="Awaiting"):
        service.merge_handoff("PR-4", "head-2")

    approved = service.approve_for_merge(
        second.cycle.cycle_id, "Operator reviewed the risk"
    )
    handoff = service.merge_handoff("PR-4", "head-2")
    assert approved.cycle.status is ReviewCycleStatus.HUMAN_APPROVED
    assert handoff.approved_by_human is True


def test_changed_finding_invalidates_a_previous_pass(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider({"PR-5": PullRequestTarget("PR-5", "head-1")})
    service = ReviewService(store, provider=provider)
    cycle = service.start_cycle("PR-5", "head-1")
    _pass_all(service, cycle.cycle.cycle_id)

    changed = service.add_finding(
        cycle.cycle.cycle_id,
        ReviewConcern.PERFORMANCE,
        "The query scans the full pull-request history",
    )
    assert changed.cycle.status is ReviewCycleStatus.FAILED
    assert changed.readers[-1].status is ReaderStatus.FAIL
    with pytest.raises(ReviewError, match="Writer must"):
        service.merge_handoff("PR-5", "head-1")

    finding = changed.findings[-1]
    service.resolve_finding(finding.finding_id, "Added an indexed query")
    with pytest.raises(ReviewError, match="Writer must"):
        service.merge_handoff("PR-5", "head-1")
    approved = service.approve_for_merge(
        cycle.cycle.cycle_id, "Accepted the residual risk"
    )
    assert approved.merge_allowed is True


def test_review_api_runs_reader_cycle_finding_and_handoff_paths(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-API": PullRequestTarget("PR-API", "h1")})

    def client_for(actor: str) -> TestClient:
        return TestClient(
            create_app(
                review_store=store,
                review_provider=provider,
                api_key="test-key",
                review_actor=actor,
            )
        )

    reader_client = client_for("reader")
    writer_client = client_for("writer")
    operator_client = client_for("operator")
    api_headers = {"X-API-Key": "test-key"}

    assert (
        writer_client.post(
            "/reviews/cycles",
            json={"pull_request_id": "PR-API", "head_sha": "h1"},
            headers=api_headers,
        ).status_code
        == 200
    )
    unauthorized = reader_client.get("/reviews/pull-requests/PR-API")
    assert unauthorized.status_code == 401
    started = reader_client.get("/reviews/pull-requests/PR-API", headers=api_headers)
    cycle_id = started.json()["cycle"]["cycle_id"]
    forged_actor = reader_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "forged"},
        headers={**api_headers, "X-Review-Actor": "operator"},
    )
    assert forged_actor.status_code == 409

    failed = reader_client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "security",
            "status": "fail",
            "findings": ["Unsafe redirect"],
        },
        headers=api_headers,
    )
    assert failed.status_code == 200
    finding_id = failed.json()["findings"][0]["finding_id"]
    assert failed.json()["writer_feedback"][0]["finding_id"] == finding_id
    pending = reader_client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={"concern": "test_coverage", "status": "pending"},
        headers=api_headers,
    )
    assert pending.status_code == 200
    assert pending.json()["readers"][1]["concern"] == "test_coverage"
    assert pending.json()["readers"][1]["status"] == "pending"
    added = writer_client.post(
        f"/reviews/cycles/{cycle_id}/findings",
        json={
            "concern": "clean_code",
            "summary": "Nested responsibility",
            "file_path": "src/review.py",
            "start_line": 4,
            "end_line": 6,
        },
        headers=api_headers,
    )
    assert added.status_code == 200
    assert added.json()["findings"][-1]["file_path"] == "src/review.py"
    assert added.json()["findings"][-1]["start_line"] == 4
    denied_publish = reader_client.post(
        f"/reviews/findings/{finding_id}/publish", headers=api_headers
    )
    assert denied_publish.status_code == 409
    retryable_publish = writer_client.post(
        f"/reviews/findings/{finding_id}/publish", headers=api_headers
    )
    assert retryable_publish.status_code == 200
    published_finding = next(
        item
        for item in retryable_publish.json()["findings"]
        if item["finding_id"] == finding_id
    )
    assert published_finding["publication"]["state"] == "retryable"
    resolved = writer_client.post(
        f"/reviews/findings/{finding_id}/resolve",
        json={"resolution": "Validated redirect target"},
        headers=api_headers,
    )
    assert resolved.status_code == 200
    blocked = operator_client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=api_headers,
    )
    assert blocked.status_code == 409

    denied_approval = reader_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=api_headers,
    )
    assert denied_approval.status_code == 409
    approved = operator_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=api_headers,
    )
    handoff = operator_client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=api_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["cycle"]["human_approval"] is True
    assert approved.json()["cycle"]["approval_actor"] == "operator"
    assert approved.json()["cycle"]["approval_reason"] == (
        "Human accepted the remaining findings"
    )
    assert handoff.status_code == 200
    assert handoff.json()["approved_by_human"] is True
    assert handoff.json()["approval_actor"] == "operator"


def test_ready_review_runs_all_injected_readers_and_requires_all_readers(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {
            "PR-READY": PullRequestTarget("PR-READY", "ready-1"),
            "PR-MISSING": PullRequestTarget("PR-MISSING", "missing-1"),
        }
    )
    readers = _reader_doubles()
    service = ReviewService(store, provider=provider, readers=readers)

    completed = service.run_ready_review("PR-READY")

    assert completed.cycle.status is ReviewCycleStatus.PASSED
    assert completed.merge_allowed is True
    assert all(reader.calls == 1 for reader in readers.values())
    assert all(reader.status is ReaderStatus.PASS for reader in completed.readers)

    repeated = service.run_ready_review("PR-READY")
    assert repeated.cycle.cycle_id == completed.cycle.cycle_id
    assert all(reader.calls == 1 for reader in readers.values())

    partial_readers = _reader_doubles()
    partial_readers.pop(ReviewConcern.PERFORMANCE)
    with pytest.raises(ReviewError, match="performance"):
        ReviewService(
            store, provider=provider, readers=partial_readers
        ).run_ready_review("PR-MISSING")

    with pytest.raises(ReviewError, match="provider"):
        ReviewService(store).run_ready_review("PR-READY")
    with pytest.raises(ReviewError, match="Unknown pull request"):
        service.run_ready_review("PR-UNKNOWN")
    provider.targets["PR-WRONG"] = PullRequestTarget("OTHER", "wrong-1")
    with pytest.raises(ReviewError, match="wrong pull request"):
        service.run_ready_review("PR-WRONG")

    provider.targets["PR-READY"] = PullRequestTarget("OTHER", "ready-1")
    with pytest.raises(ReviewError, match="wrong pull request"):
        service.start_cycle("PR-READY", "ready-1")
    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-1", ready=False)
    with pytest.raises(ReviewError, match="not ready"):
        service.start_cycle("PR-READY", "ready-1")
    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-2")
    with pytest.raises(ReviewError, match="does not match"):
        service.start_cycle("PR-READY", "ready-1")

    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-1", ready=False)
    with pytest.raises(ReviewError, match="not ready"):
        service.run_ready_review("PR-READY")


def test_review_ready_api_runs_reader_doubles(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-READY-API": PullRequestTarget("PR-READY-API", "api-ready-1")}
    )
    readers = _reader_doubles()
    client = TestClient(
        create_app(
            review_store=store,
            review_provider=provider,
            review_readers=readers,
            api_key="test-key",
            review_actor="writer",
        )
    )

    response = client.post(
        "/reviews/ready",
        json={"pull_request_id": "PR-READY-API"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["cycle"]["status"] == "passed"
    assert all(reader.calls == 1 for reader in readers.values())


def test_review_api_persists_github_evidence_references(
    review_store: ReviewStore,
) -> None:
    target = PullRequestTarget(
        "owner/repo#8",
        "evidence-head",
        evidence_json=(
            '{"pull_request":{"id":"PULL_REQUEST_NODE"},'
            '"reviews":[{"id":"REVIEW_NODE"}]}'
        ),
    )
    service = ReviewService(
        review_store,
        provider=FixtureProvider({target.pull_request_id: target}),
        readers=_reader_doubles(),
    )
    client = TestClient(
        create_app(review_service=service, api_key="test-key", review_actor="operator")
    )
    headers = {"X-API-Key": "test-key"}
    started = client.post(
        "/reviews/cycles",
        json={"pull_request_id": target.pull_request_id, "head_sha": target.head_sha},
        headers=headers,
    )
    assert started.status_code == 200
    cycle_id = started.json()["cycle"]["cycle_id"]
    assert started.json()["cycle"]["github_evidence"]["pull_request"]["id"] == (
        "PULL_REQUEST_NODE"
    )

    recorded = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "security",
            "status": "pass",
            "evidence_refs": ["PULL_REQUEST_NODE"],
        },
        headers=headers,
    )
    assert recorded.status_code == 200
    security = next(
        item for item in recorded.json()["readers"] if item["concern"] == "security"
    )
    assert security["evidence_refs"] == ["PULL_REQUEST_NODE"]

    finding = client.post(
        f"/reviews/cycles/{cycle_id}/findings",
        json={
            "concern": "test_coverage",
            "summary": "Coverage gap",
            "evidence_refs": ["REVIEW_NODE"],
        },
        headers=headers,
    )
    assert finding.status_code == 200
    assert finding.json()["findings"][0]["evidence_refs"] == ["REVIEW_NODE"]

    invalid = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "performance",
            "status": "pass",
            "evidence_refs": ["NOT_IN_SNAPSHOT"],
        },
        headers=headers,
    )
    assert invalid.status_code == 409


def test_review_api_maps_provider_adapter_failures_to_dependency_errors(
    review_store: ReviewStore,
) -> None:
    store = review_store
    client = TestClient(
        create_app(
            review_store=store,
            review_provider=ExplodingProvider(),
            review_readers=_reader_doubles(),
            api_key="test-key",
            review_actor="writer",
        )
    )

    response = client.post(
        "/reviews/ready",
        json={"pull_request_id": "PR-FAIL"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Pull-request provider failed"


def test_review_api_requires_a_configured_server_actor(
    review_store: ReviewStore,
) -> None:
    client = TestClient(create_app(review_store=review_store, api_key="test-key"))

    response = client.get(
        "/reviews/pull-requests/PR-ACTOR",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Review actor is not configured"


def test_default_production_review_adapters_require_token_at_startup(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with (
        pytest.raises(RuntimeError, match="GITHUB_TOKEN or GH_TOKEN"),
        TestClient(
            create_app(
                review_store=review_store,
                require_review_adapters=True,
            )
        ),
    ):
        pass


def test_default_production_review_adapters_start_with_read_token(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.setenv("GITHUB_TOKEN", "test-read-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    app = create_app(
        review_store=review_store,
        api_key="test-key",
        review_actor="operator",
        require_review_adapters=True,
    )
    with TestClient(app):
        pass


def test_merge_provider_io_runs_outside_the_review_write_transaction(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = LockInspectingProvider(
        store, {"PR-LOCK": PullRequestTarget("PR-LOCK", "h1")}
    )
    service = ReviewService(store, provider=provider)
    cycle = service.start_cycle("PR-LOCK", "h1")
    _pass_all(service, cycle.cycle.cycle_id)

    handoff = service.merge_handoff("PR-LOCK", "h1")

    assert handoff.cycle_id == cycle.cycle.cycle_id
    assert provider.in_transaction_during_provider_call is False


def test_merge_handoff_rechecks_the_cycle_after_provider_io(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FixtureProvider({"PR-RACE": PullRequestTarget("PR-RACE", "h1")})
    service = ReviewService(review_store, provider=provider)
    cycle = service.start_cycle("PR-RACE", "h1")
    _pass_all(service, cycle.cycle.cycle_id)

    original_current_cycle_row = review_store.current_cycle_row
    calls = 0

    def disappearing_current_cycle(
        connection: sqlite3.Connection, pull_request_id: str
    ) -> sqlite3.Row | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_current_cycle_row(connection, pull_request_id)
        return None

    monkeypatch.setattr(review_store, "current_cycle_row", disappearing_current_cycle)
    with pytest.raises(ReviewError, match="No review cycle"):
        service.merge_handoff("PR-RACE", "h1")


def test_review_store_migrates_approval_and_reader_claim_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-reviews.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE review_cycles(
            cycle_id TEXT PRIMARY KEY, pull_request_id TEXT NOT NULL,
            head_sha TEXT NOT NULL, cycle_number INTEGER NOT NULL,
            status TEXT NOT NULL, human_approval INTEGER NOT NULL DEFAULT 0,
            required_action TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(pull_request_id, cycle_number)
        );
        CREATE TABLE review_readers(
            cycle_id TEXT NOT NULL, concern TEXT NOT NULL, status TEXT NOT NULL,
            finding_ids_json TEXT NOT NULL, reader TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(cycle_id, concern)
        );
        CREATE TABLE review_findings(
            finding_id TEXT PRIMARY KEY, pull_request_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL, concern TEXT NOT NULL, summary TEXT NOT NULL,
            status TEXT NOT NULL, resolution TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO review_cycles(
            cycle_id, pull_request_id, head_sha, cycle_number, status,
            human_approval, required_action, created_at, updated_at
        ) VALUES (
            'legacy-cycle', 'owner/repo#1', 'legacy-head', 1, 'failed',
            0, NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        INSERT INTO review_findings(
            finding_id, pull_request_id, cycle_id, concern, summary, status,
            resolution, created_at, updated_at
        ) VALUES (
            'legacy-finding', 'owner/repo#1', 'legacy-cycle', 'security',
            'Legacy finding', 'open', NULL,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.close()

    store = ReviewStore(database)
    try:
        cycle_columns = {
            str(row["name"])
            for row in store._connection.execute("PRAGMA table_info(review_cycles)")
        }
        reader_columns = {
            str(row["name"])
            for row in store._connection.execute("PRAGMA table_info(review_readers)")
        }
        assert {"approval_actor", "approval_reason", "approval_at"} <= cycle_columns
        assert {"claim_token", "claim_expires_at"} <= reader_columns
        finding = store.finding_for_id("legacy-finding")
        assert finding.head_sha == "legacy-head"
        assert finding.fingerprint
        assert finding.first_seen_cycle_id == "legacy-cycle"
        assert finding.publication_state is FindingPublicationState.UNPUBLISHED
    finally:
        store.close()


def test_review_authorization_and_lookup_errors_are_explicit(
    review_store: ReviewStore,
) -> None:
    authorizer = AllowListReviewAuthorizer()
    assert authorizer.authorize("PR", " ", "read") is False
    assert authorizer.authorize("PR", "reader", "reader") is True
    assert authorizer.authorize("PR", "reader", "writer") is False
    assert authorizer.authorize("PR", "writer", "handoff") is False

    store = review_store
    service = ReviewService(store)
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.pull_request_id_for_cycle("missing")
    with pytest.raises(ReviewError, match="Unknown review finding"):
        service.pull_request_id_for_finding("missing")
    with pytest.raises(ReviewError, match="provider"):
        service.merge_handoff("PR-MISSING", "head-1")

    cycle = service.start_cycle("PR-NO-PROVIDER", "head-1")
    with pytest.raises(ReviewError, match="provider"):
        service.merge_handoff("PR-NO-PROVIDER", "head-2")
    assert cycle.cycle.pull_request_id == "PR-NO-PROVIDER"
    provider_store = ReviewStore()
    try:
        provider_service = ReviewService(
            provider_store,
            provider=FixtureProvider(
                {"PR-MISSING": PullRequestTarget("PR-MISSING", "head-1")}
            ),
        )
        with pytest.raises(ReviewError, match="No review cycle"):
            provider_service.merge_handoff("PR-MISSING", "head-1")
    finally:
        provider_store.close()


def test_review_rejects_invalid_or_stale_transitions(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-ERR", "head-1")

    with pytest.raises(ReviewError, match="required"):
        service.start_cycle(" ", "head-1")
    with pytest.raises(ReviewError, match="at most"):
        service.start_cycle("PR-LONG", "x" * 201)
    with pytest.raises(ReviewError, match="invalid format"):
        service.start_cycle("PR-INVALID", "head*1")
    with pytest.raises(ReviewError, match="Unknown review concern"):
        service.record_reader(cycle.cycle.cycle_id, "unknown", ReaderStatus.PASS)
    with pytest.raises(ReviewError, match="Unknown reader status"):
        service.record_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY, "unknown")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.record_reader("missing", ReviewConcern.SECURITY, ReaderStatus.PASS)
    with pytest.raises(ReviewError, match="failed reader"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.FAIL
        )
    with pytest.raises(ReviewError, match="Only a failed"):
        service.record_reader(
            cycle.cycle.cycle_id,
            ReviewConcern.TEST_COVERAGE,
            ReaderStatus.PASS,
            ("not allowed",),
        )
    service.record_reader(
        cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
    )
    with pytest.raises(ReviewError, match="already recorded"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="Unknown review finding"):
        service.resolve_finding("missing", "none")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.approve_for_merge("missing", "reason")
    with pytest.raises(ReviewError, match="approval reason"):
        service.approve_for_merge(cycle.cycle.cycle_id, " ")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.add_finding("missing", ReviewConcern.SECURITY, "finding")
    service.approve_for_merge(cycle.cycle.cycle_id, "Operator approval")
    with pytest.raises(ReviewError, match="no longer accepting reader"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="no longer accepting findings"):
        service.add_finding(
            cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE, "finding"
        )


def test_review_reports_stale_and_missing_reader_records(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    first = service.start_cycle("PR-STALE", "head-1")
    second = service.start_cycle("PR-STALE", "head-2")
    with pytest.raises(ReviewError, match="stale"):
        service.add_finding(first.cycle.cycle_id, ReviewConcern.SECURITY, "old finding")

    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM review_readers WHERE cycle_id = ? AND concern = ?",
            (second.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    with pytest.raises(ReviewError, match="No reader"):
        service.record_reader(
            second.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="No reader"):
        store.claim_reader(second.cycle.cycle_id, ReviewConcern.SECURITY)
    with pytest.raises(ReviewError, match="No reader"):
        service.add_finding(
            second.cycle.cycle_id, ReviewConcern.SECURITY, "missing reader"
        )


def test_review_store_rejects_corrupt_reader_finding_json(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-CORRUPT", "head-1")
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE review_readers SET finding_ids_json = ?
            WHERE cycle_id = ? AND concern = ?
            """,
            ("{bad", cycle.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    with pytest.raises(ReviewError, match="Stored reader findings"):
        store.snapshot(cycle.cycle.cycle_id)
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE review_readers SET finding_ids_json = ?
            WHERE cycle_id = ? AND concern = ?
            """,
            ('"not-a-list"', cycle.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    with pytest.raises(ReviewError, match="Stored reader findings"):
        store.snapshot(cycle.cycle.cycle_id)
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE review_readers SET finding_ids_json = ?
            WHERE cycle_id = ? AND concern = ?
            """,
            ("[1]", cycle.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    with pytest.raises(ReviewError, match="Stored reader findings"):
        store.snapshot(cycle.cycle.cycle_id)


def test_review_store_reports_unknown_cycles_and_missing_current_cycle(
    monkeypatch: pytest.MonkeyPatch,
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-MISSING", "head-1")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        store.snapshot("missing")
    with pytest.raises(ReviewError, match="No review cycle"):
        service.snapshot("missing")
    service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("finding",),
    )
    finding_id = service.snapshot("PR-MISSING").findings[0].finding_id
    monkeypatch.setattr(store, "current_cycle_row", lambda _connection, _pr: None)
    with pytest.raises(ReviewError, match="No current review cycle"):
        service.resolve_finding(finding_id, "resolution")


def test_create_app_rejects_conflicting_review_store() -> None:
    service_store = ReviewStore()
    api_store = ReviewStore()
    try:
        with pytest.raises(ValueError, match="share one review store"):
            create_app(
                review_service=ReviewService(service_store),
                review_store=api_store,
            )
    finally:
        service_store.close()
        api_store.close()


def test_create_app_rejects_adapters_with_existing_review_service(
    review_store: ReviewStore,
) -> None:
    store = review_store
    with pytest.raises(ValueError, match="adapters"):
        create_app(
            review_service=ReviewService(store),
            review_provider=FixtureProvider({}),
        )
