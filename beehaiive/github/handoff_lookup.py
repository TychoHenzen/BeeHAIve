from __future__ import annotations

from typing import Any

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.handoff_helpers import (
    _validate_branch_name as _validate_branch_name,
)
from beehaiive.github.pull_request_helpers import (
    _pull_request_snapshot as _pull_request_snapshot,
)
from beehaiive.github.queries.completion import (
    PULL_REQUEST_STATE_QUERY as PULL_REQUEST_STATE_QUERY,
)
from beehaiive.github.queries.pull_requests import (
    BASE_BRANCH_QUERY as BASE_BRANCH_QUERY,
)
from beehaiive.github.queries.pull_requests import (
    DEFAULT_BRANCH_QUERY as DEFAULT_BRANCH_QUERY,
)
from beehaiive.models import PullRequestSnapshot


class HandoffLookupMixin:
    def _resolve_custom_base_branch(
        self: Any, owner: str, name: str, requested: str
    ) -> tuple[str, str]:
        data = self._client.execute(
            BASE_BRANCH_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedBranch": f"refs/heads/{requested}",
            },
        )
        base_ref = _mapping(_mapping(data.get("repository")).get("baseRef"))
        resolved_name = base_ref.get("name")
        resolved_oid = _mapping(base_ref.get("target")).get("oid")
        if resolved_name != requested or not isinstance(resolved_oid, str):
            raise ProviderError(
                f"GitHub repository does not contain base branch: {requested}"
            )
        return requested, resolved_oid

    def resolve_base_branch(self: Any, repository: str, requested: str | None) -> str:
        owner, name = self._repository_parts(repository)
        if requested:
            resolved_name, _ = self._resolve_custom_base_branch(owner, name, requested)
            return resolved_name
        data = self._client.execute(
            DEFAULT_BRANCH_QUERY,
            {"owner": owner, "name": name},
        )
        repository_data = _mapping(data.get("repository"))
        default_branch = _mapping(repository_data.get("defaultBranchRef"))
        name_value = default_branch.get("name")
        if not isinstance(name_value, str) or not name_value:
            raise ProviderError("GitHub repository did not include a default branch")
        return name_value

    def validate_handoff(
        self: Any, repository: str, branch: str, requested_base: str | None
    ) -> str:
        _validate_branch_name(branch)
        return self.resolve_base_branch(repository, requested_base)

    def get_pull_request(
        self: Any, repository: str, number: int
    ) -> PullRequestSnapshot:
        if number <= 0:
            raise ProviderError("Pull-request number must be positive")
        owner, name = self._repository_parts(repository)
        data = self._client.execute(
            PULL_REQUEST_STATE_QUERY,
            {"owner": owner, "name": name, "number": number},
        )
        repository_data = _mapping(data.get("repository"))
        pull_request = repository_data.get("pullRequest")
        if pull_request is None:
            raise ProviderError(f"Pull request not found: {repository}#{number}")
        return _pull_request_snapshot(repository, number, pull_request)


__all__ = ["HandoffLookupMixin"]
