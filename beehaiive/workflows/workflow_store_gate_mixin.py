from __future__ import annotations

from typing import Any
from uuid import uuid4

from .gate_result import GateResult
from .helpers import checks_from_json, checks_to_json, current_timestamp
from .workflow_error import WorkflowError


class WorkflowStoreGateMixin:
    def record_gate(self: Any, lease_id: str, result: GateResult) -> None:
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT lease_id FROM workflow_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            connection.execute(
                """
                    INSERT INTO workflow_gates(
                        gate_id, lease_id, gate, allowed, checks_json,
                        required_action, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    str(uuid4()),
                    lease_id,
                    result.gate,
                    int(result.allowed),
                    checks_to_json(result.checks),
                    result.required_action,
                    current_timestamp(),
                ),
            )

    def latest_gate(self: Any, lease_id: str, gate: str) -> GateResult | None:
        with self._lock:
            row = self._connection.execute(
                """
                    SELECT gate, allowed, checks_json, required_action
                    FROM workflow_gates
                    WHERE lease_id = ? AND gate = ?
                    ORDER BY created_at DESC, gate_id DESC
                    LIMIT 1
                    """,
                (lease_id, gate),
            ).fetchone()
        if row is None:
            return None
        return GateResult(
            str(row["gate"]),
            bool(row["allowed"]),
            checks_from_json(str(row["checks_json"])),
            None if row["required_action"] is None else str(row["required_action"]),
        )
