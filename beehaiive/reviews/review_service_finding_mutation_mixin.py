from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .finding_status import FindingStatus
from .helpers import (
    coerce_enum,
    current_timestamp,
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


class ReviewServiceFindingMutationMixin:
    def add_finding(
        self: Any,
        cycle_id: str,
        concern: ReviewConcern | str,
        summary: str,
        evidence_refs: Iterable[str] = (),
        *,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        duplicate_target: str | None = None,
    ) -> ReviewSnapshot:
        concern = coerce_enum(concern, ReviewConcern, "review concern")
        summary = require_text(summary, "finding summary", 1_000)
        refs = normalized_evidence_refs(evidence_refs)
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            validate_evidence_refs(
                cycle["github_evidence_json"],
                refs,
                required=cycle["github_evidence_json"] is not None,
            )
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Finding belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting findings")
            reader = connection.execute(
                """
                    SELECT finding_ids_json, evidence_refs_json FROM review_readers
                    WHERE cycle_id = ? AND concern = ?
                    """,
                (cycle_id, concern.value),
            ).fetchone()
            if reader is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            finding_id = self._insert_finding(
                connection,
                cycle,
                concern,
                summary,
                refs,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                duplicate_target=duplicate_target,
            )
            timestamp = current_timestamp()
            finding_ids = list(json_list(str(reader["finding_ids_json"])))
            if finding_id not in finding_ids:
                finding_ids.append(finding_id)
            reader_refs = normalized_evidence_refs(
                (*json_list(str(reader["evidence_refs_json"])), *refs)
            )
            connection.execute(
                """
                    UPDATE review_readers
                    SET status = ?, finding_ids_json = ?, evidence_refs_json = ?,
                        updated_at = ?
                    WHERE cycle_id = ? AND concern = ?
                    """,
                (
                    ReaderStatus.FAIL.value,
                    json.dumps(finding_ids),
                    json.dumps(reader_refs),
                    timestamp,
                    cycle_id,
                    concern.value,
                ),
            )
            self._set_failed(connection, cycle_id)
        return self.store.snapshot(cycle_id)

    def resolve_finding(
        self: Any, finding_id: str, resolution: str, *, actor: str | None = None
    ) -> ReviewSnapshot:
        finding_id = require_text(finding_id, "finding id")
        resolution = require_text(resolution, "resolution", 1_000)
        if actor is not None:
            actor = require_text(actor, "resolution actor", 100)
        with self.store.transaction() as connection:
            finding = connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if finding is None:
                raise ReviewError(f"Unknown review finding: {finding_id}")
            timestamp = current_timestamp()
            connection.execute(
                """
                    UPDATE review_findings
                    SET status = ?, resolution = ?, resolution_actor = ?,
                        resolution_at = ?, updated_at = ?
                    WHERE finding_id = ?
                    """,
                (
                    FindingStatus.RESOLVED.value,
                    resolution,
                    actor,
                    timestamp,
                    timestamp,
                    finding_id,
                ),
            )
            current = self.store.current_cycle_row(
                connection, str(finding["pull_request_id"])
            )
            if current is None:
                raise ReviewError("No current review cycle exists for finding")
            cycle_id = str(current["cycle_id"])
        return self.store.snapshot(cycle_id)
