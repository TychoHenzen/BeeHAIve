from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

from .lifecycle_contract import (
    LifecycleState,
    TransitionReason,
)
from .models import PbiSnapshot, RunState, RunStatus, Stage

_OPTIONAL_SOURCES = ("review", "workflow", "routing")
_TERMINAL_PROJECT_STATUSES = frozenset({"closed", "completed", "done", "merged"})
_WORKFLOW_QUESTION_STATUSES = frozenset(
    {"question", "awaiting_operator", "awaiting_approval", "awaiting_clarification"}
)


@dataclass(frozen=True, slots=True)
class CanonicalLifecycleProjection:
    state: LifecycleState
    facts: Mapping[str, object]
    source_version: str
    reason_code: TransitionReason


def derive_lifecycle_projection(
    pbi: PbiSnapshot,
    run: RunState | None = None,
    optional_sources: Mapping[str, Mapping[str, object]] | None = None,
) -> CanonicalLifecycleProjection:
    metadata = dict(pbi.metadata)
    pull_requests = _mappings(metadata.get("pull_requests"))
    checks = _mapping(metadata.get("checks"))
    optional = {
        name: _optional_source((optional_sources or {}).get(name))
        for name in _OPTIONAL_SOURCES
    }
    facts: dict[str, object] = {
        "project": {
            "planning_status": pbi.planning_status,
            "stage": pbi.stage.value if pbi.stage is not None else None,
        },
        "provider": {
            "metadata_present": bool(metadata),
            "metadata_version": _source_version(metadata),
            "pull_request_count": len(pull_requests),
            "checks_verdict": checks.get("verdict"),
        },
        "run": _run_facts(run),
        "optional_sources": optional,
    }
    state = _state_for(pbi, run, pull_requests, checks, optional)
    stale = _stale_evidence(checks, pull_requests, optional)
    source_version = _source_version(facts)
    reason_code = (
        TransitionReason.COMPLETION_CONFIRMED
        if state is LifecycleState.COMPLETED
        else TransitionReason.FAILURE
        if state is LifecycleState.FAILED
        else TransitionReason.CONFLICT
        if state is LifecycleState.BLOCKED
        else TransitionReason.EVIDENCE_MISSING
        if state is LifecycleState.UNKNOWN
        else TransitionReason.ADVANCE
    )
    if state is LifecycleState.UNKNOWN and stale:
        reason_code = TransitionReason.EVIDENCE_STALE
    return CanonicalLifecycleProjection(
        state=state,
        facts=MappingProxyType(facts),
        source_version=source_version,
        reason_code=reason_code,
    )


def _state_for(
    pbi: PbiSnapshot,
    run: RunState | None,
    pull_requests: list[Mapping[str, object]],
    checks: Mapping[str, object],
    optional_sources: Mapping[str, Mapping[str, object]],
) -> LifecycleState:
    if _stale_evidence(checks, pull_requests, optional_sources):
        return LifecycleState.UNKNOWN
    review = optional_sources.get("review", {})
    workflow = optional_sources.get("workflow", {})
    routing = optional_sources.get("routing", {})
    if _normalised_status(workflow.get("status")) in _WORKFLOW_QUESTION_STATUSES:
        return LifecycleState.QUESTION
    if _normalised_status(routing.get("status")) in {
        "human_handoff",
        "human_action_required",
    }:
        return LifecycleState.HUMAN_ACTION_REQUIRED
    if (
        _normalised_status(workflow.get("status")) in {"blocked", "failed"}
        or _normalised_status(routing.get("status")) == "blocked"
    ):
        return LifecycleState.BLOCKED
    if (
        _truthy_flag(pbi.metadata, "stale_approval")
        or _truthy_flag(review, "stale_approval")
        or _truthy_flag(pbi.metadata, "approval_head_mismatch")
        or _truthy_flag(review, "approval_head_mismatch")
    ):
        return LifecycleState.BLOCKED
    if _truthy_flag(pbi.metadata, "review_head_changed") or _truthy_flag(
        review, "review_head_changed"
    ):
        return LifecycleState.IMPLEMENTATION
    if checks.get("verdict") == "blocking":
        return LifecycleState.BLOCKED
    if _provider_facts_conflict(pull_requests):
        return LifecycleState.UNKNOWN
    if run is not None:
        if run.status is RunStatus.AWAITING_OPERATOR:
            return LifecycleState.QUESTION
        if run.status is RunStatus.FAILED:
            return LifecycleState.FAILED
        if run.status is RunStatus.ACTIVE:
            if not _lease_is_live(run):
                return LifecycleState.UNKNOWN
            return _state_for_stage(run.stage)

    merged = any(pull_request.get("merged") is True for pull_request in pull_requests)
    planning_status = (pbi.planning_status or "").strip().lower()
    if merged:
        return (
            LifecycleState.COMPLETED
            if planning_status in _TERMINAL_PROJECT_STATUSES
            else LifecycleState.MERGE
        )
    if planning_status in _TERMINAL_PROJECT_STATUSES:
        return LifecycleState.UNKNOWN
    if pull_requests:
        if checks.get("verdict") == "pending":
            return LifecycleState.CHECKS
        if checks.get("verdict") != "passing":
            return LifecycleState.UNKNOWN
        if review.get("status") in {"active", "pending", "changes_requested"}:
            return LifecycleState.REVIEW
        if review.get("status") == "approved":
            return LifecycleState.MERGE
        return LifecycleState.UNKNOWN
    if pbi.stage is None:
        return {
            "backlog": LifecycleState.PLANNING,
            "todo": LifecycleState.READY,
        }.get(planning_status, LifecycleState.UNKNOWN)
    if pbi.stage in {Stage.BACKLOG, Stage.REFINE}:
        return _state_for_stage(pbi.stage)
    return LifecycleState.UNKNOWN


def _state_for_stage(stage: Stage) -> LifecycleState:
    return {
        Stage.BACKLOG: LifecycleState.PLANNING,
        Stage.REFINE: LifecycleState.REFINEMENT,
        Stage.IMPLEMENT: LifecycleState.IMPLEMENTATION,
        Stage.PULL_REQUEST: LifecycleState.PULL_REQUEST,
    }[stage]


def _run_facts(run: RunState | None) -> dict[str, object]:
    if run is None:
        return {"status": "unknown"}
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "stage": run.stage.value,
        "attempt": run.attempt,
        "lease_present": bool(run.lease_token and run.lease_token.strip()),
        "lease_expires_at": run.lease_expires_at,
        "lease_active": _lease_is_live(run),
        "branch": run.branch,
        "pull_request_url": run.pull_request_url,
        "last_error": run.last_error,
        "last_result": run.last_result,
    }


def _lease_is_live(run: RunState) -> bool:
    if not isinstance(run.lease_token, str) or not run.lease_token.strip():
        return False
    if not isinstance(run.lease_expires_at, str):
        return False
    try:
        expires_at = datetime.fromisoformat(run.lease_expires_at)
    except ValueError:
        return False
    return expires_at.tzinfo is not None and expires_at > datetime.now(UTC)


def _truthy_flag(values: Mapping[str, object], key: str) -> bool:
    return values.get(key) is True


def _normalised_status(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _provider_facts_conflict(pull_requests: list[Mapping[str, object]]) -> bool:
    for pull_request in pull_requests:
        merged = pull_request.get("merged")
        state = _normalised_status(pull_request.get("state"))
        if "merged" in pull_request and not isinstance(merged, bool):
            return True
        if state and state not in {"open", "closed", "merged"}:
            return True
        if merged is True and state and state not in {"closed", "merged"}:
            return True
        if merged is False and state == "merged":
            return True
    return False


def _stale_evidence(
    checks: Mapping[str, object],
    pull_requests: list[Mapping[str, object]],
    optional_sources: Mapping[str, Mapping[str, object]],
) -> bool:
    if _truthy_flag(checks, "stale") or checks.get("status") == "stale":
        return True
    if any(_truthy_flag(pull_request, "stale") for pull_request in pull_requests):
        return True
    return any(source.get("status") == "stale" for source in optional_sources.values())


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [
        cast(Mapping[str, object], item) for item in items if isinstance(item, Mapping)
    ]


def _optional_source(value: Mapping[str, object] | None) -> dict[str, object]:
    source = _mapping(value)
    source_id = source.get("source_id")
    source_version = source.get("source_version", source.get("version"))
    if not (
        isinstance(source_id, str)
        and source_id.strip()
        and isinstance(source_version, str)
        and source_version.strip()
    ):
        return {"status": "unknown"}
    result: dict[str, object] = {}
    for key, raw in (
        ("status", source.get("status")),
        ("source_id", source_id),
        ("source_version", source_version),
    ):
        if isinstance(raw, str) and raw.strip():
            result[key] = raw[:128]
    for key in ("review_head_changed", "stale_approval", "approval_head_mismatch"):
        if source.get(key) is True:
            result[key] = True
    return result or {"status": "unknown"}


def _source_version(facts: Mapping[str, object]) -> str:
    encoded = json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["CanonicalLifecycleProjection", "derive_lifecycle_projection"]
