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
    PBI_CREATION_LABELS_QUERY as PBI_CREATION_LABELS_QUERY,
)
from beehaiive.github.queries.pbi_creation import (
    PBI_CREATION_TARGET_QUERY as PBI_CREATION_TARGET_QUERY,
)
from beehaiive.pbi_creation import (
    PbiCreationRequest,
    PbiCreationScopeError,
    PbiCreationTarget,
    PbiCreationValidationError,
)


class PbiCreationPrepareMixin:
    def prepare_pbi_creation(
        self: Any, request: PbiCreationRequest
    ) -> PbiCreationTarget:
        """Validate the live Project, linked repository, labels, and Backlog option."""

        if request.project_id != self.project_id:
            raise PbiCreationScopeError("Project is not authorized")
        repository_owner, repository_name = self._repository_parts(request.repository)
        cursor: str | None = None
        project_node_id: str | None = None
        linked_repositories: set[str] = set()
        status_field_id: str | None = None
        backlog_option_id: str | None = None
        backlog_status: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_TARGET_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiCreationScopeError("Configured Project is unavailable")
            if project_node_id is not None and current_project_id != project_node_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_node_id = current_project_id

            repository_connection = _mapping(project.get("repositories"))
            for raw_repository in _nodes(repository_connection):
                name_with_owner = raw_repository.get("nameWithOwner")
                if isinstance(name_with_owner, str):
                    linked_repositories.add(name_with_owner.casefold())

            status_fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(status_fields) != 1:
                raise PbiCreationValidationError(
                    "Configured Project must have one Status field",
                    code="project_status_unavailable",
                )
            status_field = status_fields[0]
            raw_status_field_id = status_field.get("id")
            options = status_field.get("options")
            if not isinstance(raw_status_field_id, str) or not isinstance(
                options, list
            ):
                raise PbiCreationValidationError(
                    "Configured Project Status field is incomplete",
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
                    "Configured Project must have one Backlog Status option",
                    code="project_backlog_unavailable",
                )
            raw_backlog_option_id = backlog_options[0].get("id")
            raw_backlog_status = backlog_options[0].get("name")
            if not isinstance(raw_backlog_option_id, str) or not isinstance(
                raw_backlog_status, str
            ):
                raise PbiCreationValidationError(
                    "Configured Project Backlog option is incomplete",
                    code="project_backlog_unavailable",
                )
            if status_field_id is not None and status_field_id != raw_status_field_id:
                raise ProviderError("GitHub returned conflicting Status fields")
            if (
                backlog_option_id is not None
                and backlog_option_id != raw_backlog_option_id
            ):
                raise ProviderError("GitHub returned conflicting Backlog options")
            status_field_id = raw_status_field_id
            backlog_option_id = raw_backlog_option_id
            backlog_status = raw_backlog_status

            has_next, cursor = _next_cursor(repository_connection)
            if not has_next:
                break

        if request.repository.casefold() not in linked_repositories:
            raise PbiCreationScopeError(
                "Repository is not linked to the configured Project"
            )
        label_cursor: str | None = None
        repository_node_id: str | None = None
        labels_by_name: dict[str, str] = {}
        while True:
            label_data = self._client.execute(
                PBI_CREATION_LABELS_QUERY,
                {
                    "owner": repository_owner,
                    "name": repository_name,
                    "cursor": label_cursor,
                },
            )
            repository = _mapping(label_data.get("repository"))
            current_repository_id = repository.get("id")
            current_repository_name = repository.get("nameWithOwner")
            if (
                not isinstance(current_repository_id, str)
                or not current_repository_id
                or not isinstance(current_repository_name, str)
                or current_repository_name.casefold() != request.repository.casefold()
            ):
                raise PbiCreationScopeError("Repository is not available")
            if (
                repository_node_id is not None
                and repository_node_id != current_repository_id
            ):
                raise ProviderError("GitHub returned conflicting repository identities")
            repository_node_id = current_repository_id
            label_connection = _mapping(repository.get("labels"))
            for label in _nodes(label_connection):
                name = label.get("name")
                label_id = label.get("id")
                if isinstance(name, str) and isinstance(label_id, str):
                    labels_by_name[name] = label_id
            has_next, label_cursor = _next_cursor(label_connection)
            if not has_next:
                break

        missing_labels = [name for name in request.labels if name not in labels_by_name]
        if missing_labels:
            raise PbiCreationValidationError(
                "One or more labels do not exist in the target repository",
                code="unknown_label",
            )
        return PbiCreationTarget(
            project_node_id=project_node_id,
            repository_node_id=repository_node_id,
            status_field_id=status_field_id,
            backlog_option_id=backlog_option_id,
            backlog_status=backlog_status,
            label_ids=tuple(labels_by_name[name] for name in request.labels),
        )


__all__ = ["PbiCreationPrepareMixin"]
