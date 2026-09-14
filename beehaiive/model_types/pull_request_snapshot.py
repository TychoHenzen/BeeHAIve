from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PullRequestSnapshot"]


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Current pull-request identity and mergeability evidence."""

    repository: str
    number: int
    pull_request_id: str
    url: str
    state: str
    merged: bool
    source_branch: str | None
    source_head: str | None
    target_branch: str | None
    target_head: str | None
    mergeable: str | None
    merge_state: str | None
    evidence_error: str | None = None
    is_draft: bool | None = None
    merge_commit_oid: str | None = None
    source_repository: str | None = None

    @property
    def conflict_state(self) -> str:
        """Return conflicting, clean, or unknown without guessing."""

        if self.evidence_error is not None:
            return "unknown"
        if (
            self.state != "OPEN"
            or self.merged
            or not self.source_branch
            or not self.source_head
            or not self.target_branch
            or not self.target_head
        ):
            return "unknown"
        if self.mergeable == "CONFLICTING" and self.merge_state == "DIRTY":
            return "conflicting"
        if self.mergeable == "MERGEABLE" and self.merge_state in {
            "BEHIND",
            "BLOCKED",
            "CLEAN",
            "HAS_HOOKS",
            "UNSTABLE",
        }:
            return "clean"
        return "unknown"

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "number": self.number,
            "pull_request_id": self.pull_request_id,
            "url": self.url,
            "state": self.state,
            "merged": self.merged,
            "source_branch": self.source_branch,
            "source_head": self.source_head,
            "target_branch": self.target_branch,
            "target_head": self.target_head,
            "mergeable": self.mergeable,
            "merge_state": self.merge_state,
            "conflict_state": self.conflict_state,
            "evidence_error": self.evidence_error,
        }
