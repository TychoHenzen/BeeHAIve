from dataclasses import replace

import pytest

from beehaiive.lifecycle_contract import (
    AUTHORITY_MATRIX,
    COMPATIBILITY_RULES,
    CONFLICT_RULES,
    LIFECYCLE_RULES,
    STATE_PRECEDENCE,
    FactOwner,
    LifecycleState,
    TransitionEvidence,
    TransitionReason,
    can_transition,
    replay_id_for,
)


def test_contract_defines_all_states_and_safety_rules() -> None:
    assert set(LIFECYCLE_RULES) == set(LifecycleState)
    assert len(LifecycleState) == 14
    assert STATE_PRECEDENCE[0] is LifecycleState.UNKNOWN
    assert STATE_PRECEDENCE[-1] is LifecycleState.COMPLETED
    assert len(STATE_PRECEDENCE) == len(set(STATE_PRECEDENCE))
    assert LIFECYCLE_RULES[LifecycleState.COMPLETED].terminal is True
    assert LIFECYCLE_RULES[LifecycleState.FAILED].terminal_for_attempt is True
    assert LIFECYCLE_RULES[LifecycleState.BLOCKED].terminal_for_attempt is True
    assert (
        LIFECYCLE_RULES[LifecycleState.HUMAN_ACTION_REQUIRED].terminal_for_attempt
        is True
    )
    for state in (
        LifecycleState.QUESTION,
        LifecycleState.BLOCKED,
        LifecycleState.HUMAN_ACTION_REQUIRED,
    ):
        assert LIFECYCLE_RULES[state].blocking is True
    assert "prior_state" in LIFECYCLE_RULES[LifecycleState.QUESTION].required_evidence
    assert "prior_state" in LIFECYCLE_RULES[LifecycleState.BLOCKED].required_evidence
    assert (
        LifecycleState.COMPLETED
        not in LIFECYCLE_RULES[LifecycleState.UNKNOWN].allowed_next
    )


def test_authority_and_compatibility_matrices_preserve_raw_sources() -> None:
    assert AUTHORITY_MATRIX["planning_status"].owner is FactOwner.PROJECT
    assert (
        AUTHORITY_MATRIX["pull_request_and_checks"].owner
        is FactOwner.PULL_REQUEST_PROVIDER
    )
    assert AUTHORITY_MATRIX["run_lease_result_failure"].owner is FactOwner.ORCHESTRATOR
    assert AUTHORITY_MATRIX["review_cycle_findings_approval"].owner is FactOwner.REVIEW
    assert AUTHORITY_MATRIX["handoff_gate_question"].owner is FactOwner.WORKFLOW
    assert AUTHORITY_MATRIX["retry_and_escalation"].owner is FactOwner.ROUTING
    assert all(rule.preserve_raw for rule in AUTHORITY_MATRIX.values())
    assert COMPATIBILITY_RULES == {
        "missing_canonical_fields": "map_to_unknown",
        "legacy_status": "preserve_raw",
        "new_api_fields": "additive",
        "unknown_progression": "require_new_evidence",
    }
    assert CONFLICT_RULES["missing_required_evidence"] == "unknown"
    assert CONFLICT_RULES["stale_evidence"] == "unknown"
    assert CONFLICT_RULES["contradictory_evidence"] == "unknown"
    assert CONFLICT_RULES["concurrent_evidence"] == "unknown_until_source_revision"
    assert (
        CONFLICT_RULES["review_head_changed"]
        == "return_to_implementation_for_fresh_review"
    )
    assert CONFLICT_RULES["stale_approval"] == "blocked_until_fresh_review"
    assert CONFLICT_RULES["approval_head_mismatch"] == "blocked_until_fresh_approval"
    assert (
        CONFLICT_RULES["external_project_done_with_active_run"]
        == "preserve_active_local_run"
    )
    with pytest.raises(TypeError):
        AUTHORITY_MATRIX["new"] = AUTHORITY_MATRIX["planning_status"]  # type: ignore[index]


def test_transition_table_covers_progress_retry_and_resume() -> None:
    assert can_transition(LifecycleState.PLANNING, LifecycleState.REFINEMENT)
    assert can_transition(LifecycleState.IMPLEMENTATION, LifecycleState.PULL_REQUEST)
    assert can_transition(LifecycleState.REVIEW, LifecycleState.MERGE)
    assert can_transition(LifecycleState.MERGE, LifecycleState.COMPLETED)
    assert can_transition(LifecycleState.FAILED, LifecycleState.IMPLEMENTATION)
    assert can_transition(LifecycleState.BLOCKED, LifecycleState.REVIEW)
    assert can_transition(LifecycleState.QUESTION, LifecycleState.IMPLEMENTATION)
    assert can_transition(
        LifecycleState.HUMAN_ACTION_REQUIRED, LifecycleState.IMPLEMENTATION
    )
    assert not can_transition(LifecycleState.COMPLETED, LifecycleState.IMPLEMENTATION)
    assert not can_transition("invalid", LifecycleState.READY)


def test_transition_evidence_is_immutable_bounded_and_replay_safe() -> None:
    replay_id = replay_id_for("owner/api#1", "advance", "run-1", "7")
    assert replay_id == replay_id_for("owner/api#1", "advance", "run-1", "7")
    assert len(replay_id) == 64
    assert replay_id != replay_id_for("owner/api#1", "retry", "run-1", "7")
    assert replay_id_for("a\x1fb", "c", "d", "e") != replay_id_for(
        "a", "b\x1fc", "d", "e"
    )

    evidence = TransitionEvidence(
        item_id="owner/api#1",
        event_type="lifecycle.transition",
        source_owner=FactOwner.ORCHESTRATOR,
        source_id="run-1",
        source_version="7",
        observed_at="2026-09-15T00:00:00+00:00",
        state_before=LifecycleState.IMPLEMENTATION,
        state_after=LifecycleState.FAILED,
        reason_code=TransitionReason.FAILURE,
    )
    assert evidence.schema_version == 1
    with pytest.raises(ValueError, match="source_id exceeds"):
        replace(evidence, source_id="x" * 129)
    with pytest.raises(ValueError, match="reason_code"):
        replace(evidence, reason_code="failure")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="replay_id does not match"):
        replace(evidence, replay_id="wrong")
    with pytest.raises(ValueError, match="ISO-8601"):
        replace(evidence, observed_at="yesterday")
    with pytest.raises(ValueError, match="transition is not allowed"):
        replace(evidence, state_after=LifecycleState.COMPLETED)
    with pytest.raises(ValueError, match="replay identity parts"):
        replay_id_for("", "advance", "run-1", "7")
