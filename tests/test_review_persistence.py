import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from beehaiive.review import (
    FindingPublicationState,
    ReaderStatus,
    ReviewConcern,
    ReviewError,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
    ReviewService,
    ReviewStore,
)


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
        CREATE TABLE review_repair_attempts(
            attempt_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL UNIQUE,
            pull_request_id TEXT NOT NULL, head_sha TEXT NOT NULL,
            finding_ids_json TEXT NOT NULL, actor TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            lease_id TEXT, commit_sha TEXT, push_evidence_json TEXT, result TEXT,
            required_action TEXT, cancellation_requested INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
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
        INSERT INTO review_repair_attempts(
            attempt_id, cycle_id, pull_request_id, head_sha, finding_ids_json,
            actor, status, created_at, updated_at
        ) VALUES (
            'legacy-attempt', 'legacy-cycle', 'owner/repo#1', 'legacy-head',
            '[]', 'operator', 'succeeded',
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
        repair_columns = {
            str(row["name"])
            for row in store._connection.execute(
                "PRAGMA table_info(review_repair_attempts)"
            )
        }
        assert {"approval_actor", "approval_reason", "approval_at"} <= cycle_columns
        assert {"claim_token", "claim_expires_at"} <= reader_columns
        assert {
            "review_transition_status",
            "review_transition_cycle_id",
            "review_transition_required_action",
        } <= repair_columns
        legacy_attempt = store.repair_attempt("legacy-attempt")
        assert legacy_attempt.status is ReviewRepairStatus.SUCCEEDED
        assert legacy_attempt.review_transition_status is (
            ReviewRepairTransitionStatus.NOT_REQUIRED
        )
        finding = store.finding_for_id("legacy-finding")
        assert finding.head_sha == "legacy-head"
        assert finding.fingerprint
        assert finding.first_seen_cycle_id == "legacy-cycle"
        assert finding.publication_state is FindingPublicationState.UNPUBLISHED
    finally:
        store.close()


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


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
