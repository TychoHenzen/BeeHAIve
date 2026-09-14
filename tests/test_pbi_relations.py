from __future__ import annotations

from dataclasses import replace

import pytest

from beehaiive.pbi_relations import (
    PbiCreatedIssueReference,
    PbiRelationDependency,
    PbiRelationIssue,
    PbiRelationProviderError,
    PbiRelationRequest,
    PbiRelationService,
    PbiRelationSnapshot,
    PbiRelationValidationError,
)
from beehaiive.provider import GitHubProjectProvider, UrllibGraphQLClient


def _fact(number: int, *, title: str | None = None) -> PbiRelationIssue:
    return PbiRelationIssue(
        100 + number,
        f"node-{number}",
        number,
        f"https://github.com/owner/repo/issues/{number}",
        title or f"Issue {number}",
        "OPEN",
    )


def _request(
    dependencies: tuple[PbiRelationDependency, ...] = (),
) -> PbiRelationRequest:
    return PbiRelationRequest(
        project_id="owner:2",
        repository="owner/repo",
        parent_issue_number=1,
        children=tuple(
            PbiCreatedIssueReference(
                repository="owner/repo",
                node_id=f"node-{number}",
                number=number,
                url=f"https://github.com/owner/repo/issues/{number}",
                project_item_id=f"item-{number}",
            )
            for number in (2, 3)
        ),
        dependencies=dependencies,
    )


class FakeRelationProvider:
    def __init__(self) -> None:
        self.parent_children: set[int] = set()
        self.parent_by_child: dict[int, int | None] = {2: None, 3: None}
        self.blocked_by: dict[int, set[int]] = {2: set(), 3: set()}
        self.added_sub_issues: list[tuple[int, int]] = []
        self.added_dependencies: list[tuple[int, int]] = []
        self.preflight_calls = 0
        self.fail_blocking = False
        self.fail_sub_issue_id: int | None = None
        self.fail_parent_readback_number: int | None = None
        self.fail_blocked_by_call: tuple[int, int] | None = None
        self.fail_dependency = False
        self.blocked_by_calls: dict[int, int] = {}

    def prepare_pbi_relations(self, request: PbiRelationRequest) -> PbiRelationSnapshot:
        self.preflight_calls += 1
        return PbiRelationSnapshot(
            _fact(1),
            tuple(_fact(child.number) for child in request.children),
            tuple(_fact(number) for number in sorted(self.parent_children)),
            dict(self.parent_by_child),
        )

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        assert parent_issue_number == 1
        return tuple(_fact(number) for number in sorted(self.parent_children))

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None:
        assert repository == "owner/repo"
        if child_issue_number == self.fail_parent_readback_number:
            raise PbiRelationProviderError("permission_denied")
        return self.parent_by_child[child_issue_number]

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        self.blocked_by_calls[issue_number] = (
            self.blocked_by_calls.get(issue_number, 0) + 1
        )
        if self.fail_blocked_by_call == (
            issue_number,
            self.blocked_by_calls[issue_number],
        ):
            raise PbiRelationProviderError("permission_denied")
        return tuple(_fact(number) for number in sorted(self.blocked_by[issue_number]))

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        if self.fail_blocking:
            raise PbiRelationProviderError("permission_denied")
        return tuple(
            _fact(blocked)
            for blocked, blockers in self.blocked_by.items()
            if issue_number in blockers
        )

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        assert repository == "owner/repo"
        child_number = child_issue_id - 100
        self.added_sub_issues.append((parent_issue_number, child_issue_id))
        self.parent_children.add(child_number)
        self.parent_by_child[child_number] = parent_issue_number
        if child_issue_id == self.fail_sub_issue_id:
            raise PbiRelationProviderError("permission_denied")

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        assert repository == "owner/repo"
        blocker_number = blocker_issue_id - 100
        self.added_dependencies.append((blocked_issue_number, blocker_issue_id))
        if self.fail_dependency:
            raise PbiRelationProviderError("permission_denied")
        self.blocked_by[blocked_issue_number].add(blocker_number)


def test_relations_are_idempotent_and_read_back_from_provider() -> None:
    provider = FakeRelationProvider()
    request = _request((PbiRelationDependency(3, 2),))
    service = PbiRelationService(provider)

    first = service.apply(request)
    second = service.apply(request)

    assert first.status == "complete"
    assert first.confirmed_children == (2, 3)
    assert first.confirmed_dependencies == (PbiRelationDependency(3, 2),)
    assert second.status == "complete"
    assert len(provider.added_sub_issues) == 2
    assert provider.added_dependencies == [(3, 102)]


def test_dependency_cycle_is_rejected_before_any_relation_write() -> None:
    provider = FakeRelationProvider()
    provider.blocked_by[3].add(2)

    with pytest.raises(PbiRelationValidationError, match="cycle") as error:
        PbiRelationService(provider).apply(_request((PbiRelationDependency(2, 3),)))

    assert error.value.code == "dependency_cycle"
    assert provider.added_sub_issues == []
    assert provider.added_dependencies == []


def test_partial_sub_issue_failure_reports_confirmed_and_pending_edges() -> None:
    provider = FakeRelationProvider()
    provider.fail_sub_issue_id = 102
    request = _request((PbiRelationDependency(3, 2),))

    result = PbiRelationService(provider).apply(request)

    assert result.status == "incomplete"
    assert result.confirmed_children == (2,)
    assert result.pending_children == (3,)
    assert result.pending_dependencies == request.dependencies
    assert result.failure_code == "permission_denied"
    assert provider.added_dependencies == []


def test_sub_issue_readback_requires_both_parent_and_child_views() -> None:
    class InconsistentParentProvider(FakeRelationProvider):
        def get_pbi_parent_issue_number(
            self, repository: str, child_issue_number: int
        ) -> int | None:
            if child_issue_number == 2:
                return None
            return super().get_pbi_parent_issue_number(repository, child_issue_number)

    provider = InconsistentParentProvider()
    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.confirmed_children == (3,)
    assert result.pending_children == (2,)
    assert result.sub_issue_readback_complete is True
    assert result.failure_code == "relation_readback_incomplete"


def test_parent_endpoint_failure_preserves_other_child_confirmation() -> None:
    provider = FakeRelationProvider()
    provider.fail_parent_readback_number = 2

    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.confirmed_children == (3,)
    assert result.pending_children == (2,)
    assert result.sub_issue_readback_complete is False
    assert result.failure_code == "permission_denied"


def test_dependency_write_denial_reports_confirmed_children_and_pending_edge() -> None:
    provider = FakeRelationProvider()
    provider.fail_dependency = True
    request = _request((PbiRelationDependency(3, 2),))

    result = PbiRelationService(provider).apply(request)

    assert result.status == "incomplete"
    assert result.confirmed_children == (2, 3)
    assert result.confirmed_dependencies == ()
    assert result.pending_dependencies == request.dependencies
    assert result.failure_code == "permission_denied"


def test_dependency_readback_failure_is_incomplete_and_redacted() -> None:
    provider = FakeRelationProvider()
    provider.fail_sub_issue_id = 102
    provider.fail_blocked_by_call = (3, 2)
    result = PbiRelationService(provider).apply(
        _request((PbiRelationDependency(3, 2),))
    )

    payload = result.as_dict()
    assert result.status == "incomplete"
    assert result.dependency_readback_complete is False
    assert result.pending_dependencies == (PbiRelationDependency(3, 2),)
    assert "private-token" not in str(payload)


def test_incomplete_dependency_graph_fails_closed_before_writes() -> None:
    provider = FakeRelationProvider()
    provider.fail_blocking = True

    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.pending_step == "preflight"
    assert result.failure_code == "permission_denied"
    assert provider.added_sub_issues == []
    assert provider.added_dependencies == []


def test_cross_repository_reference_is_rejected_before_provider_read() -> None:
    provider = FakeRelationProvider()
    request = _request()
    request = replace(
        request,
        children=(replace(request.children[0], repository="owner/other"),),
    )

    with pytest.raises(PbiRelationValidationError) as error:
        PbiRelationService(provider).apply(request)

    assert error.value.code == "cross_repository_child"
    assert provider.preflight_calls == 0


def test_duplicate_dependency_declarations_are_rejected_before_provider_read() -> None:
    provider = FakeRelationProvider()
    edge = PbiRelationDependency(3, 2)

    with pytest.raises(PbiRelationValidationError) as error:
        PbiRelationService(provider).apply(_request((edge, edge)))

    assert error.value.code == "duplicate_dependency"
    assert provider.preflight_calls == 0


def _rest_issue(number: int, body: str = "") -> dict[str, object]:
    url = f"https://github.com/owner/repo/issues/{number}"
    return {
        "id": 100 + number,
        "node_id": f"node-{number}",
        "number": number,
        "url": f"https://api.github.com/repos/owner/repo/issues/{number}",
        "html_url": url,
        "title": f"Issue {number}",
        "state": "open",
        "body": body,
    }


class FakeRelationGitHubClient:
    def __init__(
        self,
        *,
        include_unrelated_field_value: bool = False,
        include_status: bool = True,
        duplicate_status: bool = False,
    ) -> None:
        self.rest_calls: list[tuple[str, str, object]] = []
        self.include_unrelated_field_value = include_unrelated_field_value
        self.include_status = include_status
        self.duplicate_status = duplicate_status
        self.parent_body = (
            "## Outcome\nOne outcome.\n"
            "## Scope\nOne scope.\n"
            "## Implementation notes\nOne note.\n"
            "## Acceptance criteria\nOne criterion.\n"
            "## Verification\nOne check.\n"
        )

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        del query, variables
        items = []
        for number, item_id, status in (
            (1, "parent-item", "Todo"),
            (2, "item-2", "Backlog"),
            (3, "item-3", "Backlog"),
        ):
            field_values: list[dict[str, object]] = []
            if self.include_unrelated_field_value:
                field_values.append({})
            status_value: dict[str, object] = {
                "name": status,
                "field": {"id": "status-field", "name": "Status"},
            }
            if self.include_status:
                field_values.append(status_value)
            if self.duplicate_status:
                field_values.append(status_value)
            items.append(
                {
                    "id": item_id,
                    "content": {
                        "__typename": "Issue",
                        "id": f"node-{number}",
                        "number": number,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    "fieldValues": {
                        "nodes": field_values,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            )
        return {
            "user": {
                "projectV2": {
                    "id": "project-node",
                    "repositories": {
                        "nodes": [{"nameWithOwner": "owner/repo"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "fields": {"nodes": [{"id": "status-field", "name": "Status"}]},
                    "items": {
                        "nodes": items,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }

    def request_rest(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | list[object]]:
        self.rest_calls.append((method, path, payload))
        if path.endswith("/parent"):
            return 404, {}
        if path.endswith("/sub_issues?per_page=100&page=1"):
            return 200, []
        if method == "GET" and "/issues/" in path:
            number = int(path.split("/issues/", maxsplit=1)[1])
            issue = _rest_issue(number, self.parent_body if number == 1 else "")
            return 200, issue
        raise AssertionError(f"Unexpected REST call: {method} {path}")


def test_provider_preflights_project_parent_and_created_children() -> None:
    client = FakeRelationGitHubClient()
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    snapshot = provider.prepare_pbi_relations(_request())

    assert snapshot.parent.number == 1
    assert [child.number for child in snapshot.children] == [2, 3]
    assert snapshot.parent_by_child == {2: None, 3: None}
    assert not any(method == "POST" for method, _, _ in client.rest_calls)


def test_provider_ignores_unrelated_project_field_values() -> None:
    client = FakeRelationGitHubClient(include_unrelated_field_value=True)
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    snapshot = provider.prepare_pbi_relations(_request())

    assert snapshot.parent.number == 1
    assert [child.number for child in snapshot.children] == [2, 3]
    assert not any(method == "POST" for method, _, _ in client.rest_calls)


def test_provider_fails_closed_when_project_status_is_missing() -> None:
    client = FakeRelationGitHubClient(include_status=False)
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    with pytest.raises(PbiRelationValidationError) as error:
        provider.prepare_pbi_relations(_request())

    assert error.value.code == "parent_not_refined"
    assert not any(method == "POST" for method, _, _ in client.rest_calls)


def test_provider_fails_closed_when_project_status_is_ambiguous() -> None:
    client = FakeRelationGitHubClient(duplicate_status=True)
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    with pytest.raises(PbiRelationProviderError, match="project_item_status_ambiguous"):
        provider.prepare_pbi_relations(_request())

    assert not any(method == "POST" for method, _, _ in client.rest_calls)


def test_provider_reads_parent_from_child_endpoint() -> None:
    client = FakeRelationGitHubClient()
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    assert provider.get_pbi_parent_issue_number("owner/repo", 2) is None
    assert client.rest_calls == [("GET", "/repos/owner/repo/issues/2/parent", None)]


class FakeRestArrayClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def request_rest(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | list[object]]:
        self.calls.append((method, path, payload))
        if method == "GET" and path.endswith("page=1"):
            return 200, [_rest_issue(number) for number in range(2, 102)]
        if method == "GET" and path.endswith("page=2"):
            return 200, [_rest_issue(102)]
        if method == "POST":
            return 201, {}
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        raise AssertionError(f"Unexpected GraphQL query: {query[:40]} {variables}")


def test_provider_paginates_relation_reads_and_uses_native_write_payloads() -> None:
    client = FakeRestArrayClient()
    provider = GitHubProjectProvider(
        owner="owner", project_number=2, token="unused", client=client
    )

    issues = provider.list_pbi_sub_issues("owner/repo", 1)
    provider.add_pbi_sub_issue("owner/repo", 1, 102)
    provider.add_pbi_dependency("owner/repo", 3, 102)

    assert len(issues) == 101
    assert client.calls[0][1].endswith("?per_page=100&page=1")
    assert client.calls[1][1].endswith("?per_page=100&page=2")
    assert client.calls[2] == (
        "POST",
        "/repos/owner/repo/issues/1/sub_issues",
        {"sub_issue_id": 102},
    )
    assert client.calls[3] == (
        "POST",
        "/repos/owner/repo/issues/3/dependencies/blocked_by",
        {"issue_id": 102},
    )


def test_rest_client_accepts_native_relation_array_responses(monkeypatch) -> None:
    class Response:
        status = 200
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b"[]"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "beehaiive.provider.urlopen", lambda *_args, **_kwargs: Response()
    )
    status, payload = UrllibGraphQLClient("not-returned").request_rest(
        "GET", "/repos/owner/repo/issues/1/sub_issues"
    )

    assert status == 200
    assert payload == []
