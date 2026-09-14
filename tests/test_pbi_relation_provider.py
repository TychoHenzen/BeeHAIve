from __future__ import annotations

import pytest

from beehaiive.pbi_relations import (
    PbiCreatedIssueReference,
    PbiRelationDependency,
    PbiRelationProviderError,
    PbiRelationRequest,
    PbiRelationValidationError,
)
from beehaiive.provider import GitHubProjectProvider
from tests.support.fake_relation_git_hub_client import FakeRelationGitHubClient
from tests.support.fake_rest_array_client import FakeRestArrayClient


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
