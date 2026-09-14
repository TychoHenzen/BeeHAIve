from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from ..contracts import TaskOutcome
from .attempt_outcome import AttemptOutcome
from .model_execution import ModelExecution
from .model_spec import ModelSpec
from .model_tier import ModelTier
from .routing_attempt import RoutingAttempt
from .routing_error import RoutingError
from .routing_state import RoutingState
from .routing_status import RoutingStatus


def default_writer() -> ModelSpec:
    return ModelSpec(ModelTier.LUNA, "luna", 0.20, 1.20)


def default_triage() -> tuple[ModelSpec, ...]:
    return (
        ModelSpec(ModelTier.TERRA, "terra", 0.50, 2.00),
        ModelSpec(ModelTier.SOL, "sol", 1.00, 4.00),
        ModelSpec(ModelTier.ASTRA, "astra", 2.00, 8.00),
    )


def now() -> str:
    return datetime.now(UTC).isoformat()


def compact_context(value: str, limit: int = 280) -> str:
    compacted = " ".join(value.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def state_values(state: RoutingState) -> tuple[object, ...]:
    return (
        state.problem_id,
        state.status.value,
        state.current_tier.value,
        state.triage_index,
        state.consecutive_failures,
        state.bounce_count,
        state.round,
        state.total_tokens,
        state.total_cost,
        state.recursive_spawn_depth,
        state.last_failure_context,
        state.required_action,
        state.next_reason,
        state.created_at,
        state.updated_at,
    )


def state_from_row(row: sqlite3.Row) -> RoutingState:
    return RoutingState(
        problem_id=str(row["problem_id"]),
        status=RoutingStatus(str(row["status"])),
        current_tier=ModelTier(str(row["current_tier"])),
        triage_index=int(row["triage_index"]),
        consecutive_failures=int(row["consecutive_failures"]),
        bounce_count=int(row["bounce_count"]),
        round=int(row["round"]),
        total_tokens=int(row["total_tokens"]),
        total_cost=float(row["total_cost"]),
        recursive_spawn_depth=int(row["recursive_spawn_depth"]),
        last_failure_context=row["last_failure_context"],
        required_action=row["required_action"],
        next_reason=str(row["next_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def attempt_from_row(row: sqlite3.Row) -> RoutingAttempt:
    return RoutingAttempt(
        attempt_id=int(row["attempt_id"]),
        problem_id=str(row["problem_id"]),
        round=int(row["round"]),
        model=str(row["model"]),
        tier=ModelTier(str(row["tier"])),
        reason=str(row["reason"]),
        outcome=AttemptOutcome(str(row["outcome"])),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        total_tokens=int(row["total_tokens"]),
        estimated_cost=float(row["estimated_cost"]),
        bounce_count=int(row["bounce_count"]),
        recursive_spawn_depth=int(row["recursive_spawn_depth"]),
        failure_context=row["failure_context"],
        created_at=str(row["created_at"]),
    )


def failed_model_execution(error: Exception) -> ModelExecution:
    def usage_value(name: str) -> int:
        value = getattr(error, name, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    failure_context = getattr(error, "failure_context", "")
    if not isinstance(failure_context, str) or not failure_context.strip():
        failure_context = f"Model execution failed: {error or type(error).__name__}"
    return ModelExecution(
        AttemptOutcome.FAILURE,
        input_tokens=usage_value("input_tokens"),
        output_tokens=usage_value("output_tokens"),
        failure_context=failure_context,
        recursive_spawn_depth=usage_value("recursive_spawn_depth"),
    )


def task_transition(
    execution: ModelExecution,
) -> tuple[AttemptOutcome | str, str, str | None]:
    result = execution.task_result
    if result is None:
        return execution.outcome, "", None
    if result.outcome is TaskOutcome.PASS:
        return AttemptOutcome.SUCCESS, "", None
    if result.outcome is TaskOutcome.FAIL:
        return (
            AttemptOutcome.FAILURE,
            result.validation_reason or "Task reported failure",
            None,
        )
    if result.outcome is TaskOutcome.BLOCKED:
        reason = result.required_action or "Task is blocked"
        return AttemptOutcome.FAILURE, reason, reason
    reason = result.question or "Task requires an operator answer"
    return AttemptOutcome.FAILURE, reason, reason


def normalize_problem_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RoutingError("A routing problem id is required")
    if len(normalized) > 200:
        raise RoutingError("A routing problem id must be 200 characters or fewer")
    return normalized
