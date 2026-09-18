from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.dashboard_helpers import (
    _dashboard_metadata as _dashboard_metadata,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.graphql_helpers import (
    _project_item_status_values as _project_item_status_values,
)
from beehaiive.github.graphql_helpers import (
    _project_status_index as _project_status_index,
)
from beehaiive.github.graphql_helpers import _stage_from_status as _stage_from_status
from beehaiive.github.queries.discovery import ITEMS_QUERY as ITEMS_QUERY
from beehaiive.github.queries.discovery import PROJECT_QUERY as PROJECT_QUERY
from beehaiive.github.queries.discovery import REPOSITORIES_QUERY as REPOSITORIES_QUERY
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot


class DiscoveryMixin:
    def discover_project(self: Any, project_id: str) -> ProjectSnapshot:
        if project_id != self.project_id:
            raise ProviderError(
                f"Provider is configured for {self.project_id}, not {project_id}"
            )

        with self._discovery_lock:
            cached = self._discovery_cache
            if (
                cached is not None
                and time.monotonic() - cached[0] < self._discovery_cache_seconds
            ):
                return cached[1]
            try:
                snapshot = self._discover_project_uncached()
            except GitHubRateLimitError:
                if cached is None:
                    raise
                return cached[1]
            self._discovery_cache = (time.monotonic(), snapshot)
            return snapshot

    def invalidate_discovery_cache(self: Any) -> None:
        with self._discovery_lock:
            self._discovery_cache = None

    def _discover_project_uncached(self: Any) -> ProjectSnapshot:

        data = self._client.execute(
            _owner_query(PROJECT_QUERY, self.owner_type),
            {
                "owner": self.owner,
                "number": self.project_number,
            },
        )
        project = _project(data, self.owner_type)
        repositories: dict[str, list[PbiSnapshot]] = {}
        repository_cursor: str | None = None
        while True:
            repository_data = self._client.execute(
                _owner_query(REPOSITORIES_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": repository_cursor,
                },
            )
            repository_connection = _mapping(
                _project(repository_data, self.owner_type).get("repositories")
            )
            for repository in _nodes(repository_connection):
                name = repository.get("nameWithOwner")
                if isinstance(name, str) and name:
                    repositories.setdefault(name, [])
            has_next, repository_cursor = _next_cursor(repository_connection)
            if not has_next:
                break

        project_items: list[Mapping[str, Any]] = []
        item_cursor: str | None = None
        while True:
            item_data = self._client.execute(
                _owner_query(ITEMS_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": item_cursor,
                },
            )
            item_connection = _mapping(
                _project(item_data, self.owner_type).get("items")
            )
            project_items.extend(_nodes(item_connection))
            has_next, item_cursor = _next_cursor(item_connection)
            if not has_next:
                break

        project_statuses, project_status_conflicts = _project_status_index(
            project_items
        )
        parent_numbers: dict[tuple[str, int], int] = {}
        for item in project_items:
            content_value = item.get("content")
            if not isinstance(content_value, Mapping):
                continue
            content = cast(Mapping[str, Any], content_value)
            repository_value = content.get("repository")
            if not isinstance(repository_value, Mapping):
                continue
            repository = cast(Mapping[str, Any], repository_value)
            repository_name = repository.get("nameWithOwner")
            number = content.get("number")
            if not isinstance(repository_name, str) or not isinstance(number, int):
                continue
            for child in _nodes(content.get("subIssues", {})):
                child_number = child.get("number")
                if isinstance(child_number, int):
                    parent_numbers[(repository_name.casefold(), child_number)] = number

        for project_order, item in enumerate(project_items):
            content_value = item.get("content")
            if content_value is None:
                continue
            content = _mapping(content_value)
            if content.get("__typename") != "Issue":
                continue
            repository = _mapping(content.get("repository"))
            repository_name = repository.get("nameWithOwner")
            number = content.get("number")
            title = content.get("title")
            if (
                not isinstance(repository_name, str)
                or not isinstance(number, int)
                or not isinstance(title, str)
            ):
                continue
            key = (repository_name.casefold(), number)
            status_values = set(_project_item_status_values(item))
            status = next(iter(status_values)) if len(status_values) == 1 else None
            content_with_project_status = dict(content)
            content_with_project_status["projectStatus"] = status
            content_with_project_status["projectStatusConflict"] = (
                len(status_values) > 1 or key in project_status_conflicts
            )
            completed_content = self._complete_issue_metadata(
                content_with_project_status,
                project_statuses,
                project_status_conflicts,
            )
            stage = _stage_from_status(status)
            metadata = _dashboard_metadata(completed_content)
            metadata["project_order"] = project_order
            parent_number = parent_numbers.get((repository_name.casefold(), number))
            if parent_number is not None:
                metadata["parent_issue_number"] = parent_number
            repositories.setdefault(repository_name, []).append(
                PbiSnapshot(
                    repository_name,
                    number,
                    title,
                    stage,
                    status,
                    stage is not None,
                    metadata,
                )
            )

        repository_snapshots = tuple(
            RepositorySnapshot(
                name=name,
                pbis=tuple(sorted(pbis, key=lambda pbi: pbi.number)),
            )
            for name, pbis in sorted(repositories.items())
        )
        project_name = project.get("title")
        if not isinstance(project_name, str):
            raise ProviderError("GitHub Project did not include a title")
        return ProjectSnapshot(self.project_id, project_name, repository_snapshots)

    @staticmethod
    def _repository_parts(repository: str) -> tuple[str, str]:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise ProviderError(f"Repository must use owner/name format: {repository}")
        return owner, name


__all__ = ["DiscoveryMixin"]
