from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .pbi_relation_dependency import PbiRelationDependency
from .pbi_relation_issue import PbiRelationIssue

__all__ = ["PbiRelationResult"]


@dataclass(frozen=True, slots=True)
class PbiRelationResult:
    status: Literal["complete", "incomplete"]
    parent: PbiRelationIssue | None
    parent_issue_number: int
    requested_children: tuple[int, ...]
    preexisting_children: tuple[int, ...]
    confirmed_children: tuple[int, ...]
    pending_children: tuple[int, ...]
    requested_dependencies: tuple[PbiRelationDependency, ...]
    preexisting_dependencies: tuple[PbiRelationDependency, ...]
    confirmed_dependencies: tuple[PbiRelationDependency, ...]
    pending_dependencies: tuple[PbiRelationDependency, ...]
    sub_issue_readback_complete: bool
    dependency_readback_complete: bool
    completed_steps: tuple[str, ...]
    pending_step: str | None = None
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "parent": (
                self.parent.as_dict()
                if self.parent is not None
                else {"number": self.parent_issue_number}
            ),
            "relations": {
                "sub_issues": {
                    "requested": list(self.requested_children),
                    "preexisting": list(self.preexisting_children),
                    "confirmed": [
                        {
                            "parent_issue_number": self.parent_issue_number,
                            "child_issue_number": number,
                        }
                        for number in self.confirmed_children
                    ],
                    "pending": list(self.pending_children),
                    "readback_complete": self.sub_issue_readback_complete,
                },
                "dependencies": {
                    "requested": [
                        edge.as_dict() for edge in self.requested_dependencies
                    ],
                    "preexisting": [
                        edge.as_dict() for edge in self.preexisting_dependencies
                    ],
                    "confirmed": [
                        edge.as_dict() for edge in self.confirmed_dependencies
                    ],
                    "pending": [edge.as_dict() for edge in self.pending_dependencies],
                    "readback_complete": self.dependency_readback_complete,
                },
            },
            "completed_steps": list(self.completed_steps),
            "pending_step": self.pending_step,
            "failure_code": self.failure_code,
        }
