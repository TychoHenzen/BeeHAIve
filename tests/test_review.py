from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    REQUIRED_CONCERNS,
    AllowListReviewAuthorizer,
    FindingStatus,
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


def _reader_doubles(
    status: ReaderStatus = ReaderStatus.PASS,
) -> dict[ReviewConcern, ReaderDouble]:
    return {concern: ReaderDouble(status) for concern in REQUIRED_CONCERNS}


def _pass_all(service: ReviewService, cycle_id: str) -> None:
    for concern in REQUIRED_CONCERNS:
        service.record_reader(cycle_id, concern, ReaderStatus.PASS)


def test_cycle_starts_with_four_pending_readers_and_is_idempotent() -> None:
    store = ReviewStore()
    service = ReviewService(store)

    started = service.start_cycle("PR-1", "abc123")
    repeated = service.start_cycle("PR-1", "abc123")

    assert started.cycle.cycle_number == 1
    assert started.cycle.status is ReviewCycleStatus.ACTIVE
    assert [reader.concern for reader in started.readers] == list(REQUIRED_CONCERNS)
    assert all(reader.status is ReaderStatus.PENDING for reader in started.readers)
    assert repeated.cycle.cycle_id == started.cycle.cycle_id
    assert repeated.merge_allowed is False
    store.close()


def test_all_specialized_readers_pass_and_persist_for_merge(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    provider = FixtureProvider({"PR-2": PullRequestTarget("PR-2", "head-1")})
    first_store = ReviewStore(database)
    first_service = ReviewService(first_store, provider=provider)
    cycle = first_service.start_cycle("PR-2", "head-1")
    _pass_all(first_service, cycle.cycle.cycle_id)

    passed = first_service.snapshot("PR-2")
    provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-2")
    with pytest.raises(ReviewError, match="no current review authorization"):
        first_service.merge_handoff("PR-2", "head-1")
    provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-1")
    handoff = first_service.merge_handoff("PR-2", "head-1")
    first_store.close()

    second_store = ReviewStore(database)
    resumed = ReviewService(second_store).snapshot("PR-2")
    assert passed.cycle.status is ReviewCycleStatus.PASSED
    assert passed.merge_allowed is True
    assert handoff.approved_by_human is False
    assert resumed.cycle.cycle_id == cycle.cycle.cycle_id
    assert all(reader.status is ReaderStatus.PASS for reader in resumed.readers)
    second_store.close()


def test_failed_finding_reaches_writer_and_new_cycle_can_pass() -> None:
    store = ReviewStore()
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
    store.close()


def test_stale_results_cannot_authorize_a_new_head() -> None:
    store = ReviewStore()
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
    store.close()


def test_changed_finding_invalidates_a_previous_pass() -> None:
    store = ReviewStore()
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
    store.close()


def test_review_api_runs_reader_cycle_finding_and_handoff_paths() -> None:
    store = ReviewStore()
    provider = FixtureProvider({"PR-API": PullRequestTarget("PR-API", "h1")})
    client = TestClient(
        create_app(review_store=store, review_provider=provider, api_key="test-key")
    )
    reader_auth = {"X-API-Key": "test-key", "X-Review-Actor": "reader"}
    writer_auth = {"X-API-Key": "test-key", "X-Review-Actor": "writer"}
    operator_auth = {"X-API-Key": "test-key", "X-Review-Actor": "operator"}

    assert (
        client.post(
            "/reviews/cycles",
            json={"pull_request_id": "PR-API", "head_sha": "h1"},
            headers=writer_auth,
        ).status_code
        == 200
    )
    unauthorized = client.get("/reviews/pull-requests/PR-API")
    assert unauthorized.status_code == 401
    missing_actor = client.get(
        "/reviews/pull-requests/PR-API", headers={"X-API-Key": "test-key"}
    )
    assert missing_actor.status_code == 401
    started = client.get("/reviews/pull-requests/PR-API", headers=reader_auth)
    cycle_id = started.json()["cycle"]["cycle_id"]

    failed = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "security",
            "status": "fail",
            "findings": ["Unsafe redirect"],
        },
        headers=reader_auth,
    )
    assert failed.status_code == 200
    finding_id = failed.json()["findings"][0]["finding_id"]
    assert failed.json()["writer_feedback"][0]["finding_id"] == finding_id
    pending = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={"concern": "test_coverage", "status": "pending"},
        headers=reader_auth,
    )
    assert pending.status_code == 200
    added = client.post(
        f"/reviews/cycles/{cycle_id}/findings",
        json={"concern": "clean_code", "summary": "Nested responsibility"},
        headers=writer_auth,
    )
    assert added.status_code == 200
    resolved = client.post(
        f"/reviews/findings/{finding_id}/resolve",
        json={"resolution": "Validated redirect target"},
        headers=writer_auth,
    )
    assert resolved.status_code == 200
    blocked = client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=operator_auth,
    )
    assert blocked.status_code == 409

    denied_approval = client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=reader_auth,
    )
    assert denied_approval.status_code == 409
    approved = client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=operator_auth,
    )
    handoff = client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=operator_auth,
    )
    assert approved.status_code == 200
    assert approved.json()["cycle"]["human_approval"] is True
    assert handoff.status_code == 200
    assert handoff.json()["approved_by_human"] is True
    store.close()


def test_ready_review_runs_all_injected_readers_and_requires_all_readers() -> None:
    store = ReviewStore()
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
    store.close()


def test_review_ready_api_runs_reader_doubles() -> None:
    store = ReviewStore()
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
        )
    )

    response = client.post(
        "/reviews/ready",
        json={"pull_request_id": "PR-READY-API"},
        headers={"X-API-Key": "test-key", "X-Review-Actor": "writer"},
    )

    assert response.status_code == 200
    assert response.json()["cycle"]["status"] == "passed"
    assert all(reader.calls == 1 for reader in readers.values())
    store.close()


def test_review_authorization_and_lookup_errors_are_explicit() -> None:
    authorizer = AllowListReviewAuthorizer()
    assert authorizer.authorize("PR", " ", "read") is False
    assert authorizer.authorize("PR", "reader", "reader") is True
    assert authorizer.authorize("PR", "reader", "writer") is False
    assert authorizer.authorize("PR", "writer", "handoff") is False

    store = ReviewStore()
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
    store.close()

    provider_store = ReviewStore()
    provider_service = ReviewService(
        provider_store,
        provider=FixtureProvider(
            {"PR-MISSING": PullRequestTarget("PR-MISSING", "head-1")}
        ),
    )
    with pytest.raises(ReviewError, match="No review cycle"):
        provider_service.merge_handoff("PR-MISSING", "head-1")
    provider_store.close()


def test_review_rejects_invalid_or_stale_transitions() -> None:
    store = ReviewStore()
    service = ReviewService(store)
    cycle = service.start_cycle("PR-ERR", "head-1")

    with pytest.raises(ReviewError, match="required"):
        service.start_cycle(" ", "head-1")
    with pytest.raises(ReviewError, match="at most"):
        service.start_cycle("PR-LONG", "x" * 201)
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
    store.close()


def test_review_reports_stale_and_missing_reader_records() -> None:
    store = ReviewStore()
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
        service.add_finding(
            second.cycle.cycle_id, ReviewConcern.SECURITY, "missing reader"
        )
    store.close()


def test_review_store_rejects_corrupt_reader_finding_json() -> None:
    store = ReviewStore()
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
    store.close()


def test_review_store_reports_unknown_cycles_and_missing_current_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReviewStore()
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
    store.close()


def test_create_app_rejects_conflicting_review_store() -> None:
    service_store = ReviewStore()
    api_store = ReviewStore()
    with pytest.raises(ValueError, match="share one review store"):
        create_app(
            review_service=ReviewService(service_store),
            review_store=api_store,
        )
    service_store.close()
    api_store.close()


def test_create_app_rejects_adapters_with_existing_review_service() -> None:
    store = ReviewStore()
    with pytest.raises(ValueError, match="adapters"):
        create_app(
            review_service=ReviewService(store),
            review_provider=FixtureProvider({}),
        )
    store.close()
