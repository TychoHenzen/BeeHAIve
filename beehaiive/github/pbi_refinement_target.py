from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import (
    _complete_connection as _complete_connection,
)
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.discovery import ISSUE_LABELS_QUERY as ISSUE_LABELS_QUERY
from beehaiive.github.queries.discovery import (
    ISSUE_SUB_ISSUES_QUERY as ISSUE_SUB_ISSUES_QUERY,
)
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_LABELS_QUERY as PBI_CREATION_LABELS_QUERY,
)
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_TARGET_QUERY as PBI_CREATION_TARGET_QUERY,
)
from beehaiive.github.queries.pbi_refinement import (
    PBI_REFINEMENT_ISSUE_QUERY as PBI_REFINEMENT_ISSUE_QUERY,
)
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementTarget,
    PbiRefinementUpdateRequest,
)


class PbiRefinementTargetMixin:
    def _prepare_pbi_refinement_target(
        self: Any, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementTarget:
        if request.project_id != self.project_id:
            raise PbiRefinementMutationError(
                "Project is not authorized",
                code="project_not_authorized",
                status_code=403,
            )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        project_node_id: str | None = None
        repository_names: set[str] = set()
        status_field_id: str | None = None
        status_options: dict[str, tuple[str, str]] = {}
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_TARGET_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiRefinementMutationError(
                    "Configured Project is unavailable",
                    code="project_unavailable",
                    status_code=403,
                )
            if project_node_id is not None and current_project_id != project_node_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_node_id = current_project_id

            repositories = _mapping(project.get("repositories"))
            for repository in _nodes(repositories):
                name = repository.get("nameWithOwner")
                if isinstance(name, str):
                    repository_names.add(name.casefold())

            fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(fields) != 1:
                raise PbiRefinementMutationError(
                    "Configured Project must have one Status field",
                    code="project_status_unavailable",
                )
            field_id = fields[0].get("id")
            options = fields[0].get("options")
            if not isinstance(field_id, str) or not isinstance(options, list):
                raise PbiRefinementMutationError(
                    "Configured Project Status field is incomplete",
                    code="project_status_unavailable",
                )
            if status_field_id is not None and status_field_id != field_id:
                raise ProviderError("GitHub returned conflicting Status fields")
            status_field_id = field_id

            found_options: dict[str, list[tuple[str, str]]] = {
                "backlog": [],
                "todo": [],
            }
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                name = option.get("name")
                option_id = option.get("id")
                if (
                    isinstance(name, str)
                    and name.casefold() in found_options
                    and isinstance(option_id, str)
                    and option_id
                ):
                    found_options[name.casefold()].append((option_id, name))
            if any(len(matches) != 1 for matches in found_options.values()):
                raise PbiRefinementMutationError(
                    "Configured Project must have unique Backlog and Todo options",
                    code="project_status_options_unavailable",
                )
            page_options = {name: matches[0] for name, matches in found_options.items()}
            if status_options and status_options != page_options:
                raise ProviderError("GitHub returned conflicting Status options")
            status_options = page_options

            has_next, cursor = _next_cursor(repositories, seen_cursors)
            if not has_next:
                break

        if request.repository.casefold() not in repository_names:
            raise PbiRefinementMutationError(
                "Repository is not linked to the configured Project",
                code="repository_not_linked",
                status_code=403,
            )
        backlog_id, backlog_name = status_options["backlog"]
        todo_id, todo_name = status_options["todo"]
        return PbiRefinementTarget(
            project_node_id=project_node_id,
            status_field_id=status_field_id,
            backlog_option_id=backlog_id,
            backlog_status=backlog_name,
            todo_option_id=todo_id,
            todo_status=todo_name,
        )

    def _pbi_refinement_repository_labels(
        self: Any, request: PbiRefinementUpdateRequest
    ) -> dict[str, list[Mapping[str, Any]]]:
        owner, name = self._repository_parts(request.repository)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        repository_id: str | None = None
        labels: dict[str, list[Mapping[str, Any]]] = {}
        while True:
            data = self._client.execute(
                PBI_CREATION_LABELS_QUERY,
                {"owner": owner, "name": name, "cursor": cursor},
            )
            repository = _mapping(data.get("repository"))
            current_id = repository.get("id")
            current_name = repository.get("nameWithOwner")
            if (
                not isinstance(current_id, str)
                or not current_id
                or not isinstance(current_name, str)
                or current_name.casefold() != request.repository.casefold()
            ):
                raise PbiRefinementMutationError(
                    "Repository is not available",
                    code="repository_unavailable",
                    status_code=403,
                )
            if repository_id is not None and repository_id != current_id:
                raise ProviderError("GitHub returned conflicting repository identities")
            repository_id = current_id
            connection = _mapping(repository.get("labels"))
            for label in _nodes(connection):
                label_name = label.get("name")
                if isinstance(label_name, str):
                    labels.setdefault(label_name.casefold(), []).append(label)
            has_next, cursor = _next_cursor(connection, seen_cursors)
            if not has_next:
                break
        return labels

    def _pbi_refinement_issue_state(
        self: Any, request: PbiRefinementUpdateRequest
    ) -> Mapping[str, Any]:
        owner, name = self._repository_parts(request.repository)
        variables = {"owner": owner, "name": name, "number": request.pbi_number}
        data = self._client.execute(PBI_REFINEMENT_ISSUE_QUERY, variables)
        issue = _mapping(_mapping(data.get("repository")).get("issue"))
        if not issue:
            raise PbiRefinementMutationError(
                "Issue was not found in the configured repository",
                code="issue_not_found",
                status_code=404,
            )
        if issue.get("number") != request.pbi_number:
            raise PbiRefinementMutationError(
                "GitHub returned a different issue",
                code="issue_identity_mismatch",
                status_code=409,
            )
        completed = dict(issue)
        completed["labels"] = _complete_connection(
            self._client,
            issue.get("labels"),
            ISSUE_LABELS_QUERY,
            variables,
            ("repository", "issue", "labels"),
            strict=True,
        )
        completed["subIssues"] = _complete_connection(
            self._client,
            issue.get("subIssues"),
            ISSUE_SUB_ISSUES_QUERY,
            variables,
            ("repository", "issue", "subIssues"),
            strict=True,
        )
        return completed

    @classmethod
    def _pbi_refinement_issue_matches(
        cls: Any,
        issue: Mapping[str, Any],
        expected_body: str,
        expected_labels: set[str],
        expected_sub_issues: tuple[Mapping[str, object], ...],
    ) -> bool:
        body = issue.get("body")
        if body is None:
            body = ""
        try:
            return (
                issue.get("state") == "OPEN"
                and body == expected_body
                and {name.casefold() for name in cls._pbi_refinement_label_names(issue)}
                == expected_labels
                and cls._pbi_refinement_sub_issues(issue) == expected_sub_issues
            )
        except PbiRefinementMutationError:
            return False


__all__ = ["PbiRefinementTargetMixin"]
