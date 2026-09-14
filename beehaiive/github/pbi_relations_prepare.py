from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.pbi_relations import (
    PBI_RELATION_PROJECT_QUERY as PBI_RELATION_PROJECT_QUERY,
)
from beehaiive.pbi_refinement_mutation import has_refined_pbi_sections
from beehaiive.pbi_relations import (
    PbiRelationIssue,
    PbiRelationProviderError,
    PbiRelationRequest,
    PbiRelationScopeError,
    PbiRelationSnapshot,
    PbiRelationValidationError,
)


class PbiRelationsPrepareMixin:
    def prepare_pbi_relations(
        self: Any, request: PbiRelationRequest
    ) -> PbiRelationSnapshot:
        request.validate()
        if request.project_id != self.project_id:
            raise PbiRelationScopeError()
        owner, name = self._repository_parts(request.repository)
        repository = f"{owner}/{name}"
        if repository.casefold() != request.repository.casefold():
            raise PbiRelationValidationError(
                "Repository identity is not canonical", code="invalid_repository"
            )

        linked_repositories, project_items = self._pbi_relation_project_state()
        if repository.casefold() not in linked_repositories:
            raise PbiRelationScopeError(
                "Repository is not linked to the configured Project"
            )

        parent, parent_payload = self._pbi_relation_issue_state(
            repository, request.parent_issue_number
        )
        if parent.state != "OPEN":
            raise PbiRelationValidationError(
                "Parent PBI must be open", code="parent_not_open", status_code=409
            )
        body = parent_payload.get("body")
        if not isinstance(body, str):
            raise PbiRelationProviderError("parent_body_unavailable")
        if not has_refined_pbi_sections(body):
            raise PbiRelationValidationError(
                "Parent PBI does not have the required refined sections",
                code="parent_not_refined",
                status_code=409,
            )

        parent_item = self._pbi_relation_project_item(
            project_items, parent.node_id, repository
        )
        if str(parent_item.get("status", "")).casefold() not in {
            "todo",
            "in progress",
        }:
            raise PbiRelationValidationError(
                "Parent PBI must be in Todo or In Progress",
                code="parent_not_refined",
                status_code=409,
            )

        children: list[PbiRelationIssue] = []
        for reference in request.children:
            issue, _ = self._pbi_relation_issue_state(repository, reference.number)
            if (
                issue.node_id != reference.node_id
                or issue.number != reference.number
                or issue.url != reference.url
            ):
                raise PbiRelationValidationError(
                    "Created child reference does not match the live issue",
                    code="child_identity_conflict",
                    status_code=409,
                )
            if issue.state != "OPEN":
                raise PbiRelationValidationError(
                    "Created child must remain open",
                    code="child_not_open",
                    status_code=409,
                )
            self._pbi_relation_project_item(
                project_items,
                issue.node_id,
                repository,
                expected_item_id=reference.project_item_id,
            )
            children.append(issue)

        child_numbers = {issue.number for issue in children}
        parent_by_child: dict[int, int | None] = {}
        for child in children:
            existing_parent = self._pbi_relation_parent_number(repository, child.number)
            if existing_parent not in (None, parent.number):
                raise PbiRelationValidationError(
                    "Child already belongs to another parent",
                    code="child_has_other_parent",
                    status_code=409,
                )
            parent_by_child[child.number] = existing_parent
        self._pbi_relation_check_parent_chain(repository, parent.number, child_numbers)
        parent_sub_issues = self.list_pbi_sub_issues(repository, parent.number)
        return PbiRelationSnapshot(
            parent,
            tuple(children),
            parent_sub_issues,
            parent_by_child,
        )

    def _pbi_relation_project_state(
        self: Any,
    ) -> tuple[set[str], dict[str, list[dict[str, object]]]]:
        repository_cursor: str | None = None
        item_cursor: str | None = None
        seen_repository_cursors: set[str] = set()
        seen_item_cursors: set[str] = set()
        project_id: str | None = None
        status_field_id: str | None = None
        linked_repositories: set[str] = set()
        project_items: dict[str, list[dict[str, object]]] = {}
        while True:
            data = self._client.execute(
                _owner_query(PBI_RELATION_PROJECT_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "repositoryCursor": repository_cursor,
                    "itemCursor": item_cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiRelationProviderError("project_unavailable")
            if project_id is not None and project_id != current_project_id:
                raise PbiRelationProviderError("project_identity_changed")
            project_id = current_project_id

            fields_value = project.get("fields")
            if not isinstance(fields_value, Mapping):
                raise PbiRelationProviderError("project_fields_incomplete")
            fields = cast(Mapping[str, Any], fields_value)
            field_nodes = fields.get("nodes")
            if not isinstance(field_nodes, list):
                raise PbiRelationProviderError("project_fields_incomplete")
            status_fields: list[Mapping[str, Any]] = []
            for raw_field in cast(list[object], field_nodes):
                if not isinstance(raw_field, Mapping):
                    continue
                field = cast(Mapping[str, Any], raw_field)
                field_name = field.get("name")
                if isinstance(field_name, str) and field_name.casefold() == "status":
                    status_fields.append(field)
            if len(status_fields) != 1:
                raise PbiRelationProviderError("project_status_unavailable")
            raw_status_field_id = status_fields[0].get("id")
            if not isinstance(raw_status_field_id, str) or not raw_status_field_id:
                raise PbiRelationProviderError("project_status_unavailable")
            if status_field_id is not None and status_field_id != raw_status_field_id:
                raise PbiRelationProviderError("project_status_changed")
            status_field_id = raw_status_field_id

            repository_nodes, has_repository_page, next_repository_cursor = (
                self._pbi_relation_connection(project.get("repositories"))
            )
            for raw_repository in repository_nodes:
                repository_name = _mapping(raw_repository).get("nameWithOwner")
                if not isinstance(repository_name, str) or not repository_name:
                    raise PbiRelationProviderError("project_repository_incomplete")
                linked_repositories.add(repository_name.casefold())

            item_nodes, has_item_page, next_item_cursor = self._pbi_relation_connection(
                project.get("items")
            )
            for raw_item in item_nodes:
                item = _mapping(raw_item)
                raw_content = item.get("content")
                if raw_content is None:
                    continue
                content = _mapping(raw_content)
                if content.get("__typename") != "Issue":
                    continue
                node_id = content.get("id")
                number = content.get("number")
                content_repository = _mapping(content.get("repository")).get(
                    "nameWithOwner"
                )
                item_id = item.get("id")
                if (
                    not isinstance(node_id, str)
                    or type(number) is not int
                    or not isinstance(content_repository, str)
                    or not isinstance(item_id, str)
                ):
                    raise PbiRelationProviderError("project_item_incomplete")
                field_values = item.get("fieldValues")
                values, has_more_values, _ = self._pbi_relation_connection(field_values)
                if has_more_values:
                    raise PbiRelationProviderError("project_item_fields_incomplete")
                statuses: list[str] = []
                for raw_value in values:
                    value = _mapping(raw_value)
                    raw_field = value.get("field")
                    if raw_field is None:
                        continue
                    field = _mapping(raw_field)
                    if field.get("id") == status_field_id:
                        name_value = value.get("name")
                        if not isinstance(name_value, str) or not name_value:
                            raise PbiRelationProviderError(
                                "project_item_status_incomplete"
                            )
                        statuses.append(name_value)
                if len(statuses) > 1:
                    raise PbiRelationProviderError("project_item_status_ambiguous")
                project_items.setdefault(node_id, []).append(
                    {
                        "id": item_id,
                        "number": number,
                        "repository": content_repository,
                        "status": statuses[0] if statuses else None,
                    }
                )

            repository_cursor = next_repository_cursor
            item_cursor = next_item_cursor
            if has_repository_page and (
                not repository_cursor or repository_cursor in seen_repository_cursors
            ):
                raise PbiRelationProviderError(
                    "project_repository_pagination_incomplete"
                )
            if has_item_page and (not item_cursor or item_cursor in seen_item_cursors):
                raise PbiRelationProviderError("project_item_pagination_incomplete")
            if repository_cursor:
                seen_repository_cursors.add(repository_cursor)
            if item_cursor:
                seen_item_cursors.add(item_cursor)
            if not has_repository_page and not has_item_page:
                return linked_repositories, project_items


__all__ = ["PbiRelationsPrepareMixin"]
