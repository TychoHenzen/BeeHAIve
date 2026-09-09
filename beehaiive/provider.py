"""Provider boundaries for GitHub discovery and pull-request handoff."""

from __future__ import annotations

import base64
import json
import os
import re
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

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        """Validate handoff names and return the existing base branch."""

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
              labels(first: 100) { nodes { name } }
              subIssues(first: 100) {
                nodes {
                  number
                  title
                  state
                  labels(first: 20) { nodes { name } }
                }
              }
              comments(first: 50) {
                nodes {
                  author { ... on User { login } ... on Bot { login } }
                  body
                  createdAt
                  url
                }
              }
              closedByPullRequestsReferences(includeClosedPrs: true, first: 100) {
                nodes {
                  number
                  url
                  reviewDecision
                  reviewRequests(first: 100) {
                    nodes {
                      requestedReviewer {
                        ... on User { login }
                        ... on Team { name }
                      }
                    }
                  }
                  latestReviews(first: 100) {
                    nodes {
                      author { ... on User { login } ... on Bot { login } }
                      state
                      body
                      submittedAt
                      url
                    }
                  }
                }
              }
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
      nodes { number url headRefName baseRefName body }
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


def _project(data: Mapping[str, Any], owner_type: str = "user") -> Mapping[str, Any]:
    owner = _mapping(data.get(owner_type))
    return _mapping(owner.get("projectV2"))


def _owner_query(query: str, owner_type: str) -> str:
    if owner_type not in {"user", "organization"}:
        raise ProviderError("GitHub Project owner type must be user or organization")
    return query.replace("user(login:", f"{owner_type}(login:")


def _next_cursor(connection: Mapping[str, Any]) -> tuple[bool, str | None]:
    page_info = _mapping(connection.get("pageInfo", {}))
    has_next = page_info.get("hasNextPage") is True
    cursor = page_info.get("endCursor")
    if has_next and not isinstance(cursor, str):
        raise ProviderError("GitHub GraphQL page did not include an end cursor")
    return has_next, cursor if isinstance(cursor, str) else None


def _stage_from_status(status: str | None) -> Stage | None:
    normalized = (status or "").strip().lower()
    if normalized == "backlog":
        return Stage.BACKLOG
    if normalized == "todo":
        return Stage.REFINE
    if normalized == "in progress":
        return Stage.IMPLEMENT
    return None


def _actor_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    actor = cast(Mapping[str, Any], value)
    for key in ("login", "name"):
        name = actor.get(key)
        if isinstance(name, str) and name:
            return name
    return None


def _label_names(value: object) -> list[str]:
    names: list[str] = []
    for label in _nodes(value):
        name = label.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _review_status(state: object) -> str:
    normalized = str(state or "").strip().upper()
    if normalized == "APPROVED":
        return "pass"
    if normalized == "CHANGES_REQUESTED":
        return "fail"
    return "pending"


def _dashboard_metadata(issue: Mapping[str, Any]) -> dict[str, object]:
    """Project issue metadata that the dashboard can show without fake state."""

    metadata: dict[str, object] = {}
    labels = _label_names(issue.get("labels", {}))

    subtasks: list[dict[str, object]] = []
    for raw_subtask in _nodes(issue.get("subIssues", {})):
        number = raw_subtask.get("number")
        title = raw_subtask.get("title")
        if not isinstance(number, int) or not isinstance(title, str):
            continue
        subtask: dict[str, object] = {
            "id": f"#{number}",
            "number": number,
            "title": title,
        }
        state = raw_subtask.get("state")
        if isinstance(state, str):
            subtask["status"] = state.lower()
        subtask_labels = _label_names(raw_subtask.get("labels", {}))
        if subtask_labels:
            subtask["labels"] = subtask_labels
        subtasks.append(subtask)
    if subtasks:
        metadata["subtasks"] = subtasks

    readers: list[dict[str, object]] = []
    reviewers: dict[str, dict[str, object]] = {}
    pull_requests: list[dict[str, object]] = []
    for raw_pull_request in _nodes(issue.get("closedByPullRequestsReferences", {})):
        pull_request_number = raw_pull_request.get("number")
        if not isinstance(pull_request_number, int):
            continue
        pull_request_readers: list[dict[str, object]] = []
        pull_request_reviewers: dict[str, dict[str, object]] = {}
        for raw_request in _nodes(raw_pull_request.get("reviewRequests", {})):
            name = _actor_name(raw_request.get("requestedReviewer"))
            if name is None:
                continue
            reader: dict[str, object] = {
                "id": f"#{pull_request_number}:{name}",
                "name": name,
                "pull_request": pull_request_number,
                "status": "pending",
            }
            pull_request_readers.append(reader)
            readers.append(reader)

        for raw_review in _nodes(raw_pull_request.get("latestReviews", {})):
            name = _actor_name(raw_review.get("author"))
            if name is None:
                continue
            status = _review_status(raw_review.get("state"))
            reviewer: dict[str, object] = {
                "status": status,
                "pull_request": pull_request_number,
            }
            body = raw_review.get("body")
            if isinstance(body, str) and body.strip():
                reviewer["comment"] = body
            submitted_at = raw_review.get("submittedAt")
            if isinstance(submitted_at, str):
                reviewer["submitted_at"] = submitted_at
            reviewer_key = f"#{pull_request_number}:{name}"
            pull_request_reviewers[reviewer_key] = reviewer
            reviewers[reviewer_key] = reviewer
            for reader in pull_request_readers:
                if reader["name"] == name:
                    reader["status"] = status
                    break
            else:
                reader = {
                    "id": reviewer_key,
                    "name": name,
                    "pull_request": pull_request_number,
                    "status": status,
                }
                pull_request_readers.append(reader)
                readers.append(reader)

        pull_request: dict[str, object] = {
            "number": pull_request_number,
            "readers": pull_request_readers,
            "reviewers": pull_request_reviewers,
        }
        url = raw_pull_request.get("url")
        if isinstance(url, str):
            pull_request["url"] = url
        decision = raw_pull_request.get("reviewDecision")
        if isinstance(decision, str):
            pull_request["review_decision"] = decision.lower()
        pull_requests.append(pull_request)

    if readers:
        metadata["readers"] = readers
    if reviewers:
        metadata["reviewers"] = reviewers
    if pull_requests:
        metadata["pull_requests"] = pull_requests

    activity: list[dict[str, object]] = []
    for comment in _nodes(issue.get("comments", {})):
        body = comment.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        entry: dict[str, object] = {
            "agent": _actor_name(comment.get("author")) or "github",
            "action": body,
        }
        for source_key, target_key in (("createdAt", "time"), ("url", "url")):
            value = comment.get(source_key)
            if isinstance(value, str):
                entry[target_key] = value
        activity.append(entry)
    if activity:
        metadata["activity"] = activity

    bounce_count = 0
    for label in labels:
        match = re.fullmatch(r"bounces?/(\d+)", label.strip(), re.IGNORECASE)
        if match:
            bounce_count = max(bounce_count, int(match.group(1)))
    escalation_log = [
        {"tier": label.split("/", 1)[1], "resolved": False}
        for label in labels
        if label.lower().startswith("escalation/") and "/" in label
    ]
    if bounce_count or escalation_log:
        metadata["escalation"] = {
            "current": bounce_count,
            "consecutive": bounce_count,
        }
        metadata["escalation_log"] = escalation_log

    return metadata


def _validate_branch_name(branch: str) -> None:
    if (
        not branch
        or branch.strip() != branch
        or branch in {".", "..", "@"}
        or ".." in branch
        or "@{" in branch
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.startswith(".")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or "//" in branch
        or any(ord(char) < 32 or char in " ~^:?*[\\" for char in branch)
    ):
        raise ProviderError("GitHub branch name is invalid")


def _handoff_marker(request: HandoffRequest) -> str:
    if not request.run_id.strip():
        raise ProviderError("Handoff run identity is required")
    payload = json.dumps(
        {
            "pbi_number": request.pbi_number,
            "project_id": request.project_id,
            "repository": request.repository,
            "run_id": request.run_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"<!-- beehaiive-handoff:{encoded} -->"


def _handoff_body(body: str, marker: str) -> str:
    if marker in body:
        return body
    return f"{body.rstrip()}\n\n{marker}" if body.strip() else marker


def _pull_request_matches(
    pull_request: Mapping[str, Any],
    branch: str,
    base_branch: str,
    identity_marker: str,
) -> bool:
    return (
        pull_request.get("headRefName") == branch
        and pull_request.get("baseRefName") == base_branch
        and isinstance(pull_request.get("body"), str)
        and identity_marker in pull_request["body"]
    )


class GitHubProjectProvider:
    """GitHub ProjectV2 provider for a user- or organization-owned project."""

    def __init__(
        self,
        owner: str,
        project_number: int,
        token: str,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
        owner_type: str = "user",
    ) -> None:
        if owner_type not in {"user", "organization"}:
            raise ProviderError(
                "GitHub Project owner type must be user or organization"
            )
        self.owner = owner
        self.project_number = project_number
        self.owner_type = owner_type
        self.project_id = f"{owner}:{project_number}"
        self._client = client or UrllibGraphQLClient(token, endpoint)

    @classmethod
    def from_environment(cls) -> GitHubProjectProvider:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        owner = os.environ.get("GITHUB_PROJECT_OWNER")
        number_text = os.environ.get("GITHUB_PROJECT_NUMBER")
        owner_type = os.environ.get("GITHUB_PROJECT_OWNER_TYPE", "user")
        if not token or not owner or not number_text:
            raise ProviderError(
                "Set GITHUB_TOKEN, GITHUB_PROJECT_OWNER, and GITHUB_PROJECT_NUMBER"
            )
        try:
            number = int(number_text)
        except ValueError as exc:
            raise ProviderError("GITHUB_PROJECT_NUMBER must be an integer") from exc
        return cls(owner, number, token, owner_type=owner_type)

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        if project_id != self.project_id:
            raise ProviderError(
                f"Provider is configured for {self.project_id}, not {project_id}"
            )

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
                stage = _stage_from_status(status)
                repositories.setdefault(repository_name, []).append(
                    PbiSnapshot(
                        repository_name,
                        number,
                        title,
                        stage,
                        status,
                        stage is not None,
                        _dashboard_metadata(content),
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

    @staticmethod
    def _repository_parts(repository: str) -> tuple[str, str]:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise ProviderError(f"Repository must use owner/name format: {repository}")
        return owner, name

    def _resolve_custom_base_branch(
        self, owner: str, name: str, requested: str
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

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
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
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        _validate_branch_name(branch)
        return self.resolve_base_branch(repository, requested_base)

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        owner, name = self._repository_parts(request.repository)
        _validate_branch_name(request.branch)
        identity_marker = _handoff_marker(request)
        qualified_branch = f"refs/heads/{request.branch}"
        data = self._client.execute(
            REPOSITORY_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedBranch": qualified_branch,
                "pullRequestCursor": None,
            },
        )
        repository = _mapping(data.get("repository"))
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
            _, base_oid = self._resolve_custom_base_branch(owner, name, base_branch)

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
                    retry_repository = _mapping(_mapping(retry_data.get("repository")))
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
                        f"GitHub did not confirm branch creation: {qualified_branch}"
                    )

        existing = self._find_existing_pull_request(
            owner,
            name,
            qualified_branch,
            request.branch,
            base_branch,
            identity_marker,
            initial_repository=repository,
        )
        if existing is not None:
            return existing

        try:
            pull_request_data = self._client.execute(
                CREATE_PULL_REQUEST_MUTATION,
                {
                    "input": {
                        "repositoryId": repository_id,
                        "baseRefName": base_branch,
                        "headRefName": request.branch,
                        "title": request.title,
                        "body": _handoff_body(request.body, identity_marker),
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
                    identity_marker,
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
        identity_marker: str,
        initial_repository: Mapping[str, Any] | None = None,
    ) -> HandoffResult | None:
        cursor: str | None = None
        repository = initial_repository
        while True:
            if repository is None:
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
                if _pull_request_matches(
                    pull_request, branch, base_branch, identity_marker
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
            repository = None


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

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return GitHubProjectProvider.from_environment().validate_handoff(
            repository, branch, requested_base
        )
