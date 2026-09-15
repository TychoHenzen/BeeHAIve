from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from beehaiive.contract_types import TaskOutcome
from beehaiive.graph import GraphEdge
from beehaiive.graph_execution import (
    GRAPH_TRANSITION_CLAIM_SECONDS,
    GraphTransition,
    GraphTransitionStatus,
)

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class GraphTransitionMixin:
    def claim_graph_transition(
        self: Any, replay_id: str, owner_id: str
    ) -> GraphTransition | None:
        if not isinstance(cast(object, replay_id), str) or not replay_id.strip():
            raise StoreError("A replay id is required")
        if not isinstance(cast(object, owner_id), str) or not owner_id.strip():
            raise StoreError("A claim owner is required")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT transition_json
                FROM graph_transitions
                WHERE replay_id = ?
                LIMIT 1
                """,
                (replay_id,),
            ).fetchone()
            if row is not None:
                return _decode_transition(row["transition_json"])
            cutoff = (
                datetime.now(UTC) - timedelta(seconds=GRAPH_TRANSITION_CLAIM_SECONDS)
            ).isoformat()
            connection.execute(
                "DELETE FROM graph_transition_claims "
                "WHERE replay_id = ? AND claimed_at < ?",
                (replay_id, cutoff),
            )
            inserted = connection.execute(
                "INSERT OR IGNORE INTO graph_transition_claims "
                "(replay_id, owner_id, claimed_at) VALUES (?, ?, ?)",
                (replay_id, owner_id, _now()),
            )
            if inserted.rowcount != 1:
                raise StoreError("Graph transition is already in progress")
        return None

    def release_graph_transition(self: Any, replay_id: str, owner_id: str) -> None:
        if not isinstance(cast(object, replay_id), str) or not replay_id.strip():
            raise StoreError("A replay id is required")
        if not isinstance(cast(object, owner_id), str) or not owner_id.strip():
            raise StoreError("A claim owner is required")
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM graph_transition_claims "
                "WHERE replay_id = ? AND owner_id = ?",
                (replay_id, owner_id),
            )

    def graph_transition_for_replay(
        self: Any, replay_id: str
    ) -> GraphTransition | None:
        if not isinstance(cast(object, replay_id), str) or not replay_id.strip():
            raise StoreError("A replay id is required")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT transition_json
                FROM graph_transitions
                WHERE replay_id = ?
                LIMIT 1
                """,
                (replay_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode_transition(row["transition_json"])

    def record_graph_transition(
        self: Any, transition: GraphTransition, *, owner_id: str | None = None
    ) -> GraphTransition:
        if not isinstance(cast(object, transition), GraphTransition):
            raise StoreError("A GraphTransition is required")
        payload = json.dumps(
            transition.as_dict(), sort_keys=True, separators=(",", ":")
        )
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT transition_json
                FROM graph_transitions
                WHERE replay_id = ?
                """,
                (transition.replay_id,),
            ).fetchone()
            if row is not None:
                if row["transition_json"] != payload:
                    raise StoreError("Graph transition replay identity conflicts")
                _delete_claim(connection, transition.replay_id, owner_id)
                return transition
            connection.execute(
                """
                INSERT INTO graph_transitions(
                    replay_id, execution_id, workflow_id, revision, node_id,
                    step, attempt, transition_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.replay_id,
                    transition.execution_id,
                    transition.workflow_id,
                    transition.revision,
                    transition.node_id,
                    transition.step,
                    transition.attempt,
                    payload,
                    transition.created_at,
                ),
            )
            _delete_claim(connection, transition.replay_id, owner_id)
        return transition

    def graph_transitions_for(
        self: Any, execution_id: str
    ) -> tuple[GraphTransition, ...]:
        if not isinstance(cast(object, execution_id), str) or not execution_id.strip():
            raise StoreError("An execution id is required")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT transition_json
                FROM graph_transitions
                WHERE execution_id = ?
                ORDER BY transition_id
                """,
                (execution_id,),
            ).fetchall()
        transitions: list[GraphTransition] = []
        for row in rows:
            transitions.append(_decode_transition(row["transition_json"]))
        return tuple(transitions)


def _delete_claim(connection: Any, replay_id: str, owner_id: str | None) -> None:
    if owner_id is None:
        connection.execute(
            "DELETE FROM graph_transition_claims WHERE replay_id = ?",
            (replay_id,),
        )
    else:
        connection.execute(
            "DELETE FROM graph_transition_claims WHERE replay_id = ? AND owner_id = ?",
            (replay_id, owner_id),
        )


def _decode_transition(value: object) -> GraphTransition:
    try:
        if not isinstance(value, (str, bytes, bytearray)):
            raise ValueError("transition JSON must be text")
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise ValueError("transition must be an object")
        return _transition_from_dict(cast(Mapping[str, object], decoded))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StoreError("Stored graph transition is invalid") from error


def _transition_from_dict(value: Mapping[str, object]) -> GraphTransition:
    selected_edge = value.get("selected_edge")
    return GraphTransition(
        execution_id=_required_text(value.get("execution_id"), "execution_id"),
        task_id=_required_text(value.get("task_id"), "task_id"),
        workflow_id=_required_text(value.get("workflow_id"), "workflow_id"),
        revision=_required_int(value.get("revision"), "revision"),
        node_id=_required_text(value.get("node_id"), "node_id"),
        step=_required_int(value.get("step"), "step"),
        attempt=_required_int(value.get("attempt"), "attempt"),
        outcome=TaskOutcome(_required_text(value.get("outcome"), "outcome")),
        status=GraphTransitionStatus(_required_text(value.get("status"), "status")),
        selected_edge=(GraphEdge.from_dict(selected_edge) if selected_edge else None),
        reason=_required_text(value.get("reason"), "reason"),
        evidence=_required_mapping(value.get("evidence"), "evidence"),
        created_at=_required_text(value.get("created_at"), "created_at"),
        question=_optional_text(value.get("question"), "question"),
        required_action=_optional_text(value.get("required_action"), "required_action"),
        replay_id=_required_text(value.get("replay_id"), "replay_id"),
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _required_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


__all__ = ["GraphTransitionMixin"]
