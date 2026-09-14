from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import quote

from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.pbi_relations import (
    MAX_PBI_RELATION_GRAPH_ISSUES,
    PbiRelationIssue,
    PbiRelationProviderError,
    PbiRelationValidationError,
)


class PbiRelationsLookupMixin:
    @staticmethod
    def _pbi_relation_connection(
        connection_value: object,
    ) -> tuple[list[object], bool, str | None]:
        connection = _mapping(connection_value)
        nodes = connection.get("nodes")
        page_info = _mapping(connection.get("pageInfo"))
        has_next = page_info.get("hasNextPage")
        cursor = page_info.get("endCursor")
        if not isinstance(nodes, list) or type(has_next) is not bool:
            raise PbiRelationProviderError("github_connection_incomplete")
        if has_next and (not isinstance(cursor, str) or not cursor):
            raise PbiRelationProviderError("github_cursor_missing")
        return (
            cast(list[object], nodes),
            has_next,
            cursor if isinstance(cursor, str) else None,
        )

    @staticmethod
    def _pbi_relation_project_item(
        project_items: Mapping[str, list[dict[str, object]]],
        node_id: str,
        repository: str,
        *,
        expected_item_id: str | None = None,
    ) -> dict[str, object]:
        matches = project_items.get(node_id, [])
        if len(matches) != 1:
            raise PbiRelationValidationError(
                "Issue must appear exactly once in the configured Project",
                code="project_item_unavailable",
                status_code=409,
            )
        item = matches[0]
        if str(item.get("repository", "")).casefold() != repository.casefold():
            raise PbiRelationValidationError(
                "Issue is in another repository", code="cross_repository_issue"
            )
        if expected_item_id is not None and item.get("id") != expected_item_id:
            raise PbiRelationValidationError(
                "Created child Project item does not match the live item",
                code="project_item_conflict",
                status_code=409,
            )
        return item

    def _pbi_relation_issue_state(
        self: Any, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, Mapping[str, Any]]:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}"
        )
        status, payload = self._rest_request("GET", path)
        if status != 200:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))
        issue = self._pbi_relation_issue(payload)
        if issue.number != issue_number:
            raise PbiRelationProviderError("issue_identity_conflict")
        return issue, payload

    @staticmethod
    def _pbi_relation_issue(payload: Mapping[str, Any]) -> PbiRelationIssue:
        issue_id = payload.get("id")
        node_id = payload.get("node_id")
        number = payload.get("number")
        url = payload.get("html_url") or payload.get("url")
        title = payload.get("title")
        state = payload.get("state")
        state_reason = payload.get("state_reason")
        if (
            type(issue_id) is not int
            or issue_id <= 0
            or not isinstance(node_id, str)
            or not node_id
            or type(number) is not int
            or number <= 0
            or not isinstance(url, str)
            or not url
            or not isinstance(title, str)
            or not isinstance(state, str)
            or (state_reason is not None and not isinstance(state_reason, str))
        ):
            raise PbiRelationProviderError("issue_response_incomplete")
        return PbiRelationIssue(
            issue_id,
            node_id,
            number,
            url,
            title,
            state.upper(),
            state_reason,
        )

    def _pbi_relation_parent_number(
        self: Any, repository: str, issue_number: int
    ) -> int | None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}/parent"
        )
        status, payload = self._rest_request("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))
        return self._pbi_relation_issue(payload).number

    def _pbi_relation_check_parent_chain(
        self: Any, repository: str, parent_issue_number: int, child_numbers: set[int]
    ) -> None:
        current = parent_issue_number
        visited = {current}
        for _ in range(100):
            ancestor = self._pbi_relation_parent_number(repository, current)
            if ancestor is None:
                return
            if ancestor in child_numbers:
                raise PbiRelationValidationError(
                    "Parent-child declarations would create a cycle",
                    code="sub_issue_cycle",
                    status_code=409,
                )
            if ancestor in visited:
                raise PbiRelationProviderError("sub_issue_graph_inconsistent")
            visited.add(ancestor)
            current = ancestor
        raise PbiRelationProviderError("sub_issue_graph_too_deep")

    @staticmethod
    def _pbi_relation_status_code(status: int) -> str:
        if status in {401, 403}:
            return "permission_denied"
        if status in {404, 410}:
            return "issue_unavailable"
        return "github_request_failed"

    def _pbi_relation_list_issues(
        self: Any, repository: str, issue_number: int, relation: str
    ) -> tuple[PbiRelationIssue, ...]:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}/"
        )
        path += {
            "sub_issues": "sub_issues",
            "blocked_by": "dependencies/blocked_by",
            "blocking": "dependencies/blocking",
        }[relation]
        results: list[PbiRelationIssue] = []
        page = 1
        while True:
            status, payload = self._rest_list_request(
                "GET", f"{path}?per_page=100&page={page}"
            )
            if status != 200:
                raise PbiRelationProviderError(self._pbi_relation_status_code(status))
            if (
                len(payload) > 100
                or len(results) + len(payload) > MAX_PBI_RELATION_GRAPH_ISSUES
            ):
                raise PbiRelationProviderError("relation_set_too_large")
            for raw_issue in payload:
                if not isinstance(raw_issue, Mapping):
                    raise PbiRelationProviderError("relation_response_incomplete")
                results.append(
                    self._pbi_relation_issue(cast(Mapping[str, Any], raw_issue))
                )
            if len(payload) < 100:
                return tuple(results)
            page += 1

    def _rest_list_request(
        self: Any, method: str, path: str
    ) -> tuple[int, list[object]]:
        request_rest = getattr(self._client, "request_rest", None)
        if not callable(request_rest):
            raise PbiRelationProviderError("rest_api_unavailable")
        rest_call = cast(
            Callable[
                [str, str, Mapping[str, object] | None],
                tuple[int, Mapping[str, Any] | list[Any]],
            ],
            request_rest,
        )
        status, payload = rest_call(method, path, None)
        if not isinstance(payload, list):
            raise PbiRelationProviderError("github_list_response_invalid")
        return status, cast(list[object], payload)


__all__ = ["PbiRelationsLookupMixin"]
