from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .finding_status import FindingStatus

if TYPE_CHECKING:
    from .reader_result import ReaderResult
    from .review_cycle import ReviewCycle
    from .review_finding import ReviewFinding
    from .review_repair_attempt import ReviewRepairAttempt


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    cycle: ReviewCycle
    readers: tuple[ReaderResult, ...]
    findings: tuple[ReviewFinding, ...]
    merge_allowed: bool
    repair_attempt: ReviewRepairAttempt | None = None

    def as_dict(self) -> dict[str, object]:
        open_findings = [
            finding.as_dict()
            for finding in self.findings
            if finding.status is FindingStatus.OPEN
        ]
        result: dict[str, object] = {
            "cycle": self.cycle.as_dict(),
            "readers": [reader.as_dict() for reader in self.readers],
            "findings": [finding.as_dict() for finding in self.findings],
            "writer_feedback": open_findings,
            "merge_allowed": self.merge_allowed,
        }
        if self.repair_attempt is not None:
            result["repair"] = {
                "attempt_id": self.repair_attempt.attempt_id,
                "status": self.repair_attempt.status.value,
                "required_action": self.repair_attempt.required_action,
                "review_transition": self.repair_attempt.review_transition_as_dict(),
            }
        return result
