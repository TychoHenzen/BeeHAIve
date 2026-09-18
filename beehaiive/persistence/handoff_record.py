from __future__ import annotations

from typing import Any

from beehaiive.models import HandoffIntent, HandoffRequest, RunState, RunStatus, Stage

from .constants import MAX_AGENT_RESULT_LENGTH as MAX_AGENT_RESULT_LENGTH
from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class HandoffRecordMixin:
    def record_handoff(
        self: Any,
        run_id: str,
        branch: str,
        pull_request_url: str,
        pull_request_number: int | None,
        lease_token: str,
        result: str | None = None,
    ) -> RunState:
        if not branch.strip() or not pull_request_url.strip():
            raise StoreError("A branch and pull-request URL are required")
        normalized_result = result.strip()[:MAX_AGENT_RESULT_LENGTH] if result else None
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is RunStatus.COMPLETED and row.stage is Stage.PULL_REQUEST:
                return row
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            intent = connection.execute(
                """
                SELECT branch, handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            ).fetchone()
            if (
                intent is None
                or intent["handoff_status"] != "pending"
                or intent["branch"] != branch
            ):
                raise StoreError("Handoff does not match the persisted intent")
            connection.execute(
                """
                UPDATE pbis
                SET stage = ?, branch = ?, pull_request_url = ?,
                    handoff_status = 'completed', claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    Stage.PULL_REQUEST.value,
                    branch,
                    pull_request_url,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', execution_token = NULL,
                    last_error = NULL, last_result = COALESCE(?, last_result),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_result, _now(), run_id),
            )
            connection.execute(
                """
                INSERT INTO handoffs(
                    run_id, project_id, repository_name, pbi_number, branch,
                    pull_request_url, pull_request_number, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                    branch,
                    pull_request_url,
                    pull_request_number,
                    _now(),
                ),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "transition",
                Stage.IMPLEMENT,
                Stage.PULL_REQUEST,
                {"branch": branch, "pull_request_url": pull_request_url},
            )
            return self._run_for_id(connection, run_id) or row

    def handoff_for_pull_request(
        self: Any, repository: str, number: int
    ) -> tuple[HandoffRequest, str]:
        """Return the unique persisted PBI handoff for a pull request."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT h.project_id, h.repository_name, h.pbi_number, h.branch,
                       h.pull_request_url, h.pull_request_number, h.run_id,
                       p.title, p.handoff_base_branch, p.handoff_body,
                       p.handoff_head_sha, p.handoff_verification_evidence
                FROM handoffs AS h
                JOIN pbis AS p
                  ON p.project_id = h.project_id
                 AND p.repository_name = h.repository_name
                 AND p.number = h.pbi_number
                WHERE h.repository_name = ? AND h.pull_request_number = ?
                """,
                (repository, number),
            ).fetchall()
            if len(rows) != 1:
                raise StoreError(
                    f"Expected one persisted handoff for {repository}#{number}, "
                    f"found {len(rows)}"
                )
            row = rows[0]
            return (
                HandoffRequest(
                    project_id=str(row["project_id"]),
                    repository=str(row["repository_name"]),
                    pbi_number=int(row["pbi_number"]),
                    title=str(row["title"]),
                    branch=str(row["branch"]),
                    base_branch=row["handoff_base_branch"],
                    body=str(row["handoff_body"] or ""),
                    run_id=str(row["run_id"]),
                    head_sha=row["handoff_head_sha"],
                    verification_evidence=str(
                        row["handoff_verification_evidence"] or ""
                    ),
                    mutation_audit=self,
                ),
                str(row["pull_request_url"]),
            )

    def prepare_handoff(
        self: Any,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
        head_sha: str | None = None,
        verification_evidence: str = "",
    ) -> HandoffIntent:
        if not branch.strip():
            raise StoreError("A branch is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            persisted = connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body,
                       handoff_head_sha, handoff_verification_evidence,
                       handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            ).fetchone()
            if persisted is None:
                raise StoreError(f"Unknown PBI for run: {run_id}")
            if row.status is RunStatus.COMPLETED and row.stage is Stage.PULL_REQUEST:
                return HandoffIntent(
                    row,
                    str(persisted["branch"]),
                    persisted["handoff_base_branch"],
                    str(persisted["handoff_body"] or ""),
                    persisted["handoff_head_sha"],
                    str(persisted["handoff_verification_evidence"] or ""),
                )
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            if persisted["handoff_status"] == "pending":
                if (
                    persisted["branch"] != branch
                    or persisted["handoff_base_branch"] != base_branch
                    or persisted["handoff_body"] != body
                    or persisted["handoff_head_sha"] != head_sha
                    or str(persisted["handoff_verification_evidence"] or "")
                    != verification_evidence
                ):
                    raise StoreError(
                        "Handoff request does not match the persisted intent"
                    )
                self._renew_lease(connection, run_id, lease_token)
                renewed_row = self._run_for_id(connection, run_id)
                if renewed_row is None:
                    raise StoreError(f"Unknown run: {run_id}")
                return HandoffIntent(
                    renewed_row,
                    str(persisted["branch"]),
                    persisted["handoff_base_branch"],
                    str(persisted["handoff_body"]),
                    persisted["handoff_head_sha"],
                    str(persisted["handoff_verification_evidence"] or ""),
                )
            if persisted["handoff_status"] == "completed":
                raise StoreError("Handoff intent is already completed")
            connection.execute(
                """
                UPDATE pbis
                SET branch = ?, handoff_base_branch = ?, handoff_body = ?,
                    handoff_head_sha = ?, handoff_verification_evidence = ?,
                    handoff_status = 'pending'
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    branch,
                    base_branch,
                    body,
                    head_sha,
                    verification_evidence,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            self._renew_lease(connection, run_id, lease_token)
            persisted_row = self._run_for_id(connection, run_id)
            if persisted_row is None:
                raise StoreError(f"Unknown run: {run_id}")
            return HandoffIntent(
                persisted_row,
                branch,
                base_branch,
                body,
                head_sha,
                verification_evidence,
            )

    def pending_handoff(
        self: Any, run_id: str, lease_token: str
    ) -> HandoffIntent | None:
        with self._transaction():
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            row = self._connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body,
                       handoff_head_sha, handoff_verification_evidence,
                       handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (run.project_id, run.repository, run.pbi_number),
            ).fetchone()
            if row is None or row["handoff_status"] != "pending":
                return None
            return HandoffIntent(
                run,
                str(row["branch"]),
                row["handoff_base_branch"],
                str(row["handoff_body"] or ""),
                row["handoff_head_sha"],
                str(row["handoff_verification_evidence"] or ""),
            )


__all__ = ["HandoffRecordMixin"]
