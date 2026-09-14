from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .attempt_outcome import AttemptOutcome
from .helpers import normalize_problem_id, now
from .model_tier import ModelTier
from .routing_decision import RoutingDecision
from .routing_error import RoutingError
from .routing_status import RoutingStatus

if TYPE_CHECKING:
    from .model_execution import ModelExecution
    from .routing_result import RoutingResult
    from .routing_state import RoutingState
from typing import Any


class RouterDecisionMixin:
    def _next_state(
        self: Any,
        state: RoutingState,
        outcome: AttemptOutcome,
        context: str,
        attempt_round: int,
        total_tokens: int,
        total_depth: int,
        attempt_bounces: int,
        attempt_cost: float,
        consecutive_failures: int,
        force_human_reason: str | None = None,
    ) -> RoutingState:
        total_cost = round(state.total_cost + attempt_cost, 8)
        if force_human_reason is not None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                force_human_reason,
                context,
                consecutive_failures,
            )
        limit_reason = self._limit_reason(
            attempt_round, total_tokens, total_depth, attempt_bounces
        )
        if outcome is AttemptOutcome.SUCCESS:
            if state.current_tier is not ModelTier.HUMAN and limit_reason is not None:
                return self._human_state(
                    state,
                    attempt_round,
                    total_tokens,
                    total_cost,
                    total_depth,
                    attempt_bounces,
                    limit_reason,
                    context,
                    consecutive_failures,
                )
            return replace(
                state,
                status=RoutingStatus.RESOLVED,
                current_tier=self.config.writer.tier,
                triage_index=0,
                consecutive_failures=0,
                bounce_count=0,
                round=attempt_round,
                total_tokens=total_tokens,
                total_cost=total_cost,
                recursive_spawn_depth=total_depth,
                last_failure_context=None,
                required_action=None,
                next_reason="reset after successful fix",
                updated_at=now(),
            )

        if limit_reason is not None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                limit_reason,
                context,
                consecutive_failures,
            )

        if outcome is AttemptOutcome.RETRY:
            return replace(
                state,
                current_tier=self.config.writer.tier,
                round=attempt_round,
                total_tokens=total_tokens,
                total_cost=total_cost,
                recursive_spawn_depth=total_depth,
                bounce_count=attempt_bounces,
                last_failure_context=context,
                required_action=None,
                next_reason="triage context returned to writer",
                updated_at=now(),
            )

        next_tier, next_index = self._next_triage_tier(state)
        if next_tier is None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                "All configured triage tiers failed",
                context,
                consecutive_failures,
            )
        return replace(
            state,
            current_tier=next_tier,
            triage_index=next_index,
            consecutive_failures=consecutive_failures,
            round=attempt_round,
            total_tokens=total_tokens,
            total_cost=total_cost,
            recursive_spawn_depth=total_depth,
            bounce_count=attempt_bounces,
            last_failure_context=context,
            required_action=None,
            next_reason=f"escalation after {state.current_tier.value} failure",
            updated_at=now(),
        )

    def _next_triage_tier(
        self: Any, state: RoutingState
    ) -> tuple[ModelTier | None, int]:
        if state.triage_index >= len(self.config.triage):
            return None, state.triage_index
        next_spec = self.config.triage[state.triage_index]
        return next_spec.tier, state.triage_index + 1

    def _limit_reason(
        self: Any, round_number: int, total_tokens: int, depth: int, bounces: int
    ) -> str | None:
        limits = self.config.limits
        if round_number >= limits.max_rounds:
            return f"Maximum routing rounds reached ({limits.max_rounds})"
        if total_tokens >= limits.max_tokens:
            return f"Token ceiling reached ({limits.max_tokens})"
        if depth > limits.max_recursive_spawn_depth:
            return f"Recursive spawn limit reached ({limits.max_recursive_spawn_depth})"
        if bounces >= limits.max_bounces:
            return f"Maximum routing bounces reached ({limits.max_bounces})"
        return None

    @staticmethod
    def _human_state(
        state: RoutingState,
        round_number: int,
        total_tokens: int,
        total_cost: float,
        depth: int,
        bounces: int,
        reason: str,
        failure_context: str,
        consecutive_failures: int,
    ) -> RoutingState:
        return replace(
            state,
            status=RoutingStatus.HUMAN_HANDOFF,
            current_tier=ModelTier.HUMAN,
            round=round_number,
            total_tokens=total_tokens,
            total_cost=total_cost,
            recursive_spawn_depth=depth,
            bounce_count=bounces,
            consecutive_failures=consecutive_failures,
            last_failure_context=failure_context,
            required_action=f"Human action required: {reason}",
            next_reason="human handoff",
            updated_at=now(),
        )

    def _decision(self: Any, state: RoutingState) -> RoutingDecision:
        spec = self.config.spec_for(state.current_tier)
        limits = self.config.limits
        return RoutingDecision(
            problem_id=state.problem_id,
            status=state.status,
            tier=state.current_tier,
            model=spec.model,
            reason=state.next_reason,
            failure_context=state.last_failure_context,
            requires_human=state.status is RoutingStatus.HUMAN_HANDOFF,
            round=state.round,
            consecutive_failures=state.consecutive_failures,
            bounce_count=state.bounce_count,
            total_tokens=state.total_tokens,
            total_cost=state.total_cost,
            remaining_rounds=max(0, limits.max_rounds - state.round),
            remaining_tokens=max(0, limits.max_tokens - state.total_tokens),
            remaining_recursive_spawn_depth=max(
                0, limits.max_recursive_spawn_depth - state.recursive_spawn_depth
            ),
            remaining_bounces=max(0, limits.max_bounces - state.bounce_count),
        )

    def handoff_limit_reason(self: Any, problem_id: str) -> str | None:
        """Return a limit that blocks resolving a handoff without a model call."""

        state = self.store.get_problem(normalize_problem_id(problem_id))
        if state is None:
            raise RoutingError(f"Unknown routing problem: {problem_id}")
        if state.status is not RoutingStatus.ACTIVE:
            return None
        return self._limit_reason(
            state.round + 1,
            state.total_tokens,
            state.recursive_spawn_depth,
            state.bounce_count,
        )

    def _usage_violation(
        self: Any, current: RoutingResult, execution: ModelExecution
    ) -> str | None:
        if (
            execution.input_tokens + execution.output_tokens
            > current.decision.remaining_tokens
        ):
            return (
                "Token ceiling reached. Model reported more tokens than the "
                f"remaining routing budget ({current.decision.remaining_tokens})"
            )
        if (
            execution.recursive_spawn_depth
            > self.config.limits.max_recursive_spawn_depth
        ):
            return (
                "Recursive spawn limit reached. Model reported depth "
                f"{execution.recursive_spawn_depth} above the configured limit "
                f"({self.config.limits.max_recursive_spawn_depth})"
            )
        return None
