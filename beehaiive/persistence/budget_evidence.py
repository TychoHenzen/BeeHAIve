from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from beehaiive.scheduler_types.budget import AccountUsageSnapshot, BudgetDecision

from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _json_mapping as _json_mapping

_RESET_BASELINE_REASONS = frozenset(
    {
        "budget_evidence_stale",
        "budget_evidence_unavailable",
        "budget_evidence_contradictory",
        "budget_buckets_missing",
        "budget_bucket_values_invalid",
        "budget_exhausted",
        "budget_fallback_model_unavailable",
    }
)


class BudgetEvidenceMixin:
    def record_budget_decision(
        self: Any,
        project_id: str,
        snapshot: AccountUsageSnapshot,
        decision: BudgetDecision,
    ) -> dict[str, object]:
        from beehaiive.scheduler_types.budget import (
            MAX_BUDGET_EVIDENCE_RECORDS,
            budget_replay_id,
        )

        if not project_id.strip():
            raise StoreError("A project id is required")
        replay_id = budget_replay_id(project_id, snapshot, decision)
        created_at = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO budget_decision_evidence(
                    project_id, replay_id, source_id, source_version,
                    evidence_status, action, reason_code, fallback_model,
                    observed_at, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    replay_id,
                    snapshot.source_id,
                    snapshot.source_version,
                    snapshot.evidence.value,
                    decision.action.value,
                    decision.reason.value,
                    decision.fallback_model,
                    snapshot.observed_at,
                    json.dumps(snapshot.as_dict(), sort_keys=True),
                    created_at,
                ),
            )
            rows = connection.execute(
                """
                SELECT evidence_id, reason_code
                FROM budget_decision_evidence
                WHERE project_id = ?
                ORDER BY evidence_id DESC
                """,
                (project_id,),
            ).fetchall()
            keep_ids = [
                int(row["evidence_id"]) for row in rows[:MAX_BUDGET_EVIDENCE_RECORDS]
            ]
            if rows and rows[0]["reason_code"] == "budget_reset_unverified":
                baseline_id = next(
                    (
                        int(row["evidence_id"])
                        for row in rows[1:]
                        if row["reason_code"] in _RESET_BASELINE_REASONS
                    ),
                    None,
                )
                if baseline_id is not None and baseline_id not in keep_ids:
                    keep_ids = keep_ids[: MAX_BUDGET_EVIDENCE_RECORDS - 1]
                    keep_ids.append(baseline_id)
            keep_id_set = set(keep_ids)
            delete_ids = [
                int(row["evidence_id"])
                for row in rows
                if int(row["evidence_id"]) not in keep_id_set
            ]
            if delete_ids:
                placeholders = ", ".join("?" for _ in delete_ids)
                connection.execute(
                    f"DELETE FROM budget_decision_evidence "
                    f"WHERE project_id = ? AND evidence_id IN ({placeholders})",
                    (project_id, *delete_ids),
                )
        return {
            "project_id": project_id,
            "replay_id": replay_id,
            "source_id": snapshot.source_id,
            "source_version": snapshot.source_version,
            "evidence_status": snapshot.evidence.value,
            "action": decision.action.value,
            "reason_code": decision.reason.value,
            "observed_at": snapshot.observed_at,
            "created_at": created_at,
        }

    def budget_snapshot_for_project(
        self: Any, project_id: str
    ) -> AccountUsageSnapshot | None:
        from beehaiive.scheduler_types.budget import AccountUsageSnapshot

        with self._lock:
            row = self._connection.execute(
                """
                SELECT snapshot_json
                FROM budget_decision_evidence
                WHERE project_id = ?
                ORDER BY evidence_id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(str(row["snapshot_json"]))
            if not isinstance(snapshot, Mapping):
                raise ValueError("snapshot must be a mapping")
            return AccountUsageSnapshot.from_dict(cast(Mapping[str, object], snapshot))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StoreError("Stored budget evidence is invalid") from error

    def budget_decision_for_project(
        self: Any, project_id: str
    ) -> BudgetDecision | None:
        from beehaiive.scheduler_types.budget import (
            BudgetAction,
            BudgetDecision,
            BudgetReason,
        )

        with self._lock:
            row = self._connection.execute(
                """
                SELECT action, reason_code, source_version, fallback_model
                FROM budget_decision_evidence
                WHERE project_id = ?
                ORDER BY evidence_id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return BudgetDecision(
                action=BudgetAction(row["action"]),
                reason=BudgetReason(row["reason_code"]),
                source_version=row["source_version"],
                fallback_model=row["fallback_model"],
            )
        except (TypeError, ValueError) as error:
            raise StoreError("Stored budget decision is invalid") from error

    def budget_reset_baseline_for_project(
        self: Any, project_id: str
    ) -> AccountUsageSnapshot | None:
        from beehaiive.scheduler_types.budget import AccountUsageSnapshot

        placeholders = ", ".join("?" for _ in _RESET_BASELINE_REASONS)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT snapshot_json
                FROM budget_decision_evidence
                WHERE project_id = ?
                  AND reason_code IN ({placeholders})
                ORDER BY evidence_id DESC
                """,
                (project_id, *_RESET_BASELINE_REASONS),
            ).fetchall()
        for row in rows:
            try:
                snapshot = json.loads(str(row["snapshot_json"]))
                if not isinstance(snapshot, Mapping):
                    raise ValueError("snapshot must be a mapping")
                return AccountUsageSnapshot.from_dict(
                    cast(Mapping[str, object], snapshot)
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise StoreError("Stored budget evidence is invalid") from error
        return None

    def budget_evidence_for(
        self: Any, project_id: str
    ) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT replay_id, source_id, source_version, evidence_status,
                       action, reason_code, fallback_model, observed_at,
                       snapshot_json, created_at
                FROM budget_decision_evidence
                WHERE project_id = ?
                ORDER BY evidence_id
                """,
                (project_id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            snapshot = _json_mapping(row["snapshot_json"])
            result.append(
                {
                    "replay_id": row["replay_id"],
                    "source_id": row["source_id"],
                    "source_version": row["source_version"],
                    "evidence_status": row["evidence_status"],
                    "action": row["action"],
                    "reason_code": row["reason_code"],
                    "fallback_model": row["fallback_model"],
                    "observed_at": row["observed_at"],
                    "snapshot": snapshot,
                    "created_at": row["created_at"],
                }
            )
        return tuple(result)


__all__ = ["BudgetEvidenceMixin"]
