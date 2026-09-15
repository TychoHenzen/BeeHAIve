from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.scheduler import (
    AccountUsageSnapshot,
    AgentScheduler,
    AllowanceBucket,
    BudgetAction,
    BudgetDecision,
    BudgetEvidence,
    BudgetPolicy,
    BudgetReason,
    SchedulerConfig,
    evaluate_budget,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.scheduler.fake_orchestrator import FakeOrchestrator
from tests.support.scheduler.fake_worker import FakeWorker


def _snapshot(
    *,
    evidence: BudgetEvidence = BudgetEvidence.FRESH,
    source_version: str = "v1",
    observed_at: datetime | None = None,
    remaining: float | None = 10,
    reset_at: str | None = None,
    models: dict[str, bool] | None = None,
) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        source_id="account",
        source_version=source_version,
        observed_at=(observed_at or datetime.now(UTC)).isoformat(),
        evidence=evidence,
        buckets=(
            AllowanceBucket(
                "five-hour",
                remaining=remaining,
                limit=100,
                reset_at=reset_at,
                model_availability=models or {},
            ),
        ),
    )


def test_budget_snapshot_round_trip_and_validation() -> None:
    snapshot = _snapshot(models={"sol": True})
    restored = AccountUsageSnapshot.from_dict(snapshot.as_dict())
    assert restored == snapshot
    assert "credentials" not in snapshot.as_dict()
    multi = AccountUsageSnapshot(
        "account",
        "multi",
        datetime.now(UTC).isoformat(),
        BudgetEvidence.FRESH,
        (
            AllowanceBucket("five-hour", remaining=10, credits=2),
            AllowanceBucket("weekly", remaining=20),
        ),
    )
    assert len(multi.buckets) == 2
    with pytest.raises(ValueError, match="timezone"):
        AllowanceBucket("five-hour", reset_at="tomorrow")
    with pytest.raises(ValueError, match="unique"):
        AccountUsageSnapshot(
            "account",
            "v1",
            datetime.now(UTC).isoformat(),
            BudgetEvidence.FRESH,
            (AllowanceBucket("one"), AllowanceBucket("one")),
        )
    with pytest.raises(ValueError, match="downgrade_model"):
        BudgetPolicy(downgrade_remaining=1)
    with pytest.raises(ValueError, match="downgrade_remaining"):
        BudgetPolicy(downgrade_model="sol", downgrade_remaining=True)
    with pytest.raises(ValueError, match="reset_at"):
        AllowanceBucket("five-hour", reset_at="x" * 129)
    with pytest.raises(ValueError, match="observed_at"):
        AccountUsageSnapshot("account", "v1", "x" * 129, BudgetEvidence.FRESH)
    with pytest.raises(ValueError, match="mappings"):
        AccountUsageSnapshot.from_dict(
            {
                "source_id": "account",
                "source_version": "v1",
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence": "fresh",
                "buckets": ["invalid"],
            }
        )
    with pytest.raises(ValueError, match="reset_at"):
        AccountUsageSnapshot.from_dict(
            {
                "source_id": "account",
                "source_version": "v1",
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence": "fresh",
                "buckets": [{"bucket_id": "x", "reset_at": 123}],
            }
        )
    with pytest.raises(ValueError, match="finite"):
        AccountUsageSnapshot.from_dict(
            {
                "source_id": "account",
                "source_version": "v1",
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence": "fresh",
                "buckets": [{"bucket_id": "x", "remaining": True}],
            }
        )


def test_budget_evaluation_fails_closed_for_unsafe_evidence() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    policy = BudgetPolicy(freshness_seconds=300)
    for evidence, reason in (
        (BudgetEvidence.STALE, BudgetReason.EVIDENCE_STALE),
        (BudgetEvidence.UNAVAILABLE, BudgetReason.EVIDENCE_UNAVAILABLE),
        (BudgetEvidence.CONTRADICTORY, BudgetReason.EVIDENCE_CONTRADICTORY),
    ):
        decision = evaluate_budget(
            _snapshot(evidence=evidence, observed_at=now), policy, now=now
        )
        assert decision.action is BudgetAction.PAUSE
        assert decision.reason is reason
    assert (
        evaluate_budget(
            _snapshot(observed_at=now - timedelta(seconds=301)), policy, now=now
        ).reason
        is BudgetReason.EVIDENCE_STALE
    )
    assert (
        evaluate_budget(
            AccountUsageSnapshot(
                "account", "v1", now.isoformat(), BudgetEvidence.FRESH
            ),
            policy,
            now=now,
        ).reason
        is BudgetReason.BUCKETS_MISSING
    )
    assert (
        evaluate_budget(
            _snapshot(observed_at=now, remaining=101), policy, now=now
        ).reason
        is BudgetReason.BUCKET_VALUES_INVALID
    )
    assert (
        evaluate_budget(
            AccountUsageSnapshot(
                "account",
                "v1",
                now.isoformat(),
                BudgetEvidence.FRESH,
                (AllowanceBucket("x", remaining=20, used=90, limit=100),),
            ),
            policy,
            now=now,
        ).reason
        is BudgetReason.BUCKET_VALUES_INVALID
    )


def test_budget_evaluation_allows_and_downgrades_only_with_explicit_availability() -> (
    None
):
    now = datetime(2026, 9, 15, tzinfo=UTC)
    policy = BudgetPolicy(
        freshness_seconds=300,
        downgrade_model="sol",
        downgrade_remaining=5,
    )
    allowed = evaluate_budget(_snapshot(observed_at=now, remaining=20), policy, now=now)
    assert allowed.action is BudgetAction.ALLOW
    downgraded = evaluate_budget(
        _snapshot(observed_at=now, remaining=3, models={"sol": True}), policy, now=now
    )
    assert downgraded.action is BudgetAction.DOWNGRADE
    assert downgraded.fallback_model == "sol"
    unavailable = evaluate_budget(
        _snapshot(observed_at=now, remaining=0, models={"sol": False}),
        policy,
        now=now,
    )
    assert unavailable.action is BudgetAction.PAUSE
    assert unavailable.reason is BudgetReason.MODEL_UNAVAILABLE


def test_budget_reset_requires_new_revision_and_post_reset_availability() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    previous = _snapshot(
        evidence=BudgetEvidence.STALE,
        source_version="v1",
        observed_at=now - timedelta(hours=1),
        remaining=0,
        reset_at=(now - timedelta(minutes=1)).isoformat(),
    )
    current = _snapshot(
        source_version="v2", observed_at=now - timedelta(seconds=1), remaining=50
    )
    decision = evaluate_budget(current, BudgetPolicy(), previous, now=now)
    assert decision.action is BudgetAction.RESUME
    assert decision.reason is BudgetReason.RESET_VERIFIED
    low_after_reset = _snapshot(
        source_version="v2-low",
        observed_at=now - timedelta(seconds=1),
        remaining=3,
        models={"sol": True},
    )
    assert (
        evaluate_budget(
            low_after_reset,
            BudgetPolicy(downgrade_model="sol", downgrade_remaining=5),
            previous,
            now=now,
        ).action
        is BudgetAction.DOWNGRADE
    )
    unverified = evaluate_budget(
        _snapshot(source_version="v1", observed_at=now, remaining=50),
        BudgetPolicy(),
        previous,
        now=now,
    )
    assert unverified.action is BudgetAction.PAUSE
    assert unverified.reason is BudgetReason.RESET_UNVERIFIED
    out_of_order = evaluate_budget(
        _snapshot(
            source_version="v3", observed_at=now - timedelta(seconds=2), remaining=50
        ),
        BudgetPolicy(),
        current,
        now=now,
    )
    assert out_of_order.reason is BudgetReason.EVIDENCE_CONTRADICTORY
    fresh_exhausted = _snapshot(
        source_version="v4",
        observed_at=now - timedelta(hours=1),
        remaining=0,
        reset_at=(now - timedelta(minutes=1)).isoformat(),
    )
    fresh_resume = _snapshot(
        source_version="v5", observed_at=now - timedelta(seconds=1), remaining=50
    )
    assert (
        evaluate_budget(fresh_resume, BudgetPolicy(), fresh_exhausted, now=now).action
        is BudgetAction.RESUME
    )
    future_reset = _snapshot(
        source_version="v6",
        observed_at=now - timedelta(seconds=1),
        remaining=0,
        reset_at=(now + timedelta(minutes=5)).isoformat(),
    )
    assert (
        evaluate_budget(
            _snapshot(source_version="v7", observed_at=now, remaining=50),
            BudgetPolicy(),
            future_reset,
            now=now,
        ).reason
        is BudgetReason.RESET_UNVERIFIED
    )
    other_source = AccountUsageSnapshot(
        "other-account",
        "v8",
        now.isoformat(),
        BudgetEvidence.FRESH,
        (AllowanceBucket("five-hour", remaining=50),),
    )
    assert (
        evaluate_budget(other_source, BudgetPolicy(), previous, now=now).reason
        is BudgetReason.EVIDENCE_CONTRADICTORY
    )


def test_budget_evidence_persists_replays_and_retains_latest_records(
    tmp_path: Path,
) -> None:
    database = tmp_path / "budget.sqlite3"
    store = OrchestratorStore(database)
    store.sync_project(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "one"),)),),
        )
    )
    policy = BudgetPolicy()
    first_snapshot = _snapshot(source_version="first")
    first_decision = evaluate_budget(first_snapshot, policy)
    first = store.record_budget_decision("project-1", first_snapshot, first_decision)
    replay = store.record_budget_decision("project-1", first_snapshot, first_decision)
    assert replay["replay_id"] == first["replay_id"]
    assert len(store.budget_evidence_for("project-1")) == 1
    assert store.budget_snapshot_for_project("project-1") == first_snapshot
    low_snapshot = _snapshot(source_version="low", remaining=3, models={"sol": True})
    low_decision = evaluate_budget(
        low_snapshot,
        BudgetPolicy(downgrade_model="sol", downgrade_remaining=5),
    )
    store.record_budget_decision("project-1", low_snapshot, low_decision)
    assert store.budget_evidence_for("project-1")[-1]["fallback_model"] == "sol"
    for marker in range(105):
        snapshot = _snapshot(source_version=f"v-{marker}")
        store.record_budget_decision(
            "project-1", snapshot, evaluate_budget(snapshot, policy)
        )
    evidence = store.budget_evidence_for("project-1")
    assert len(evidence) == 100
    assert len({item["replay_id"] for item in evidence}) == 100
    store.close()
    reopened = OrchestratorStore(database)
    assert len(reopened.budget_evidence_for("project-1")) == 100
    reopened._connection.execute(
        "UPDATE budget_decision_evidence SET snapshot_json = '{}'"
    )
    with pytest.raises(StoreError, match="budget evidence is invalid"):
        reopened.budget_snapshot_for_project("project-1")
    reopened._connection.execute(
        "UPDATE budget_decision_evidence SET snapshot_json = '[]'"
    )
    with pytest.raises(StoreError, match="budget evidence is invalid"):
        reopened.budget_snapshot_for_project("project-1")
    reopened.close()


class _BudgetAdapter:
    def __init__(self, snapshot: AccountUsageSnapshot) -> None:
        self.current = snapshot

    def snapshot(self, project_id: str) -> AccountUsageSnapshot:
        return self.current


class _FlakyBudgetAdapter(_BudgetAdapter):
    def __init__(self, snapshot: AccountUsageSnapshot) -> None:
        super().__init__(snapshot)
        self.calls = 0

    def snapshot(self, project_id: str) -> AccountUsageSnapshot:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider failure")
        return self.current


def test_scheduler_budget_gate_pauses_new_claims_and_reports_decision() -> None:
    now = datetime.now(UTC)
    orchestrator = FakeOrchestrator(
        {"project": {"repositories": [{"name": "owner/api", "active": True}]}}
    )
    worker = FakeWorker()
    scheduler = AgentScheduler(
        orchestrator,
        worker,
        {"project"},
        SchedulerConfig(enabled=True),
        budget_adapter=_BudgetAdapter(
            _snapshot(evidence=BudgetEvidence.UNAVAILABLE, observed_at=now)
        ),
    )
    assert scheduler.poll_once() == ()
    assert worker.claims == []
    assert scheduler.status_for("project")["budget"]["action"] == "pause"  # type: ignore[index]


def test_scheduler_keeps_reset_denial_after_restart(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    orchestrator = FakeOrchestrator({"project": {"repositories": []}})
    store = OrchestratorStore(tmp_path / "scheduler-budget.sqlite3")
    store.sync_project(
        ProjectSnapshot(
            "project",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "one"),)),),
        )
    )
    orchestrator.store = store
    adapter = _BudgetAdapter(
        _snapshot(
            evidence=BudgetEvidence.STALE,
            source_version="v1",
            observed_at=now - timedelta(hours=1),
            remaining=0,
            reset_at=(now - timedelta(minutes=1)).isoformat(),
        )
    )
    scheduler = AgentScheduler(
        orchestrator,
        FakeWorker(),
        {"project"},
        SchedulerConfig(enabled=True),
        budget_adapter=adapter,
    )
    assert scheduler.poll_once() == ()
    adapter.current = _snapshot(source_version="v1", observed_at=now, remaining=50)
    assert scheduler.poll_once() == ()
    assert store.budget_decision_for_project("project") is not None
    restarted = AgentScheduler(
        orchestrator,
        FakeWorker(),
        {"project"},
        SchedulerConfig(enabled=True),
        budget_adapter=adapter,
    )
    assert restarted.poll_once() == ()
    persisted = store.budget_decision_for_project("project")
    assert persisted is not None
    assert persisted.action is BudgetAction.PAUSE
    assert persisted.reason is BudgetReason.RESET_UNVERIFIED
    for marker in range(105):
        snapshot = _snapshot(
            source_version=f"blocked-{marker}",
            observed_at=now - timedelta(minutes=10),
            remaining=50,
        )
        store.record_budget_decision(
            "project",
            snapshot,
            BudgetDecision(
                BudgetAction.PAUSE,
                BudgetReason.RESET_UNVERIFIED,
                snapshot.source_version,
            ),
        )
    assert len(store.budget_evidence_for("project")) == 100
    baseline = store.budget_reset_baseline_for_project("project")
    assert baseline is not None
    assert baseline.source_version == "v1"
    restarted.budget_policy = BudgetPolicy(downgrade_model="sol", downgrade_remaining=5)
    adapter.current = _snapshot(
        source_version="v2",
        observed_at=datetime.now(UTC) - timedelta(milliseconds=1),
        remaining=3,
        models={"sol": True},
    )
    verified = restarted._budget_decision("project")
    assert verified is not None
    assert verified.action is BudgetAction.DOWNGRADE
    assert verified.fallback_model == "sol"
    store.close()


def test_scheduler_latches_unsafe_provider_recovery(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    orchestrator = FakeOrchestrator({"project": {"repositories": []}})
    store = OrchestratorStore(tmp_path / "scheduler-budget-unsafe.sqlite3")
    store.sync_project(
        ProjectSnapshot(
            "project",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "one"),)),),
        )
    )
    orchestrator.store = store
    adapter = _FlakyBudgetAdapter(
        _snapshot(source_version="v1", observed_at=now, remaining=50)
    )
    scheduler = AgentScheduler(
        orchestrator,
        FakeWorker(),
        {"project"},
        SchedulerConfig(enabled=True),
        budget_adapter=adapter,
    )
    first = scheduler._budget_decision("project")
    assert first is not None
    assert first.reason is BudgetReason.EVIDENCE_UNAVAILABLE
    second = scheduler._budget_decision("project")
    assert second is not None
    assert second.reason is BudgetReason.EVIDENCE_CONTRADICTORY
    adapter.current = _snapshot(
        source_version="v2",
        observed_at=datetime.now(UTC) - timedelta(milliseconds=1),
        remaining=50,
    )
    third = scheduler._budget_decision("project")
    assert third is not None
    assert third.action is BudgetAction.PAUSE
    assert third.reason is BudgetReason.RESET_UNVERIFIED
    store.close()
