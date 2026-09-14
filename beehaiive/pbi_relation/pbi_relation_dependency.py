from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiRelationDependency"]


@dataclass(frozen=True, slots=True)
class PbiRelationDependency:
    blocked_issue_number: int
    blocked_by_issue_number: int

    def as_dict(self) -> dict[str, int]:
        return {
            "blocked_issue_number": self.blocked_issue_number,
            "blocked_by_issue_number": self.blocked_by_issue_number,
        }
