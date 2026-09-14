from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_ISSUE_QUERY as PBI_CREATION_ISSUE_QUERY,
)
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_PROJECT_QUERY as PBI_CREATION_PROJECT_QUERY,
)
from beehaiive.pbi_creation import (
    PbiCreationError,
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationScopeError,
    PbiCreationTarget,
    PbiCreationValidationError,
)


class PbiCreationStateMixin:
    def _pbi_creation_issue_state(
        self: Any, owner: str, name: str, issue_number: int
    ) -> Mapping[str, Any]:
        data = self._client.execute(
            PBI_CREATION_ISSUE_QUERY,
            {"owner": owner, "name": name, "number": issue_number},
        )
        issue = _mapping(_mapping(data.get("repository")).get("issue"))
        if not issue:
            raise PbiCreationError(
                "Created issue could not be read back",
                code="issue_readback_failed",
                status_code=502,
            )
        return issue

    @staticmethod
    def _validate_pbi_creation_issue(
        request: PbiCreationRequest,
        progress: PbiCreationProgress,
        issue: Mapping[str, Any],
    ) -> None:
        if (
            issue.get("id") != progress.issue_id
            or issue.get("number") != progress.issue_number
            or issue.get("title") != request.title
            or issue.get("body") != request.body
            or issue.get("state") != "OPEN"
        ):
            raise PbiCreationError(
                "Issue identity or caller content did not read back",
                code="issue_identity_or_content_conflict",
                status_code=409,
            )

    def _pbi_creation_project_item(
        self: Any,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        issue_id: str,
        issue_number: int,
    ) -> dict[str, object] | None:
        cursor: str | None = None
        matching_items: list[dict[str, object]] = []
        project_id: str | None = None
        status_field_id: str | None = None
        backlog_option_id: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_PROJECT_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if current_project_id != target.project_node_id:
                raise PbiCreationScopeError("Configured Project identity changed")
            if project_id is not None and current_project_id != project_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_id = (
                current_project_id if isinstance(current_project_id, str) else None
            )

            fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(fields) != 1:
                raise PbiCreationValidationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                )
            field_id = fields[0].get("id")
            options = fields[0].get("options")
            if not isinstance(field_id, str) or field_id != target.status_field_id:
                raise PbiCreationValidationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                )
            if not isinstance(options, list):
                raise PbiCreationValidationError(
                    "Configured Project Status options are incomplete",
                    code="project_status_unavailable",
                )
            backlog_options: list[Mapping[str, object]] = []
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                option_name = option.get("name")
                if isinstance(option_name, str) and option_name.casefold() == "backlog":
                    backlog_options.append(option)
            if len(backlog_options) != 1:
                raise PbiCreationValidationError(
                    "Configured Project Backlog option changed",
                    code="project_backlog_unavailable",
                )
            option_id = backlog_options[0].get("id")
            if not isinstance(option_id, str) or option_id != target.backlog_option_id:
                raise PbiCreationValidationError(
                    "Configured Project Backlog option changed",
                    code="project_backlog_unavailable",
                )
            status_field_id = field_id
            backlog_option_id = option_id

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                raw_content: object = raw_item.get("content")
                if not isinstance(raw_content, Mapping):
                    continue
                content = cast(Mapping[str, Any], raw_content)
                if content.get("__typename") != "Issue":
                    continue
                repository_value: object = content.get("repository")
                repository_name = (
                    cast(Mapping[str, Any], repository_value).get("nameWithOwner")
                    if isinstance(repository_value, Mapping)
                    else None
                )
                matches = content.get("id") == issue_id or (
                    content.get("number") == issue_number
                    and isinstance(repository_name, str)
                    and repository_name.casefold() == request.repository.casefold()
                )
                if not matches:
                    continue
                status_values: list[Mapping[str, Any]] = []
                for value in _nodes(raw_item.get("fieldValues")):
                    raw_field: object = value.get("field")
                    if not isinstance(raw_field, Mapping):
                        continue
                    field = cast(Mapping[str, Any], raw_field)
                    if field.get("id") == field_id:
                        status_values.append(value)
                if len(status_values) > 1:
                    raise ProviderError("Project item has conflicting Status values")
                raw_status = status_values[0].get("name") if status_values else None
                status = raw_status if isinstance(raw_status, str) else None
                matching_items.append(
                    {
                        "item_id": raw_item.get("id"),
                        "status": status,
                    }
                )
            has_next, cursor = _next_cursor(items)
            if not has_next:
                break

        if (
            status_field_id != target.status_field_id
            or backlog_option_id != target.backlog_option_id
        ):
            raise ProviderError("Project creation configuration changed")
        if len(matching_items) > 1:
            raise PbiCreationError(
                "Issue appears more than once in the configured Project",
                code="duplicate_project_items",
                status_code=409,
            )
        return matching_items[0] if matching_items else None


__all__ = ["PbiCreationStateMixin"]
