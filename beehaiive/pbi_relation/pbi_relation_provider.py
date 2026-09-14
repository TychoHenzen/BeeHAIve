from __future__ import annotations

from typing import Protocol

from .pbi_relation_issue import PbiRelationIssue
from .pbi_relation_request import PbiRelationRequest
from .pbi_relation_snapshot import PbiRelationSnapshot

__all__ = ["PbiRelationProvider"]


class PbiRelationProvider(Protocol):
    def prepare_pbi_relations(
        self, request: PbiRelationRequest
    ) -> PbiRelationSnapshot: ...

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None: ...

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None: ...

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None: ...
