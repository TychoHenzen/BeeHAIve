from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .finding_publication_state import FindingPublicationState

if TYPE_CHECKING:
    from .finding_publication_channel import FindingPublicationChannel
    from .finding_status import FindingStatus
    from .review_concern import ReviewConcern


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    finding_id: str
    pull_request_id: str
    cycle_id: str
    concern: ReviewConcern
    summary: str
    status: FindingStatus
    resolution: str | None
    created_at: str
    updated_at: str
    evidence_refs: tuple[str, ...] = ()
    head_sha: str = ""
    fingerprint: str = ""
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    duplicate_target: str | None = None
    first_seen_cycle_id: str = ""
    stale: bool = False
    resolution_actor: str | None = None
    resolution_at: str | None = None
    publication_state: FindingPublicationState = FindingPublicationState.UNPUBLISHED
    publication_channel: FindingPublicationChannel | None = None
    remote_id: str | None = None
    remote_url: str | None = None
    publication_attempts: int = 0
    publication_retry_evidence: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "finding_id": self.finding_id,
            "pull_request_id": self.pull_request_id,
            "cycle_id": self.cycle_id,
            "concern": self.concern.value,
            "summary": self.summary,
            "status": self.status.value,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "head_sha": self.head_sha,
            "fingerprint": self.fingerprint,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "duplicate_target": self.duplicate_target,
            "first_seen_cycle_id": self.first_seen_cycle_id,
            "stale": self.stale,
            "resolution_actor": self.resolution_actor,
            "resolution_at": self.resolution_at,
            "publication": {
                "state": self.publication_state.value,
                "channel": (
                    None
                    if self.publication_channel is None
                    else self.publication_channel.value
                ),
                "remote_id": self.remote_id,
                "remote_url": self.remote_url,
                "attempts": self.publication_attempts,
                "retry_evidence": self.publication_retry_evidence,
            },
        }
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result
