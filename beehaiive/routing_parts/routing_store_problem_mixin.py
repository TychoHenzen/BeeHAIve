from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

from .helpers import attempt_from_row, state_from_row, state_values
from .routing_error import RoutingError

if TYPE_CHECKING:
    from .routing_attempt import RoutingAttempt
    from .routing_state import RoutingState
from typing import Any


class RoutingStoreProblemMixin:
    def create_problem(self: Any, state: RoutingState) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                        INSERT INTO routing_problems(
                            problem_id, status, current_tier, triage_index,
                            consecutive_failures, bounce_count, round, total_tokens,
                            total_cost, recursive_spawn_depth, last_failure_context,
                            required_action, next_reason, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                    state_values(state),
                )
        except sqlite3.IntegrityError as exc:
            raise RoutingError(
                f"Routing problem {state.problem_id} already exists"
            ) from exc

    def get_problem(self: Any, problem_id: str) -> RoutingState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
        return None if row is None else state_from_row(row)

    def get_attempts(
        self: Any, problem_id: str, limit: int | None = None
    ) -> tuple[RoutingAttempt, ...]:
        if limit is not None and limit <= 0:
            raise RoutingError("attempt limit must be positive")
        query = """
                SELECT * FROM routing_attempts
                WHERE problem_id = ?
                ORDER BY attempt_id
            """
        parameters: tuple[object, ...] = (problem_id,)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(attempt_from_row(row) for row in rows)

    def has_transition(self: Any, transition_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM routing_attempts WHERE transition_id = ?",
                (transition_id,),
            ).fetchone()
        return row is not None

    def save_transition(
        self: Any,
        problem_id: str,
        expected_round: int,
        state: RoutingState,
        attempt: RoutingAttempt,
        transition_id: str | None = None,
    ) -> tuple[RoutingState, RoutingAttempt]:
        with self._transaction() as connection:
            if transition_id is not None:
                existing_attempt = connection.execute(
                    "SELECT * FROM routing_attempts WHERE transition_id = ?",
                    (transition_id,),
                ).fetchone()
                if existing_attempt is not None:
                    if str(existing_attempt["problem_id"]) != problem_id:
                        raise RoutingError(
                            "Routing transition belongs to another problem"
                        )
                    existing_state = connection.execute(
                        "SELECT * FROM routing_problems WHERE problem_id = ?",
                        (problem_id,),
                    ).fetchone()
                    if existing_state is None:
                        raise RoutingError(f"Unknown routing problem: {problem_id}")
                    return state_from_row(existing_state), attempt_from_row(
                        existing_attempt
                    )
            current = connection.execute(
                "SELECT round, status FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
            if current is None:
                raise RoutingError(f"Unknown routing problem: {problem_id}")
            if int(current["round"]) != expected_round:
                raise RoutingError("Routing problem changed during the transition")
            connection.execute(
                """
                    UPDATE routing_problems
                    SET status = ?, current_tier = ?, triage_index = ?,
                        consecutive_failures = ?, bounce_count = ?, round = ?,
                        total_tokens = ?, total_cost = ?,
                        recursive_spawn_depth = ?, last_failure_context = ?,
                        required_action = ?, next_reason = ?, updated_at = ?
                    WHERE problem_id = ?
                    """,
                (
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
                    state.updated_at,
                    problem_id,
                ),
            )
            cursor = connection.execute(
                """
                    INSERT INTO routing_attempts(
                        problem_id, round, model, tier, reason, outcome,
                        input_tokens, output_tokens, total_tokens, estimated_cost,
                        bounce_count, recursive_spawn_depth, failure_context,
                        created_at, transition_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    attempt.problem_id,
                    attempt.round,
                    attempt.model,
                    attempt.tier.value,
                    attempt.reason,
                    attempt.outcome.value,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.total_tokens,
                    attempt.estimated_cost,
                    attempt.bounce_count,
                    attempt.recursive_spawn_depth,
                    attempt.failure_context,
                    attempt.created_at,
                    transition_id,
                ),
            )
            attempt_id = cursor.lastrowid
            if attempt_id is None:  # pragma: no cover - SQLite assigns row ids
                raise RoutingError("Routing attempt was not assigned an id")
            saved_state = state_from_row(
                connection.execute(
                    "SELECT * FROM routing_problems WHERE problem_id = ?",
                    (problem_id,),
                ).fetchone()
            )
        return saved_state, replace(attempt, attempt_id=int(attempt_id))

    def reopen_problem(
        self: Any, problem_id: str, expected_round: int, state: RoutingState
    ) -> RoutingState:
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT round FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
            if current is None:
                raise RoutingError(f"Unknown routing problem: {problem_id}")
            if int(current["round"]) != expected_round:
                raise RoutingError("Routing problem changed during recovery")
            connection.execute(
                """
                    UPDATE routing_problems
                    SET status = ?, current_tier = ?, triage_index = ?,
                        consecutive_failures = ?, bounce_count = ?, round = ?,
                        total_tokens = ?, total_cost = ?, recursive_spawn_depth = ?,
                        last_failure_context = ?, required_action = ?,
                        next_reason = ?, updated_at = ?
                    WHERE problem_id = ?
                    """,
                (
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
                    state.updated_at,
                    problem_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the update
                raise RoutingError(f"Unknown routing problem: {problem_id}")
            return state_from_row(row)
