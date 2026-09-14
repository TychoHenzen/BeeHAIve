from __future__ import annotations

from typing import Any

from beehaiive.models import RoutingFailure

from .helpers.lease_helpers import _now as _now


class RoutingFailureMixin:
    def pending_routing_failures(self: Any, run_id: str) -> tuple[RoutingFailure, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT transition_id, run_id, error, input_tokens, output_tokens,
                       recursive_spawn_depth
                FROM routing_failure_outbox
                WHERE run_id = ? AND status = 'pending'
                ORDER BY created_at, transition_id
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            RoutingFailure(
                transition_id=str(row["transition_id"]),
                run_id=str(row["run_id"]),
                error=str(row["error"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                recursive_spawn_depth=int(row["recursive_spawn_depth"]),
            )
            for row in rows
        )

    def mark_routing_failure_processed(self: Any, transition_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE routing_failure_outbox
                SET status = 'processed', processed_at = ?
                WHERE transition_id = ? AND status = 'pending'
                """,
                (_now(), transition_id),
            )


__all__ = ["RoutingFailureMixin"]
