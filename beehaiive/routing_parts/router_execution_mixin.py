from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from ..contracts import TaskResult
from .attempt_outcome import AttemptOutcome
from .helpers import (
    compact_context,
    failed_model_execution,
    normalize_problem_id,
    now,
    task_transition,
)
from .model_tier import ModelTier
from .routing_attempt import RoutingAttempt
from .routing_error import RoutingError
from .routing_result import RoutingResult
from .routing_status import RoutingStatus

if TYPE_CHECKING:
    from .model_execution import ModelExecution
    from .model_executor import ModelExecutor
from typing import Any


class RouterExecutionMixin:
    def execute(
        self: Any,
        problem_id: str,
        executor: ModelExecutor,
        before_record: Callable[[], None] | None = None,
        persist_task_result: Callable[[ModelExecution], TaskResult] | None = None,
        model_override: str | None = None,
    ) -> RoutingResult:
        """Invoke the selected model and persist its measured routing result."""

        normalized_id = normalize_problem_id(problem_id)
        with self.coordinate(normalized_id):
            current = self.snapshot(normalized_id)
            if current.state.status is not RoutingStatus.ACTIVE:
                raise RoutingError(
                    f"Routing problem {current.state.problem_id} is already "
                    f"{current.state.status.value}"
                )
            limit_reason = self._limit_reason(
                current.state.round,
                current.state.total_tokens,
                current.state.recursive_spawn_depth,
                current.state.bounce_count,
            )
            if limit_reason is not None:
                if before_record is not None:
                    before_record()
                return self.record(
                    normalized_id,
                    AttemptOutcome.FAILURE,
                    failure_context=f"Model invocation skipped: {limit_reason}",
                    force_human_reason=limit_reason,
                    model_override=model_override,
                )
            try:
                spec = self.config.spec_for(current.state.current_tier)
                if model_override is not None:
                    spec = replace(spec, model=model_override)
                execution = executor.execute(spec, current.decision)
            except Exception as exc:
                execution = failed_model_execution(exc)
            if before_record is not None:
                before_record()
            if persist_task_result is not None:
                execution = replace(
                    execution, task_result=persist_task_result(execution)
                )
            routed_outcome, task_failure, task_handoff = task_transition(execution)
            failure_context = task_failure or execution.failure_context
            usage_violation = self._usage_violation(current, execution)
            if usage_violation is not None:
                remaining_tokens = current.decision.remaining_tokens
                bounded_input = min(execution.input_tokens, remaining_tokens)
                bounded_output = min(
                    execution.output_tokens, remaining_tokens - bounded_input
                )
                return replace(
                    self.record(
                        normalized_id,
                        AttemptOutcome.FAILURE,
                        transition_outcome=AttemptOutcome.FAILURE,
                        input_tokens=bounded_input,
                        output_tokens=bounded_output,
                        failure_context=usage_violation,
                        recursive_spawn_depth=min(
                            execution.recursive_spawn_depth,
                            self.config.limits.max_recursive_spawn_depth,
                        ),
                        force_human_reason=usage_violation,
                        model_override=model_override,
                    ),
                    execution_result=execution.result or None,
                    task_result=execution.task_result,
                )
            return replace(
                self.record(
                    normalized_id,
                    execution.outcome,
                    transition_outcome=routed_outcome,
                    input_tokens=execution.input_tokens,
                    output_tokens=execution.output_tokens,
                    failure_context=failure_context,
                    recursive_spawn_depth=execution.recursive_spawn_depth,
                    force_human_reason=task_handoff,
                    model_override=model_override,
                ),
                execution_result=execution.result or None,
                task_result=execution.task_result,
            )

    def record(
        self: Any,
        problem_id: str,
        outcome: AttemptOutcome | str,
        *,
        transition_outcome: AttemptOutcome | str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        failure_context: str = "",
        recursive_spawn_depth: int = 0,
        transition_id: str | None = None,
        force_human_reason: str | None = None,
        model_override: str | None = None,
    ) -> RoutingResult:
        normalized_id = normalize_problem_id(problem_id)
        try:
            resolved_outcome = AttemptOutcome(outcome)
        except ValueError as exc:
            raise RoutingError(f"Unknown attempt outcome: {outcome}") from exc
        try:
            resolved_transition = AttemptOutcome(
                transition_outcome
                if transition_outcome is not None
                else resolved_outcome
            )
        except ValueError as exc:
            raise RoutingError(
                f"Unknown routing transition outcome: {transition_outcome}"
            ) from exc
        if input_tokens < 0 or output_tokens < 0:
            raise RoutingError("Token usage must not be negative")
        if recursive_spawn_depth < 0:
            raise RoutingError("Recursive spawn depth must not be negative")
        if force_human_reason is not None and not force_human_reason.strip():
            raise RoutingError("Human handoff reason must not be empty")
        state = self.store.get_problem(normalized_id)
        if state is None:
            raise RoutingError(f"Unknown routing problem: {normalized_id}")
        can_resolve_human_handoff = (
            state.status is RoutingStatus.HUMAN_HANDOFF
            and resolved_transition is AttemptOutcome.SUCCESS
        )
        if state.status is not RoutingStatus.ACTIVE and not can_resolve_human_handoff:
            raise RoutingError(
                f"Routing problem {normalized_id} is already {state.status.value}"
            )

        context = compact_context(failure_context)
        if (
            resolved_transition in {AttemptOutcome.FAILURE, AttemptOutcome.RETRY}
            and not context
        ):
            raise RoutingError("Failure context is required for an unresolved attempt")
        if resolved_transition is AttemptOutcome.RETRY and state.current_tier in {
            self.config.writer.tier,
            ModelTier.HUMAN,
        }:
            raise RoutingError("Only a triage tier can return writer retry context")

        spec = self.config.spec_for(state.current_tier)
        if model_override is not None:
            spec = replace(spec, model=model_override)
        attempt_round = state.round + 1
        attempt_tokens = input_tokens + output_tokens
        total_tokens = state.total_tokens + attempt_tokens
        total_depth = max(state.recursive_spawn_depth, recursive_spawn_depth)
        consecutive_failures = (
            state.consecutive_failures + 1
            if resolved_transition is AttemptOutcome.FAILURE
            else state.consecutive_failures
        )
        attempt_bounces = (
            state.bounce_count + 1
            if resolved_transition is not AttemptOutcome.SUCCESS
            else state.bounce_count
        )
        attempt = RoutingAttempt(
            attempt_id=None,
            problem_id=normalized_id,
            round=attempt_round,
            model=spec.model,
            tier=state.current_tier,
            reason=state.next_reason,
            outcome=resolved_outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=attempt_tokens,
            estimated_cost=spec.estimate_cost(input_tokens, output_tokens),
            bounce_count=attempt_bounces,
            recursive_spawn_depth=recursive_spawn_depth,
            failure_context=state.last_failure_context,
            created_at=now(),
        )
        next_state = self._next_state(
            state,
            resolved_transition,
            context,
            attempt_round,
            total_tokens,
            total_depth,
            attempt_bounces,
            attempt.estimated_cost,
            consecutive_failures,
            force_human_reason,
        )
        saved_state, saved_attempt = self.store.save_transition(
            normalized_id, state.round, next_state, attempt, transition_id
        )
        return RoutingResult(
            saved_state,
            self._decision(saved_state),
            saved_attempt,
            self.store.get_attempts(normalized_id),
        )
