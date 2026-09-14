from __future__ import annotations

from typing import Any
from urllib.parse import quote

from beehaiive.pbi_relations import PbiRelationIssue, PbiRelationProviderError


class PbiRelationsMutationMixin:
    def list_pbi_sub_issues(
        self: Any, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(
            repository, parent_issue_number, "sub_issues"
        )

    def get_pbi_parent_issue_number(
        self: Any, repository: str, child_issue_number: int
    ) -> int | None:
        return self._pbi_relation_parent_number(repository, child_issue_number)

    def list_pbi_blocked_by(
        self: Any, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(repository, issue_number, "blocked_by")

    def list_pbi_blocking(
        self: Any, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(repository, issue_number, "blocking")

    def add_pbi_sub_issue(
        self: Any, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{parent_issue_number}/sub_issues"
        )
        status, _ = self._rest_request("POST", path, {"sub_issue_id": child_issue_id})
        if status != 201:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))

    def add_pbi_dependency(
        self: Any, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{blocked_issue_number}/dependencies/blocked_by"
        )
        status, _ = self._rest_request("POST", path, {"issue_id": blocker_issue_id})
        if status != 201:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))


__all__ = ["PbiRelationsMutationMixin"]
