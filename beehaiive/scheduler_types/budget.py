from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol, cast

MAX_BUDGET_TEXT = 128
MAX_BUDGET_BUCKETS = 32
MAX_BUDGET_MODELS = 32
MAX_BUDGET_EVIDENCE_RECORDS = 100


class BudgetEvidence(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    CONTRADICTORY = "contradictory"


class BudgetAction(StrEnum):
    ALLOW = "allow"
    PAUSE = "pause"
    DOWNGRADE = "downgrade"
    RESUME = "resume"


class BudgetReason(StrEnum):
    FRESH_BUDGET = "fresh_budget"
    EVIDENCE_STALE = "budget_evidence_stale"
    EVIDENCE_UNAVAILABLE = "budget_evidence_unavailable"
    EVIDENCE_CONTRADICTORY = "budget_evidence_contradictory"
    BUCKETS_MISSING = "budget_buckets_missing"
    BUCKET_VALUES_INVALID = "budget_bucket_values_invalid"
    BUDGET_LOW = "budget_low"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_UNAVAILABLE = "budget_fallback_model_unavailable"
    RESET_VERIFIED = "budget_reset_verified"
    RESET_UNVERIFIED = "budget_reset_unverified"


@dataclass(frozen=True, slots=True)
class AllowanceBucket:
    bucket_id: str
    remaining: float | None = None
    limit: float | None = None
    used: float | None = None
    reset_at: str | None = None
    credits: float | None = None
    model_availability: Mapping[str, bool] = MappingProxyType({})

    def __post_init__(self) -> None:
        _validate_text(self.bucket_id, "bucket_id")
        _validate_numbers(
            (self.remaining, self.limit, self.used, self.credits),
            "bucket value",
        )
        if self.reset_at is not None:
            _validate_timestamp(self.reset_at, "reset_at")
        if len(self.model_availability) > MAX_BUDGET_MODELS:
            raise ValueError("model_availability has too many entries")
        for model, available in self.model_availability.items():
            _validate_text(model, "model")
            if type(available) is not bool:
                raise ValueError("model availability must be boolean")
        object.__setattr__(
            self, "model_availability", MappingProxyType(dict(self.model_availability))
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AllowanceBucket:
        models = value.get("model_availability")
        if (
            "model_availability" in value
            and models is not None
            and not isinstance(models, Mapping)
        ):
            raise ValueError("model_availability must be a mapping")
        return cls(
            bucket_id=_required_text(value.get("bucket_id"), "bucket_id"),
            remaining=_optional_number(value.get("remaining"), "remaining"),
            limit=_optional_number(value.get("limit"), "limit"),
            used=_optional_number(value.get("used"), "used"),
            reset_at=_optional_text(value.get("reset_at"), "reset_at"),
            credits=_optional_number(value.get("credits"), "credits"),
            model_availability=(
                dict(cast(Mapping[str, bool], models))
                if isinstance(models, Mapping)
                else {}
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket_id": self.bucket_id,
            "remaining": self.remaining,
            "limit": self.limit,
            "used": self.used,
            "reset_at": self.reset_at,
            "credits": self.credits,
            "model_availability": dict(sorted(self.model_availability.items())),
        }


@dataclass(frozen=True, slots=True)
class AccountUsageSnapshot:
    source_id: str
    source_version: str
    observed_at: str
    evidence: BudgetEvidence
    buckets: tuple[AllowanceBucket, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(self.source_id, "source_id")
        _validate_text(self.source_version, "source_version")
        _validate_timestamp(self.observed_at, "observed_at")
        if type(self.evidence) is not BudgetEvidence:
            raise ValueError("evidence must be a BudgetEvidence value")
        if len(self.buckets) > MAX_BUDGET_BUCKETS:
            raise ValueError("too many budget buckets")
        if any(type(bucket) is not AllowanceBucket for bucket in self.buckets):
            raise ValueError("buckets must contain AllowanceBucket values")
        bucket_ids = [bucket.bucket_id for bucket in self.buckets]
        if len(bucket_ids) != len(set(bucket_ids)):
            raise ValueError("budget bucket ids must be unique")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AccountUsageSnapshot:
        raw_buckets = value.get("buckets")
        if raw_buckets is not None and not isinstance(raw_buckets, list):
            raise ValueError("buckets must be a list")
        items = cast(list[object], raw_buckets) if isinstance(raw_buckets, list) else []
        if any(not isinstance(item, Mapping) for item in items):
            raise ValueError("buckets must contain mappings")
        buckets = tuple(
            AllowanceBucket.from_dict(cast(Mapping[str, object], item))
            for item in items
            if isinstance(item, Mapping)
        )
        return cls(
            source_id=_required_text(value.get("source_id"), "source_id"),
            source_version=_required_text(
                value.get("source_version"), "source_version"
            ),
            observed_at=_required_text(value.get("observed_at"), "observed_at"),
            evidence=BudgetEvidence(_required_text(value.get("evidence"), "evidence")),
            buckets=buckets,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "observed_at": self.observed_at,
            "evidence": self.evidence.value,
            "buckets": [bucket.as_dict() for bucket in self.buckets],
        }


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    freshness_seconds: float = 300.0
    downgrade_model: str | None = None
    downgrade_remaining: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.freshness_seconds) not in {int, float}
            or not isfinite(self.freshness_seconds)
            or self.freshness_seconds <= 0
        ):
            raise ValueError("freshness_seconds must be a finite positive number")
        if self.downgrade_model is not None:
            _validate_text(self.downgrade_model, "downgrade_model")
        if self.downgrade_remaining is not None and (
            isinstance(self.downgrade_remaining, bool)
            or not isfinite(self.downgrade_remaining)
            or self.downgrade_remaining < 0
        ):
            raise ValueError("downgrade_remaining must be finite and nonnegative")
        if self.downgrade_remaining is not None and self.downgrade_model is None:
            raise ValueError("downgrade_model is required with downgrade_remaining")


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    action: BudgetAction
    reason: BudgetReason
    source_version: str
    fallback_model: str | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not BudgetAction:
            raise ValueError("action must be a BudgetAction value")
        if type(self.reason) is not BudgetReason:
            raise ValueError("reason must be a BudgetReason value")
        _validate_text(self.source_version, "source_version")
        if self.fallback_model is not None:
            _validate_text(self.fallback_model, "fallback_model")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "reason": self.reason.value,
            "source_version": self.source_version,
            "fallback_model": self.fallback_model,
        }


class BudgetAdapter(Protocol):
    def snapshot(self, project_id: str) -> AccountUsageSnapshot: ...


def evaluate_budget(
    snapshot: AccountUsageSnapshot,
    policy: BudgetPolicy,
    previous: AccountUsageSnapshot | None = None,
    *,
    now: datetime | None = None,
) -> BudgetDecision:
    current_time = now or datetime.now(UTC)
    if snapshot.evidence is not BudgetEvidence.FRESH:
        return BudgetDecision(
            BudgetAction.PAUSE,
            {
                BudgetEvidence.STALE: BudgetReason.EVIDENCE_STALE,
                BudgetEvidence.UNAVAILABLE: BudgetReason.EVIDENCE_UNAVAILABLE,
                BudgetEvidence.CONTRADICTORY: BudgetReason.EVIDENCE_CONTRADICTORY,
            }[snapshot.evidence],
            snapshot.source_version,
        )
    observed = _timestamp(snapshot.observed_at)
    if observed is None or observed > current_time:
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.EVIDENCE_CONTRADICTORY,
            snapshot.source_version,
        )
    if (current_time - observed).total_seconds() > policy.freshness_seconds:
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.EVIDENCE_STALE,
            snapshot.source_version,
        )
    if previous is not None and snapshot.source_id != previous.source_id:
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.EVIDENCE_CONTRADICTORY,
            snapshot.source_version,
        )
    if previous is not None and snapshot.source_version != previous.source_version:
        previous_observed = _timestamp(previous.observed_at)
        if previous_observed is not None and observed <= previous_observed:
            return BudgetDecision(
                BudgetAction.PAUSE,
                BudgetReason.EVIDENCE_CONTRADICTORY,
                snapshot.source_version,
            )
    if not snapshot.buckets:
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.BUCKETS_MISSING,
            snapshot.source_version,
        )
    if _invalid_bucket_values(snapshot.buckets):
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.BUCKET_VALUES_INVALID,
            snapshot.source_version,
        )
    reset_verified = False
    if previous is not None:
        previous_observed = _timestamp(previous.observed_at)
        if (
            previous_observed is not None
            and snapshot.source_version == previous.source_version
            and observed < previous_observed
        ):
            return BudgetDecision(
                BudgetAction.PAUSE,
                BudgetReason.EVIDENCE_CONTRADICTORY,
                snapshot.source_version,
            )
        if (
            previous.evidence is not BudgetEvidence.FRESH or _budget_exhausted(previous)
        ) and _has_available_budget(snapshot):
            if _reset_verified(previous, snapshot, current_time):
                reset_verified = True
            else:
                return BudgetDecision(
                    BudgetAction.PAUSE,
                    BudgetReason.RESET_UNVERIFIED,
                    snapshot.source_version,
                )
    remaining = [bucket.remaining for bucket in snapshot.buckets]
    if any(value is None for value in remaining):
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.BUCKET_VALUES_INVALID,
            snapshot.source_version,
        )
    exhausted = any(value <= 0 for value in remaining if value is not None)
    downgrade_model = policy.downgrade_model
    downgrade_remaining = policy.downgrade_remaining
    fallback_configured = (
        downgrade_model is not None and downgrade_remaining is not None
    )
    if fallback_configured:
        assert downgrade_model is not None and downgrade_remaining is not None
        downgrade_candidate = any(
            value > 0 and value <= downgrade_remaining
            for value in remaining
            if value is not None
        )
        if (
            any(
                bucket.model_availability.get(downgrade_model) is True
                for bucket in snapshot.buckets
            )
            and downgrade_candidate
        ):
            return BudgetDecision(
                BudgetAction.DOWNGRADE,
                BudgetReason.BUDGET_LOW,
                snapshot.source_version,
                downgrade_model,
            )
        if exhausted and not any(
            bucket.model_availability.get(downgrade_model) is True
            for bucket in snapshot.buckets
        ):
            return BudgetDecision(
                BudgetAction.PAUSE,
                BudgetReason.MODEL_UNAVAILABLE,
                snapshot.source_version,
            )
    if exhausted:
        return BudgetDecision(
            BudgetAction.PAUSE,
            BudgetReason.BUDGET_EXHAUSTED,
            snapshot.source_version,
        )
    if reset_verified:
        return BudgetDecision(
            BudgetAction.RESUME,
            BudgetReason.RESET_VERIFIED,
            snapshot.source_version,
        )
    return BudgetDecision(
        BudgetAction.ALLOW,
        BudgetReason.FRESH_BUDGET,
        snapshot.source_version,
    )


def budget_replay_id(
    project_id: str, snapshot: AccountUsageSnapshot, decision: BudgetDecision
) -> str:
    values = [
        project_id,
        snapshot.source_id,
        snapshot.source_version,
        decision.action.value,
        decision.reason.value,
        decision.fallback_model,
    ]
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _reset_verified(
    previous: AccountUsageSnapshot,
    current: AccountUsageSnapshot,
    now: datetime,
) -> bool:
    previous_observed = _timestamp(previous.observed_at)
    current_observed = _timestamp(current.observed_at)
    if (
        previous_observed is None
        or current_observed is None
        or current.source_id != previous.source_id
        or current.source_version == previous.source_version
        or current_observed <= previous_observed
    ):
        return False
    previous_resets = {
        bucket.bucket_id: _timestamp(bucket.reset_at)
        for bucket in previous.buckets
        if bucket.reset_at
    }
    current_by_id = {bucket.bucket_id: bucket for bucket in current.buckets}
    for bucket_id, reset_at in previous_resets.items():
        if reset_at is None or reset_at > now:
            continue
        bucket = current_by_id.get(bucket_id)
        if bucket is not None and bucket.remaining is not None and bucket.remaining > 0:
            return True
    return False


def _has_available_budget(snapshot: AccountUsageSnapshot) -> bool:
    return bool(snapshot.buckets) and all(
        bucket.remaining is not None and bucket.remaining > 0
        for bucket in snapshot.buckets
    )


def _budget_exhausted(snapshot: AccountUsageSnapshot) -> bool:
    return bool(snapshot.buckets) and any(
        bucket.remaining is not None and bucket.remaining <= 0
        for bucket in snapshot.buckets
    )


def _invalid_bucket_values(buckets: tuple[AllowanceBucket, ...]) -> bool:
    for bucket in buckets:
        values = (bucket.remaining, bucket.limit, bucket.used, bucket.credits)
        if any(value is not None and value < 0 for value in values):
            return True
        if (
            bucket.remaining is not None
            and bucket.limit is not None
            and bucket.remaining > bucket.limit
        ):
            return True
        if (
            bucket.used is not None
            and bucket.limit is not None
            and bucket.used > bucket.limit
        ):
            return True
        if (
            bucket.used is not None
            and bucket.remaining is not None
            and bucket.limit is not None
            and bucket.used + bucket.remaining > bucket.limit
        ):
            return True
    return False


def _validate_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    if len(value) > MAX_BUDGET_TEXT:
        raise ValueError(f"{label} exceeds {MAX_BUDGET_TEXT} characters")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    if len(value) > MAX_BUDGET_TEXT:
        raise ValueError(f"{label} exceeds {MAX_BUDGET_TEXT} characters")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    if len(value) > MAX_BUDGET_TEXT:
        raise ValueError(f"{label} exceeds {MAX_BUDGET_TEXT} characters")
    return value


def _validate_numbers(values: tuple[float | None, ...], label: str) -> None:
    try:
        invalid = any(
            value is not None and (type(value) is bool or not isfinite(value))
            for value in values
        )
    except TypeError as error:
        raise ValueError(f"{label} must be finite numbers") from error
    if invalid:
        raise ValueError(f"{label} must be finite numbers")


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    if not isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _validate_timestamp(value: object, label: str) -> None:
    if _timestamp(value) is None:
        raise ValueError(f"{label} must be an ISO-8601 timestamp with timezone")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    if len(value) > MAX_BUDGET_TEXT:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = [
    "AccountUsageSnapshot",
    "AllowanceBucket",
    "BudgetAction",
    "BudgetAdapter",
    "BudgetDecision",
    "BudgetEvidence",
    "BudgetPolicy",
    "BudgetReason",
    "MAX_BUDGET_EVIDENCE_RECORDS",
    "budget_replay_id",
    "evaluate_budget",
]
