from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from .finding_status import FindingStatus
from .helpers import (
    claim_is_active,
    coerce_enum,
    current_timestamp,
    finding_anchor,
    finding_fingerprint,
    json_list,
    normalized_evidence_refs,
    require_text,
    validate_evidence_refs,
)
from .reader_status import ReaderStatus
from .review_concern import ReviewConcern
from .review_cycle_status import ReviewCycleStatus
from .review_error import ReviewError

if TYPE_CHECKING:
    from .review_snapshot import ReviewSnapshot
from typing import Any


class ReviewServiceFindingRecordMixin:
    def _insert_finding(
        self: Any,
        connection: sqlite3.Connection,
        cycle: sqlite3.Row,
        concern: ReviewConcern,
        summary: str,
        evidence_refs: tuple[str, ...],
        *,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        duplicate_target: str | None = None,
    ) -> str:
        file_path, start_line, end_line = finding_anchor(
            file_path, start_line, end_line
        )
        pull_request_id = str(cycle["pull_request_id"])
        if duplicate_target is not None:
            duplicate_target = require_text(duplicate_target, "duplicate target", 200)
            target = connection.execute(
                """
                    SELECT pull_request_id, duplicate_target
                    FROM review_findings WHERE finding_id = ?
                    """,
                (duplicate_target,),
            ).fetchone()
            if target is None or str(target["pull_request_id"]) != pull_request_id:
                raise ReviewError(
                    "Duplicate target must belong to the same pull request"
                )
            duplicate_target = target["duplicate_target"] or duplicate_target
        fingerprint = finding_fingerprint(
            concern, summary, file_path, start_line, end_line
        )
        if duplicate_target is None:
            existing = connection.execute(
                """
                    SELECT finding_id, evidence_refs_json FROM review_findings
                    WHERE pull_request_id = ? AND head_sha = ? AND fingerprint = ?
                      AND status = ? AND duplicate_target IS NULL
                    ORDER BY created_at, finding_id LIMIT 1
                    """,
                (
                    pull_request_id,
                    str(cycle["head_sha"]),
                    fingerprint,
                    FindingStatus.OPEN.value,
                ),
            ).fetchone()
            if existing is not None:
                finding_id = str(existing["finding_id"])
                refs = normalized_evidence_refs(
                    (*json_list(str(existing["evidence_refs_json"])), *evidence_refs)
                )
                connection.execute(
                    """
                        UPDATE review_findings
                        SET evidence_refs_json = ?, updated_at = ?
                        WHERE finding_id = ?
                        """,
                    (json.dumps(refs), current_timestamp(), finding_id),
                )
                return finding_id
        first_seen = connection.execute(
            """
                SELECT first_seen_cycle_id FROM review_findings
                WHERE pull_request_id = ? AND fingerprint = ?
                ORDER BY created_at, finding_id LIMIT 1
                """,
            (pull_request_id, fingerprint),
        ).fetchone()
        first_seen_cycle_id = (
            str(first_seen["first_seen_cycle_id"])
            if first_seen is not None and first_seen["first_seen_cycle_id"]
            else str(cycle["cycle_id"])
        )
        finding_id = str(uuid4())
        timestamp = current_timestamp()
        connection.execute(
            """
                INSERT INTO review_findings(
                    finding_id, pull_request_id, cycle_id, concern, summary,
                    status, resolution, created_at, updated_at, evidence_refs_json,
                    head_sha, fingerprint, file_path, start_line, end_line,
                    duplicate_target, first_seen_cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                finding_id,
                pull_request_id,
                str(cycle["cycle_id"]),
                concern.value,
                summary,
                FindingStatus.OPEN.value,
                timestamp,
                timestamp,
                json.dumps(evidence_refs),
                str(cycle["head_sha"]),
                fingerprint,
                file_path,
                start_line,
                end_line,
                duplicate_target,
                first_seen_cycle_id,
            ),
        )
        return finding_id

    def record_reader(
        self: Any,
        cycle_id: str,
        concern: ReviewConcern | str,
        status: ReaderStatus | str,
        findings: Iterable[str] = (),
        reader: str = "automated",
        claim_token: str | None = None,
        evidence_refs: Iterable[str] = (),
    ) -> ReviewSnapshot:
        concern = coerce_enum(concern, ReviewConcern, "review concern")
        status = coerce_enum(status, ReaderStatus, "reader status")
        reader = require_text(reader, "reader", 100)
        summaries = tuple(
            require_text(summary, "finding summary", 1_000) for summary in findings
        )
        refs = normalized_evidence_refs(evidence_refs)
        if status is ReaderStatus.FAIL and not summaries:
            raise ReviewError("A failed reader must provide findings")
        if status is not ReaderStatus.FAIL and summaries:
            raise ReviewError("Only a failed reader can provide findings")
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            validate_evidence_refs(
                cycle["github_evidence_json"],
                refs,
                required=(
                    cycle["github_evidence_json"] is not None
                    and status in {ReaderStatus.PASS, ReaderStatus.FAIL}
                ),
            )
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Reader result belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting reader results")
            existing = connection.execute(
                "SELECT * FROM review_readers WHERE cycle_id = ? AND concern = ?",
                (cycle_id, concern.value),
            ).fetchone()
            if existing is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            if ReaderStatus(str(existing["status"])) is not ReaderStatus.PENDING:
                raise ReviewError(f"Reader result already recorded for {concern.value}")
            if claim_token is not None and (
                existing["claim_token"] != claim_token
                or not claim_is_active(existing["claim_expires_at"])
            ):
                raise ReviewError("Reader claim is no longer valid")
            finding_ids: list[str] = []
            timestamp = current_timestamp()
            for summary in summaries:
                finding_id = self._insert_finding(
                    connection, cycle, concern, summary, refs
                )
                if finding_id not in finding_ids:
                    finding_ids.append(finding_id)
            connection.execute(
                """
                    UPDATE review_readers
                    SET status = ?, finding_ids_json = ?, reader = ?, updated_at = ?,
                        evidence_refs_json = ?,
                        claim_token = NULL, claim_expires_at = NULL
                    WHERE cycle_id = ? AND concern = ?
                    """,
                (
                    status.value,
                    json.dumps(finding_ids),
                    reader,
                    timestamp,
                    json.dumps(refs),
                    cycle_id,
                    concern.value,
                ),
            )
            self._recompute_cycle(connection, cycle_id)
        return self.store.snapshot(cycle_id)
