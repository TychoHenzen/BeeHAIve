from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_PROJECT_QUERY as PBI_CREATION_PROJECT_QUERY,
)
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementTarget,
    PbiRefinementUpdateRequest,
)


class PbiRefinementProjectMixin:
    def _pbi_refinement_project_item(
        self: Any,
        request: PbiRefinementUpdateRequest,
        target: PbiRefinementTarget,
        issue: Mapping[str, Any],
    ) -> dict[str, object]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matches: list[dict[str, object]] = []
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_PROJECT_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            if project.get("id") != target.project_node_id:
                raise PbiRefinementMutationError(
                    "Configured Project identity changed",
                    code="project_identity_changed",
                    status_code=409,
                )
            status_fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(status_fields) != 1:
                raise PbiRefinementMutationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                    status_code=409,
                )
            status_field = status_fields[0]
            field_id = status_field.get("id")
            options = status_field.get("options")
            if field_id != target.status_field_id or not isinstance(options, list):
                raise PbiRefinementMutationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                    status_code=409,
                )
            status_options: dict[str, list[tuple[str, str]]] = {
                "backlog": [],
                "todo": [],
            }
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                option_name = option.get("name")
                option_id = option.get("id")
                if (
                    isinstance(option_name, str)
                    and option_name.casefold() in status_options
                    and isinstance(option_id, str)
                ):
                    status_options[option_name.casefold()].append(
                        (option_id, option_name)
                    )
            if (
                len(status_options["backlog"]) != 1
                or status_options["backlog"][0]
                != (target.backlog_option_id, target.backlog_status)
                or len(status_options["todo"]) != 1
                or status_options["todo"][0]
                != (target.todo_option_id, target.todo_status)
            ):
                raise PbiRefinementMutationError(
                    "Configured Project Status options changed",
                    code="project_status_options_changed",
                    status_code=409,
                )

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                raw_content = raw_item.get("content")
                if not isinstance(raw_content, Mapping):
                    continue
                content = cast(Mapping[str, Any], raw_content)
                repository = _mapping(content.get("repository")).get("nameWithOwner")
                if (
                    content.get("__typename") != "Issue"
                    or content.get("number") != request.pbi_number
                    or not isinstance(repository, str)
                    or repository.casefold() != request.repository.casefold()
                    or content.get("id") != issue.get("id")
                ):
                    continue
                values = [
                    value
                    for value in _nodes(raw_item.get("fieldValues"))
                    if _mapping(value.get("field")).get("id") == target.status_field_id
                ]
                if len(values) != 1:
                    raise PbiRefinementMutationError(
                        "PBI Project Status is missing or ambiguous",
                        code="project_item_status_unavailable",
                        status_code=409,
                    )
                value = values[0]
                status = value.get("name")
                option_id = value.get("optionId")
                if not isinstance(status, str) or not isinstance(option_id, str):
                    raise PbiRefinementMutationError(
                        "PBI Project Status is incomplete",
                        code="project_item_status_unavailable",
                        status_code=409,
                    )
                expected_ids = {
                    target.backlog_status: target.backlog_option_id,
                    target.todo_status: target.todo_option_id,
                }
                if expected_ids.get(status) != option_id:
                    raise PbiRefinementMutationError(
                        "PBI has an unknown Project Status option",
                        code="project_item_status_unknown",
                        status_code=409,
                    )
                item_id = raw_item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise PbiRefinementMutationError(
                        "PBI Project item identity is incomplete",
                        code="project_item_identity_unavailable",
                        status_code=409,
                    )
                matches.append({"item_id": item_id, "status": status})
            has_next, cursor = _next_cursor(items, seen_cursors, strict=True)
            if not has_next:
                break

        if len(matches) != 1:
            raise PbiRefinementMutationError(
                "PBI must appear exactly once in the configured Project",
                code="project_membership_unconfirmed",
                status_code=409,
            )
        return matches[0]

    @staticmethod
    def _resolve_pbi_refinement_label(
        labels_by_name: Mapping[str, list[Mapping[str, Any]]],
        name: str,
        *,
        require_description: bool,
    ) -> tuple[str, str]:
        matches = labels_by_name.get(name.casefold(), [])
        if len(matches) != 1:
            raise PbiRefinementMutationError(
                "Requested or attached label is missing or ambiguous",
                code="label_unavailable",
                status_code=422,
            )
        label_id = matches[0].get("id")
        canonical_name = matches[0].get("name")
        description = matches[0].get("description")
        if (
            not isinstance(label_id, str)
            or not label_id
            or not isinstance(canonical_name, str)
            or (
                require_description
                and (not isinstance(description, str) or not description.strip())
            )
        ):
            raise PbiRefinementMutationError(
                "Requested label or its live description is incomplete",
                code="label_metadata_unavailable",
                status_code=422,
            )
        return label_id, canonical_name

    @staticmethod
    def _pbi_refinement_label_names(issue: Mapping[str, Any]) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for label in _nodes(issue.get("labels")):
            name = label.get("name")
            if not isinstance(name, str) or not name:
                raise PbiRefinementMutationError(
                    "GitHub returned an invalid issue label",
                    code="issue_labels_invalid",
                    status_code=409,
                )
            if name.casefold() in seen:
                raise PbiRefinementMutationError(
                    "GitHub returned duplicate issue labels",
                    code="issue_labels_ambiguous",
                    status_code=409,
                )
            seen.add(name.casefold())
            names.append(name)
        return tuple(names)

    @staticmethod
    def _pbi_refinement_sub_issues(
        issue: Mapping[str, Any],
    ) -> tuple[Mapping[str, object], ...]:
        sub_issues: list[Mapping[str, object]] = []
        for item in _nodes(issue.get("subIssues")):
            number = item.get("number")
            title = item.get("title")
            state = item.get("state")
            if (
                type(number) is not int
                or number <= 0
                or not isinstance(title, str)
                or not isinstance(state, str)
            ):
                raise PbiRefinementMutationError(
                    "GitHub returned invalid linked sub-issue data",
                    code="sub_issues_invalid",
                    status_code=409,
                )
            sub_issues.append({"number": number, "title": title, "state": state})
        return tuple(sorted(sub_issues, key=lambda item: cast(int, item["number"])))


__all__ = ["PbiRefinementProjectMixin"]
