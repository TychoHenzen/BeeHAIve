from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from beehaiive.models import HandoffRequest

from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _json_mapping as _json_mapping


class HandoffMutationMixin:
    def begin_handoff_mutation(
        self: Any,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        target: Mapping[str, object],
    ) -> str:
        """Record a safe GitHub mutation intent before its provider call."""

        if mutation not in {
            "create_ref",
            "create_pull_request",
            "merge_pull_request",
            "close_issue",
            "delete_ref",
            "update_project_status",
        }:
            raise StoreError("Unsupported GitHub handoff mutation")
        if not operation_key.strip() or not request.run_id.strip():
            raise StoreError("A handoff mutation identity is required")
        allowed_target_fields = {
            "branch",
            "base_branch",
            "base_sha",
            "head_sha",
            "pull_request_number",
            "pull_request_url",
            "review_cycle_id",
            "approved_by_human",
            "approval_actor",
            "approval_reason",
            "approval_at",
            "issue_number",
            "issue_id",
            "ref_id",
            "project_item_id",
            "field_id",
            "option_id",
            "status",
        }
        if any(
            key not in allowed_target_fields or not isinstance(value, str)
            for key, value in target.items()
        ):
            raise StoreError("Handoff mutation target contains unsafe fields")

        kind = f"github.{mutation}"
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT request_json, status FROM actions
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                    AND run_id = ? AND kind = ?
                """,
                (
                    request.project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                    kind,
                ),
            ).fetchall()
            attempt = 1
            for row in rows:
                prior_request = _json_mapping(row["request_json"])
                if prior_request.get("operation_key") != operation_key:
                    continue
                if row["status"] in {"pending", "uncertain"}:
                    raise StoreError(
                        "A prior GitHub handoff mutation remains unresolved"
                    )
                prior_attempt = prior_request.get("attempt")
                if type(prior_attempt) is int:
                    attempt = max(attempt, prior_attempt + 1)

            if (
                mutation
                in {
                    "merge_pull_request",
                    "close_issue",
                    "delete_ref",
                    "update_project_status",
                }
                and attempt > 2
            ):
                raise StoreError("The one-retry limit for this handoff step is spent")

            action_id = str(uuid4())
            now = _now()
            safe_request = {
                "operation_key": operation_key,
                "attempt": attempt,
                "target": dict(target),
            }
            connection.execute(
                """
                INSERT INTO actions(
                    action_id, project_id, kind, status, repository_name,
                    pbi_number, run_id, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    request.project_id,
                    kind,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                    json.dumps(safe_request, sort_keys=True),
                    now,
                    now,
                ),
            )
            return action_id

    def handoff_mutation_action(
        self: Any, request: HandoffRequest, mutation: str, operation_key: str
    ) -> dict[str, object] | None:
        """Return the latest audit record for one stable handoff operation."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM actions
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                    AND run_id = ? AND kind = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (
                    request.project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                    f"github.{mutation}",
                ),
            ).fetchall()
            matching = [
                row
                for row in rows
                if _json_mapping(row["request_json"]).get("operation_key")
                == operation_key
            ]
            if not matching:
                return None
            return self._action_from_row(matching[0])

    def finish_handoff_mutation(
        self: Any,
        action_id: str,
        status: str,
        result: Mapping[str, object],
    ) -> None:
        """Store a redacted result for one GitHub mutation attempt."""

        if status not in {"succeeded", "failed", "uncertain"}:
            raise StoreError(f"Invalid handoff mutation status: {status}")
        safe_result = dict(result)
        error_class = safe_result.get("error_class")
        self.finish_action(
            action_id,
            status,
            safe_result,
            error=(
                error_class
                if status != "succeeded" and isinstance(error_class, str)
                else None
            ),
        )

    def reconcile_handoff_mutation(
        self: Any,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        status: str,
        result: Mapping[str, object],
    ) -> None:
        """Resolve only unfinished attempts for the same stable operation."""

        if mutation not in {
            "create_ref",
            "create_pull_request",
            "merge_pull_request",
            "close_issue",
            "delete_ref",
            "update_project_status",
        }:
            raise StoreError("Unsupported GitHub handoff mutation")
        if status not in {"succeeded", "failed", "uncertain"}:
            raise StoreError(f"Invalid handoff mutation status: {status}")
        safe_result = dict(result)
        error_class = safe_result.get("error_class")
        error = (
            error_class
            if status != "succeeded" and isinstance(error_class, str)
            else None
        )
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT action_id, request_json FROM actions
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                    AND run_id = ? AND kind = ?
                    AND status IN ('pending', 'uncertain')
                """,
                (
                    request.project_id,
                    request.repository,
                    request.pbi_number,
                    request.run_id,
                    f"github.{mutation}",
                ),
            ).fetchall()
            for row in rows:
                prior_request = _json_mapping(row["request_json"])
                if prior_request.get("operation_key") != operation_key:
                    continue
                connection.execute(
                    """
                    UPDATE actions
                    SET status = ?, result_json = ?, error = ?, updated_at = ?
                    WHERE action_id = ? AND status IN ('pending', 'uncertain')
                    """,
                    (
                        status,
                        json.dumps(safe_result, sort_keys=True),
                        error,
                        _now(),
                        row["action_id"],
                    ),
                )


__all__ = ["HandoffMutationMixin"]
