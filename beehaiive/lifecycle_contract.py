from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Final, cast


class LifecycleState(StrEnum):
    PLANNING = "planning"
    REFINEMENT = "refinement"
    READY = "ready"
    IMPLEMENTATION = "implementation"
    PULL_REQUEST = "pull_request"
    CHECKS = "checks"
    REVIEW = "review"
    MERGE = "merge"
    COMPLETED = "completed"
    FAILED = "failed"
    QUESTION = "question"
    BLOCKED = "blocked"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    UNKNOWN = "unknown"


class FactOwner(StrEnum):
    PROJECT = "github_project"
    REFINEMENT = "refinement_contract"
    ORCHESTRATOR = "orchestrator_store"
    PULL_REQUEST_PROVIDER = "pull_request_provider"
    REVIEW = "review_store"
    WORKFLOW = "workflow_store"
    ROUTING = "routing_store"
    DERIVED = "lifecycle_projection"


class TransitionReason(StrEnum):
    ADVANCE = "advance"
    COMPLETION_CONFIRMED = "completion_confirmed"
    CONFLICT = "conflict"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_STALE = "evidence_stale"
    FAILURE = "failure"
    RETRY = "retry"
    RESUME_HUMAN_ACTION = "resume_human_action"
    RESUME_QUESTION = "resume_question"
    UNKNOWN_RESOLVED = "unknown_resolved"


@dataclass(frozen=True, slots=True)
class LifecycleRule:
    owner: FactOwner
    required_evidence: tuple[str, ...]
    meaning: str
    allowed_next: frozenset[LifecycleState]
    terminal: bool = False
    terminal_for_attempt: bool = False
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class FactAuthority:
    owner: FactOwner
    evidence: tuple[str, ...]
    preserve_raw: bool = True


MAX_TRANSITION_EVIDENCE_RECORDS: Final = 100
MAX_TRANSITION_EVENT_LENGTH: Final = 64
MAX_TRANSITION_REASON_LENGTH: Final = 512
MAX_TRANSITION_SOURCE_LENGTH: Final = 128
MAX_TRANSITION_TIMESTAMP_LENGTH: Final = 64
MAX_TRANSITION_REPLAY_LENGTH: Final = 128
LIFECYCLE_CONTRACT_VERSION: Final = 1


_RESUMABLE_STATES = frozenset(
    {
        LifecycleState.PLANNING,
        LifecycleState.REFINEMENT,
        LifecycleState.READY,
        LifecycleState.IMPLEMENTATION,
        LifecycleState.PULL_REQUEST,
        LifecycleState.CHECKS,
        LifecycleState.REVIEW,
        LifecycleState.MERGE,
    }
)
_UNKNOWN_RESOLUTION_STATES = frozenset(
    state
    for state in LifecycleState
    if state not in {LifecycleState.COMPLETED, LifecycleState.UNKNOWN}
)

LIFECYCLE_RULES: Mapping[LifecycleState, LifecycleRule] = MappingProxyType(
    {
        LifecycleState.PLANNING: LifecycleRule(
            FactOwner.PROJECT,
            ("project_status",),
            "Planned or backlogged work.",
            frozenset(
                {
                    LifecycleState.REFINEMENT,
                    LifecycleState.READY,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.REFINEMENT: LifecycleRule(
            FactOwner.REFINEMENT,
            ("requirements",),
            "Requirements are being clarified.",
            frozenset(
                {
                    LifecycleState.READY,
                    LifecycleState.QUESTION,
                    LifecycleState.BLOCKED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.READY: LifecycleRule(
            FactOwner.PROJECT,
            ("project_status", "refinement"),
            "The PBI is ready for implementation.",
            frozenset(
                {
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.BLOCKED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.IMPLEMENTATION: LifecycleRule(
            FactOwner.ORCHESTRATOR,
            ("active_run", "lease"),
            "Local implementation work is active.",
            frozenset(
                {
                    LifecycleState.PULL_REQUEST,
                    LifecycleState.FAILED,
                    LifecycleState.QUESTION,
                    LifecycleState.BLOCKED,
                    LifecycleState.HUMAN_ACTION_REQUIRED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.PULL_REQUEST: LifecycleRule(
            FactOwner.PULL_REQUEST_PROVIDER,
            ("branch_or_pull_request",),
            "A feature branch or pull request exists.",
            frozenset(
                {
                    LifecycleState.CHECKS,
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.FAILED,
                    LifecycleState.BLOCKED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.CHECKS: LifecycleRule(
            FactOwner.PULL_REQUEST_PROVIDER,
            ("required_checks",),
            "Required checks are running or have a known result.",
            frozenset(
                {
                    LifecycleState.REVIEW,
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.FAILED,
                    LifecycleState.BLOCKED,
                    LifecycleState.HUMAN_ACTION_REQUIRED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.REVIEW: LifecycleRule(
            FactOwner.REVIEW,
            ("review_cycle",),
            "Review is active or has findings to resolve.",
            frozenset(
                {
                    LifecycleState.MERGE,
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.FAILED,
                    LifecycleState.BLOCKED,
                    LifecycleState.QUESTION,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.MERGE: LifecycleRule(
            FactOwner.WORKFLOW,
            ("merge_gate", "provider_merge"),
            "Merge is gated or being reconciled.",
            frozenset(
                {
                    LifecycleState.COMPLETED,
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.FAILED,
                    LifecycleState.BLOCKED,
                    LifecycleState.UNKNOWN,
                }
            ),
        ),
        LifecycleState.COMPLETED: LifecycleRule(
            FactOwner.DERIVED,
            ("merged", "completion"),
            "All completion evidence is present. This state is terminal.",
            frozenset(),
            terminal=True,
        ),
        LifecycleState.FAILED: LifecycleRule(
            FactOwner.ORCHESTRATOR,
            ("failure",),
            "The current attempt failed and needs an explicit retry or action.",
            frozenset(
                {
                    LifecycleState.IMPLEMENTATION,
                    LifecycleState.QUESTION,
                    LifecycleState.BLOCKED,
                    LifecycleState.UNKNOWN,
                }
            ),
            terminal_for_attempt=True,
        ),
        LifecycleState.QUESTION: LifecycleRule(
            FactOwner.WORKFLOW,
            ("question", "prior_state"),
            "Work is waiting for an answer and is nonclaimable.",
            frozenset(
                _RESUMABLE_STATES | {LifecycleState.BLOCKED, LifecycleState.UNKNOWN}
            ),
            terminal_for_attempt=True,
            blocking=True,
        ),
        LifecycleState.BLOCKED: LifecycleRule(
            FactOwner.WORKFLOW,
            ("blocker", "prior_state"),
            "A gate or dependency prevents progress.",
            frozenset(_RESUMABLE_STATES | {LifecycleState.UNKNOWN}),
            terminal_for_attempt=True,
            blocking=True,
        ),
        LifecycleState.HUMAN_ACTION_REQUIRED: LifecycleRule(
            FactOwner.WORKFLOW,
            ("required_action", "prior_state"),
            "An authorized human action is required before progress.",
            frozenset(
                _RESUMABLE_STATES | {LifecycleState.BLOCKED, LifecycleState.UNKNOWN}
            ),
            terminal_for_attempt=True,
            blocking=True,
        ),
        LifecycleState.UNKNOWN: LifecycleRule(
            FactOwner.DERIVED,
            ("new_evidence",),
            "Required evidence is missing, stale, or contradictory. "
            "This state fails closed.",
            _UNKNOWN_RESOLUTION_STATES,
        ),
    }
)

STATE_PRECEDENCE: tuple[LifecycleState, ...] = (
    LifecycleState.UNKNOWN,
    LifecycleState.HUMAN_ACTION_REQUIRED,
    LifecycleState.BLOCKED,
    LifecycleState.QUESTION,
    LifecycleState.FAILED,
    LifecycleState.IMPLEMENTATION,
    LifecycleState.PULL_REQUEST,
    LifecycleState.CHECKS,
    LifecycleState.REVIEW,
    LifecycleState.MERGE,
    LifecycleState.PLANNING,
    LifecycleState.REFINEMENT,
    LifecycleState.READY,
    LifecycleState.COMPLETED,
)

AUTHORITY_MATRIX: Mapping[str, FactAuthority] = MappingProxyType(
    {
        "planning_status": FactAuthority(FactOwner.PROJECT, ("status",)),
        "pull_request_and_checks": FactAuthority(
            FactOwner.PULL_REQUEST_PROVIDER, ("branch", "pull_request", "checks")
        ),
        "run_lease_result_failure": FactAuthority(
            FactOwner.ORCHESTRATOR, ("run", "lease", "result", "failure")
        ),
        "review_cycle_findings_approval": FactAuthority(
            FactOwner.REVIEW, ("cycle", "findings", "approval")
        ),
        "handoff_gate_question": FactAuthority(
            FactOwner.WORKFLOW, ("handoff", "gate", "question")
        ),
        "retry_and_escalation": FactAuthority(
            FactOwner.ROUTING, ("retry", "escalation")
        ),
    }
)

CONFLICT_RULES: Mapping[str, str] = MappingProxyType(
    {
        "missing_required_evidence": "unknown",
        "stale_evidence": "unknown",
        "contradictory_evidence": "unknown",
        "concurrent_evidence": "unknown_until_source_revision",
        "review_head_changed": "return_to_implementation_for_fresh_review",
        "stale_approval": "blocked_until_fresh_review",
        "approval_head_mismatch": "blocked_until_fresh_approval",
        "external_project_done_with_active_run": "preserve_active_local_run",
    }
)

COMPATIBILITY_RULES: Mapping[str, str] = MappingProxyType(
    {
        "missing_canonical_fields": "map_to_unknown",
        "legacy_status": "preserve_raw",
        "new_api_fields": "additive",
        "unknown_progression": "require_new_evidence",
    }
)


def _validate_text(value: object, limit: int, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    if len(value) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")


@dataclass(frozen=True, slots=True)
class TransitionEvidence:
    item_id: str
    event_type: str
    source_owner: FactOwner
    source_id: str
    source_version: str
    observed_at: str
    state_before: LifecycleState
    state_after: LifecycleState
    reason_code: TransitionReason
    schema_version: int = LIFECYCLE_CONTRACT_VERSION
    replay_id: str = ""

    def __post_init__(self) -> None:
        for value, limit, label in (
            (self.item_id, MAX_TRANSITION_SOURCE_LENGTH, "item_id"),
            (self.event_type, MAX_TRANSITION_EVENT_LENGTH, "event_type"),
            (self.source_id, MAX_TRANSITION_SOURCE_LENGTH, "source_id"),
            (self.source_version, MAX_TRANSITION_SOURCE_LENGTH, "source_version"),
            (self.observed_at, MAX_TRANSITION_TIMESTAMP_LENGTH, "observed_at"),
        ):
            _validate_text(cast(object, value), limit, label)
        if not isinstance(cast(object, self.source_owner), FactOwner):
            raise ValueError("source_owner must be a FactOwner")
        if not isinstance(
            cast(object, self.state_before), LifecycleState
        ) or not isinstance(cast(object, self.state_after), LifecycleState):
            raise ValueError("transition states must be LifecycleState values")
        if not isinstance(cast(object, self.reason_code), TransitionReason):
            raise ValueError("reason_code must be a TransitionReason")
        _validate_text(
            cast(object, self.reason_code.value),
            MAX_TRANSITION_REASON_LENGTH,
            "reason_code",
        )
        try:
            observed = datetime.fromisoformat(self.observed_at)
        except ValueError as error:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from error
        if observed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if self.state_before != self.state_after and not can_transition(
            self.state_before, self.state_after
        ):
            raise ValueError("transition is not allowed by the lifecycle contract")
        expected_replay_id = replay_id_for(
            self.item_id, self.event_type, self.source_id, self.source_version
        )
        if self.replay_id and self.replay_id != expected_replay_id:
            raise ValueError("replay_id does not match transition identity")
        object.__setattr__(self, "replay_id", expected_replay_id)


def replay_id_for(
    item_id: str, transition_type: str, source_id: str, source_version: str
) -> str:
    values: tuple[object, ...] = (item_id, transition_type, source_id, source_version)
    parts: list[str] = []
    for value in values:
        if not isinstance(cast(object, value), str) or not value.strip():
            raise ValueError("replay identity parts are required")
        parts.append(value.strip())
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def can_transition(before: LifecycleState | str, after: LifecycleState | str) -> bool:
    try:
        current = LifecycleState(before)
        target = LifecycleState(after)
    except ValueError:
        return False
    return target in LIFECYCLE_RULES[current].allowed_next


__all__ = [
    "AUTHORITY_MATRIX",
    "COMPATIBILITY_RULES",
    "CONFLICT_RULES",
    "FactAuthority",
    "FactOwner",
    "LIFECYCLE_CONTRACT_VERSION",
    "LIFECYCLE_RULES",
    "LifecycleRule",
    "LifecycleState",
    "MAX_TRANSITION_EVIDENCE_RECORDS",
    "MAX_TRANSITION_EVENT_LENGTH",
    "MAX_TRANSITION_REASON_LENGTH",
    "MAX_TRANSITION_REPLAY_LENGTH",
    "MAX_TRANSITION_SOURCE_LENGTH",
    "MAX_TRANSITION_TIMESTAMP_LENGTH",
    "STATE_PRECEDENCE",
    "TransitionEvidence",
    "TransitionReason",
    "can_transition",
    "replay_id_for",
]
