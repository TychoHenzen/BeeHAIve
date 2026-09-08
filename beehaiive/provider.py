"""Provider boundaries for GitHub discovery and pull-request handoff."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol, cast
from urllib.request import Request, urlopen

from .models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)


class ProviderError(RuntimeError):
    """Raised when a provider cannot discover or hand off work."""


class GraphQLClient(Protocol):
    """Small client boundary that can be replaced by a GitHub API test double."""

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        """Execute a GraphQL operation and return its data object."""

        ...


class UrllibGraphQLClient:
    """Minimal GitHub GraphQL client using only the Python standard library."""

    def __init__(
        self,
        token: str,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._endpoint = endpoint

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        request = Request(
            self._endpoint,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(
                "utf-8"
            ),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw_payload: object = json.loads(response.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"GitHub GraphQL returned invalid JSON: {exc}") from exc
        except OSError as exc:
            raise ProviderError(f"GitHub GraphQL request failed: {exc}") from exc

        if not isinstance(raw_payload, dict):
            raise ProviderError("GitHub GraphQL returned a non-object response")
        payload = cast(dict[str, Any], raw_payload)
        errors = payload.get("errors")
        if errors:
            raise ProviderError(f"GitHub GraphQL returned errors: {errors}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError("GitHub GraphQL response did not contain data")
        return cast(dict[str, Any], data)


class ProjectProvider(Protocol):
    """Provider used by the orchestrator for discovery and handoff."""

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        """Return the selected project and every linked repository."""

        ...

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        """Create or reuse the branch and pull request for a PBI run."""

        ...

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        """Resolve and return the base branch before a handoff is persisted."""

        ...


PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      title
    }
  }
}
"""

REPOSITORIES_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      repositories(first: 100, after: $cursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        nodes {
          content {
            __typename
            ... on Issue {
              number
              title
              repository { nameWithOwner }
            }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

REPOSITORY_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedBranch: String!
  $pullRequestCursor: String
) {
  repository(owner: $owner, name: $name) {
    id
    defaultBranchRef { name target { oid } }
    ref(qualifiedName: $qualifiedBranch) { name }
    pullRequests(
      first: 100
      after: $pullRequestCursor
      states: [OPEN, CLOSED, MERGED]
    ) {
      nodes { number url headRefName baseRefName }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

DEFAULT_BRANCH_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
  }
}
"""

BASE_BRANCH_QUERY = """
query($owner: String!, $name: String!, $qualifiedBranch: String!) {
  repository(owner: $owner, name: $name) {
    baseRef: ref(qualifiedName: $qualifiedBranch) { name target { oid } }
  }
}
"""

CREATE_REF_MUTATION = """
mutation($input: CreateRefInput!) {
  createRef(input: $input) { ref { name } }
}
"""

CREATE_PULL_REQUEST_MUTATION = """
mutation($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest { number url }
  }
}
"""


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError("GitHub GraphQL returned an invalid object")
    return cast(Mapping[str, Any], value)


def _nodes(value: object) -> list[Mapping[str, Any]]:
    container = _mapping(value)
    raw_nodes: object = container.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ProviderError("GitHub GraphQL returned invalid nodes")
    node_values = cast(list[object], raw_nodes)
    return [_mapping(node) for node in node_values if node is not None]


def _project(data: Mapping[str, Any]) -> Mapping[str, Any]:
    user = _mapping(data.get("user"))
    return _mapping(user.get("projectV2"))


def _next_cursor(connection: Mapping[str, Any]) -> tuple[bool, str | None]:
    page_info = _mapping(connection.get("pageInfo", {}))
    has_next = page_info.get("hasNextPage") is True
    cursor = page_info.get("endCursor")
    if has_next and not isinstance(cursor, str):
        raise ProviderError("GitHub GraphQL page did not include an end cursor")
    return has_next, cursor if isinstance(cursor, str) else None


def _stage_from_status(status: str | None) -> Stage:
    normalized = (status or "").strip().lower()
    if normalized == "backlog":
        return Stage.BACKLOG
    if normalized == "todo":
        return Stage.REFINE
    if normalized == "in progress":
        return Stage.IMPLEMENT
    if normalized == "done":
        return Stage.PULL_REQUEST
    return Stage.BACKLOG


class GitHubProjectProvider:
    """GitHub ProjectV2 provider for a user-owned project."""

    def __init__(
        self,
        owner: str,
        project_number: int,
        token: str,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self.owner = owner
        self.project_number = project_number
        self.project_id = f"{owner}:{project_number}"
        self._client = client or UrllibGraphQLClient(token, endpoint)

    @classmethod
    def from_environment(cls) -> GitHubProjectProvider:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        owner = os.environ.get("GITHUB_PROJECT_OWNER")
        number_text = os.environ.get("GITHUB_PROJECT_NUMBER")
        if not token or not owner or not number_text:
            raise ProviderError(
                "Set GITHUB_TOKEN, GITHUB_PROJECT_OWNER, and GITHUB_PROJECT_NUMBER"
            )
        try:
            number = int(number_text)
        except ValueError as exc:
            raise ProviderError("GITHUB_PROJECT_NUMBER must be an integer") from exc
        return cls(owner, number, token)

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        if project_id != self.project_id:
            raise ProviderError(
                f"Provider is configured for {self.project_id}, not {project_id}"
            )

        data = self._client.execute(
            PROJECT_QUERY,
            {
                "owner": self.owner,
                "number": self.project_number,
            },
        )
        project = _project(data)
        repositories: dict[str, list[PbiSnapshot]] = {}
        repository_cursor: str | None = None
        while True:
            repository_data = self._client.execute(
                REPOSITORIES_QUERY,
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": repository_cursor,
                },
            )
            repository_connection = _mapping(
                _project(repository_data).get("repositories")
            )
            for repository in _nodes(repository_connection):
                name = repository.get("nameWithOwner")
                if isinstance(name, str) and name:
                    repositories.setdefault(name, [])
            has_next, repository_cursor = _next_cursor(repository_connection)
            if not has_next:
                break

        item_cursor: str | None = None
        while True:
            item_data = self._client.execute(
                ITEMS_QUERY,
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": item_cursor,
                },
            )
            item_connection = _mapping(_project(item_data).get("items"))
            for item in _nodes(item_connection):
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
                status = None
                for field_value in _nodes(item.get("fieldValues", {})):
                    raw_field = field_value.get("field")
                    if raw_field is None:
                        continue
                    field = _mapping(raw_field)
                    if field.get("name") == "Status" and isinstance(
                        field_value.get("name"), str
                    ):
                        status = field_value["name"]
                        break
                repositories.setdefault(repository_name, []).append(
                    PbiSnapshot(
                        repository_name,
                        number,
                        title,
                        _stage_from_status(status),
                    )
                )
            has_next, item_cursor = _next_cursor(item_connection)
            if not has_next:
                break

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

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        if requested:
            return requested
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise ProviderError(f"Repository must use owner/name format: {repository}")
        data = self._client.execute(
            DEFAULT_BRANCH_QUERY,
            {"owner": owner, "name": name},
        )
        repository_data = _mapping(_mapping(data.get("repository")))
        default_branch = _mapping(repository_data.get("defaultBranchRef"))
        name_value = default_branch.get("name")
        if not isinstance(name_value, str) or not name_value:
            raise ProviderError("GitHub repository did not include a default branch")
        return name_value

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        owner, separator, name = request.repository.partition("/")
        if not separator or not owner or not name:
            raise ProviderError(
                f"Repository must use owner/name format: {request.repository}"
            )
        qualified_branch = f"refs/heads/{request.branch}"
        pull_request_cursor: str | None = None
        repository_id = ""
        base_branch = ""
        base_oid = ""
        while True:
            data = self._client.execute(
                REPOSITORY_QUERY,
                {
                    "owner": owner,
                    "name": name,
                    "qualifiedBranch": qualified_branch,
                    "pullRequestCursor": pull_request_cursor,
                },
            )
            repository = _mapping(_mapping(data.get("repository")))
            if not repository_id:
                raw_repository_id = repository.get("id")
                default_branch = _mapping(repository.get("defaultBranchRef"))
                default_branch_name = default_branch.get("name")
                default_oid = _mapping(default_branch.get("target")).get("oid")
                if (
                    not isinstance(raw_repository_id, str)
                    or not isinstance(default_branch_name, str)
                    or not isinstance(default_oid, str)
                ):
                    raise ProviderError(
                        "GitHub repository did not include branch creation metadata"
                    )
                repository_id = raw_repository_id
                base_branch = request.base_branch or default_branch_name
                base_oid = default_oid
                if base_branch != default_branch_name:
                    base_data = self._client.execute(
                        BASE_BRANCH_QUERY,
                        {
                            "owner": owner,
                            "name": name,
                            "qualifiedBranch": f"refs/heads/{base_branch}",
                        },
                    )
                    base_ref = _mapping(
                        _mapping(base_data.get("repository")).get("baseRef")
                    )
                    resolved_base_name = base_ref.get("name")
                    resolved_base_oid = _mapping(base_ref.get("target")).get("oid")
                    if resolved_base_name != base_branch or not isinstance(
                        resolved_base_oid, str
                    ):
                        raise ProviderError(
                            "GitHub repository does not contain base branch: "
                            f"{base_branch}"
                        )
                    base_oid = resolved_base_oid

                if repository.get("ref") is None:
                    try:
                        create_data = self._client.execute(
                            CREATE_REF_MUTATION,
                            {
                                "input": {
                                    "repositoryId": repository_id,
                                    "name": qualified_branch,
                                    "oid": base_oid,
                                }
                            },
                        )
                    except ProviderError as create_error:
                        try:
                            retry_data = self._client.execute(
                                REPOSITORY_QUERY,
                                {
                                    "owner": owner,
                                    "name": name,
                                    "qualifiedBranch": qualified_branch,
                                    "pullRequestCursor": None,
                                },
                            )
                            retry_repository = _mapping(
                                _mapping(retry_data.get("repository"))
                            )
                        except ProviderError:
                            raise create_error from None
                        if retry_repository.get("ref") is None:
                            raise create_error from None
                        repository = retry_repository
                    else:
                        created_ref = _mapping(
                            _mapping(create_data.get("createRef")).get("ref")
                        )
                        if created_ref.get("name") != qualified_branch:
                            raise ProviderError(
                                "GitHub did not confirm branch creation: "
                                f"{qualified_branch}"
                            )

            for pull_request in _nodes(repository.get("pullRequests", {})):
                if (
                    pull_request.get("headRefName") == request.branch
                    and pull_request.get("baseRefName") == base_branch
                ):
                    url = pull_request.get("url")
                    number = pull_request.get("number")
                    if isinstance(url, str) and isinstance(number, int):
                        return HandoffResult(request.branch, url, number)

            has_next, pull_request_cursor = _next_cursor(
                _mapping(repository.get("pullRequests", {}))
            )
            if not has_next:
                break

        try:
            pull_request_data = self._client.execute(
                CREATE_PULL_REQUEST_MUTATION,
                {
                    "input": {
                        "repositoryId": repository_id,
                        "baseRefName": base_branch,
                        "headRefName": request.branch,
                        "title": request.title,
                        "body": request.body,
                    }
                },
            )
        except ProviderError as create_error:
            try:
                existing = self._find_existing_pull_request(
                    owner,
                    name,
                    qualified_branch,
                    request.branch,
                    base_branch,
                )
            except ProviderError:
                raise create_error from None
            if existing is None:
                raise create_error from None
            return existing
        pull_request = _mapping(
            _mapping(pull_request_data.get("createPullRequest")).get("pullRequest")
        )
        url = pull_request.get("url")
        number = pull_request.get("number")
        if not isinstance(url, str) or not isinstance(number, int):
            raise ProviderError("GitHub did not return a pull-request record")
        return HandoffResult(request.branch, url, number)

    def _find_existing_pull_request(
        self,
        owner: str,
        name: str,
        qualified_branch: str,
        branch: str,
        base_branch: str,
    ) -> HandoffResult | None:
        cursor: str | None = None
        while True:
            data = self._client.execute(
                REPOSITORY_QUERY,
                {
                    "owner": owner,
                    "name": name,
                    "qualifiedBranch": qualified_branch,
                    "pullRequestCursor": cursor,
                },
            )
            repository = _mapping(_mapping(data.get("repository")))
            for pull_request in _nodes(repository.get("pullRequests", {})):
                if (
                    pull_request.get("headRefName") == branch
                    and pull_request.get("baseRefName") == base_branch
                ):
                    url = pull_request.get("url")
                    number = pull_request.get("number")
                    if isinstance(url, str) and isinstance(number, int):
                        return HandoffResult(branch, url, number)
            has_next, cursor = _next_cursor(
                _mapping(repository.get("pullRequests", {}))
            )
            if not has_next:
                return None


class EnvironmentGitHubProvider:
    """Lazy provider used by the default FastAPI app."""

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return GitHubProjectProvider.from_environment().discover_project(project_id)

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return GitHubProjectProvider.from_environment().create_handoff(request)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return GitHubProjectProvider.from_environment().resolve_base_branch(
            repository, requested
        )
