from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reader_status import ReaderStatus
    from .review_concern import ReviewConcern


@dataclass(frozen=True, slots=True)
class ReaderResult:
    cycle_id: str
    concern: ReviewConcern
    status: ReaderStatus
    finding_ids: tuple[str, ...]
    reader: str
    updated_at: str
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "cycle_id": self.cycle_id,
            "concern": self.concern.value,
            "status": self.status.value,
            "finding_ids": list(self.finding_ids),
            "reader": self.reader,
            "updated_at": self.updated_at,
        }
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result
