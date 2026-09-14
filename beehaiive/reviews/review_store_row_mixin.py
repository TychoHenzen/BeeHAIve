from __future__ import annotations

import sqlite3
from typing import Any

from .finding_publication_channel import FindingPublicationChannel
from .finding_publication_state import FindingPublicationState
from .finding_status import FindingStatus
from .helpers import json_list, json_object
from .reader_result import ReaderResult
from .reader_status import ReaderStatus
from .review_concern import ReviewConcern
from .review_cycle import ReviewCycle
from .review_cycle_status import ReviewCycleStatus
from .review_finding import ReviewFinding
from .review_repair_attempt import ReviewRepairAttempt
from .review_repair_status import ReviewRepairStatus
from .review_repair_transition_status import ReviewRepairTransitionStatus


class ReviewStoreRowMixin:
    def _cycle_from_row(self: Any, row: sqlite3.Row) -> ReviewCycle:
        return ReviewCycle(
            cycle_id=str(row["cycle_id"]),
            pull_request_id=str(row["pull_request_id"]),
            head_sha=str(row["head_sha"]),
            cycle_number=int(row["cycle_number"]),
            status=ReviewCycleStatus(str(row["status"])),
            human_approval=bool(row["human_approval"]),
            required_action=row["required_action"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            approval_actor=row["approval_actor"],
            approval_reason=row["approval_reason"],
            approval_at=row["approval_at"],
            github_evidence_json=row["github_evidence_json"],
        )

    def _reader_from_row(self: Any, row: sqlite3.Row) -> ReaderResult:
        return ReaderResult(
            cycle_id=str(row["cycle_id"]),
            concern=ReviewConcern(str(row["concern"])),
            status=ReaderStatus(str(row["status"])),
            finding_ids=json_list(str(row["finding_ids_json"])),
            reader=str(row["reader"]),
            updated_at=str(row["updated_at"]),
            evidence_refs=json_list(str(row["evidence_refs_json"])),
        )

    def _finding_from_row(self: Any, row: sqlite3.Row) -> ReviewFinding:
        retry_evidence = row["publication_retry_evidence_json"]
        return ReviewFinding(
            finding_id=str(row["finding_id"]),
            pull_request_id=str(row["pull_request_id"]),
            cycle_id=str(row["cycle_id"]),
            concern=ReviewConcern(str(row["concern"])),
            summary=str(row["summary"]),
            status=FindingStatus(str(row["status"])),
            resolution=row["resolution"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            evidence_refs=json_list(str(row["evidence_refs_json"])),
            head_sha=str(row["head_sha"] or ""),
            fingerprint=str(row["fingerprint"] or ""),
            file_path=row["file_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            duplicate_target=row["duplicate_target"],
            first_seen_cycle_id=str(row["first_seen_cycle_id"] or ""),
            stale=bool(row["stale"]),
            resolution_actor=row["resolution_actor"],
            resolution_at=row["resolution_at"],
            publication_state=FindingPublicationState(str(row["publication_state"])),
            publication_channel=(
                None
                if row["publication_channel"] is None
                else FindingPublicationChannel(str(row["publication_channel"]))
            ),
            remote_id=row["remote_id"],
            remote_url=row["remote_url"],
            publication_attempts=int(row["publication_attempts"]),
            publication_retry_evidence=(
                None if retry_evidence is None else json_object(str(retry_evidence))
            ),
        )

    def repair_attempt_from_row(self: Any, row: sqlite3.Row) -> ReviewRepairAttempt:
        push_evidence = row["push_evidence_json"]
        return ReviewRepairAttempt(
            attempt_id=str(row["attempt_id"]),
            cycle_id=str(row["cycle_id"]),
            pull_request_id=str(row["pull_request_id"]),
            head_sha=str(row["head_sha"]),
            finding_ids=json_list(str(row["finding_ids_json"])),
            actor=str(row["actor"]),
            status=ReviewRepairStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            lease_id=row["lease_id"],
            commit_sha=row["commit_sha"],
            push_evidence=(
                None if push_evidence is None else json_object(str(push_evidence))
            ),
            result=row["result"],
            required_action=row["required_action"],
            cancellation_requested=bool(row["cancellation_requested"]),
            review_transition_status=ReviewRepairTransitionStatus(
                str(row["review_transition_status"])
            ),
            review_transition_cycle_id=row["review_transition_cycle_id"],
            review_transition_required_action=row["review_transition_required_action"],
        )
