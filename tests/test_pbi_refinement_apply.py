from __future__ import annotations

import pytest

from beehaiive.pbi_refinement_mutation import (
    REFINEMENT_SECTION_ORDER,
    PbiRefinementMutationError,
    PbiRefinementUpdateRequest,
)
from beehaiive.provider import GitHubProjectProvider
from tests.support.fake_refinement_graph_q_l_client import FakeRefinementGraphQLClient


def _request(*, effort: str = "Effort 5 - Large") -> PbiRefinementUpdateRequest:
    return PbiRefinementUpdateRequest(
        project_id="owner:2",
        repository="owner/repo",
        pbi_number=65,
        sections={name: f"Refined {name}." for name in REFINEMENT_SECTION_ORDER},
        priority_label="Prio 5 - Planned",
        effort_label=effort,
        standard_labels=("enhancement",),
    )


def _provider(client: FakeRefinementGraphQLClient) -> GitHubProjectProvider:
    return GitHubProjectProvider(
        owner="owner", project_number=2, token="test-token", client=client
    )


def test_apply_refinement_preserves_text_replaces_labels_and_retries() -> None:
    client = FakeRefinementGraphQLClient()
    provider = _provider(client)

    result = provider.apply_pbi_refinement(_request())

    assert result.status == "complete"
    assert result.project_status == "Todo"
    assert set(result.labels) == {
        "Prio 5 - Planned",
        "Effort 5 - Large",
        "Effort 13 - Candidate",
        "enhancement",
        "documentation",
    }
    assert client.issue["body"].startswith(
        "Preserve this preamble.\n\n## Notes\nKeep this note.\n"
    )
    assert (
        "## Acceptance criteria\nRefined Acceptance criteria." in client.issue["body"]
    )
    assert client.writes == ["issue", "project_status"]

    replay = provider.apply_pbi_refinement(_request())

    assert replay.status == "complete"
    assert client.writes == ["issue", "project_status"]


def test_effort_13_requires_split_evidence_and_stays_in_backlog() -> None:
    client = FakeRefinementGraphQLClient()
    provider = _provider(client)

    with pytest.raises(PbiRefinementMutationError) as error:
        provider.apply_pbi_refinement(_request(effort="Effort 13 - Epic"))
    assert error.value.code == "missing_split_evidence"
    assert client.writes == []

    client.sub_issues = [{"number": 66, "title": "Child PBI", "state": "OPEN"}]
    result = provider.apply_pbi_refinement(_request(effort="Effort 13 - Epic"))

    assert result.status == "complete"
    assert result.project_status == "Backlog"
    assert [issue["number"] for issue in result.linked_sub_issues] == [66]
    assert client.writes == ["issue"]


def test_partial_status_failure_is_reported_and_retry_only_finishes_pending_step() -> (
    None
):
    client = FakeRefinementGraphQLClient()
    client.fail_status_once = True
    provider = _provider(client)

    partial = provider.apply_pbi_refinement(_request())

    assert partial.status == "partial"
    assert partial.completed_steps == ("issue_body_and_labels",)
    assert partial.pending_step == "project_status"
    assert client.project_status == "Backlog"

    completed = provider.apply_pbi_refinement(_request())

    assert completed.status == "complete"
    assert completed.project_status == "Todo"
    assert client.writes.count("issue") == 1
    assert client.writes.count("project_status") == 2


def test_duplicate_managed_section_fails_before_any_github_write() -> None:
    client = FakeRefinementGraphQLClient()
    client.issue["body"] = "## Outcome\nFirst\n\n## Outcome\nSecond\n"

    with pytest.raises(PbiRefinementMutationError) as error:
        _provider(client).apply_pbi_refinement(_request())

    assert error.value.code == "duplicate_section_heading"
    assert client.writes == []


def test_empty_github_issue_body_can_be_refined() -> None:
    client = FakeRefinementGraphQLClient()
    client.issue["body"] = None

    result = _provider(client).apply_pbi_refinement(_request())

    assert result.status == "complete"
    assert "## Outcome\nRefined Outcome." in client.issue["body"]


def test_repeated_github_pagination_cursors_return_bounded_preflight_failure() -> None:
    for connection in (
        "target_repositories",
        "repository_labels",
        "project_items",
        "issue_labels",
        "issue_sub_issues",
    ):
        client = FakeRefinementGraphQLClient()
        client.repeat_cursor_for = connection

        with pytest.raises(PbiRefinementMutationError) as error:
            _provider(client).apply_pbi_refinement(_request())

        assert error.value.code == "preflight_failed"
        assert client.writes == []


@pytest.mark.parametrize(
    ("connection", "missing_field"),
    [
        ("issue_labels", "nodes"),
        ("issue_labels", "pageInfo"),
        ("issue_sub_issues", "nodes"),
        ("issue_sub_issues", "pageInfo"),
        ("project_items", "nodes"),
        ("project_items", "pageInfo"),
    ],
)
def test_incomplete_issue_connections_fail_preflight_without_writes(
    connection: str, missing_field: str
) -> None:
    client = FakeRefinementGraphQLClient()
    client.malformed_connection_for = (connection, missing_field)

    with pytest.raises(PbiRefinementMutationError) as error:
        _provider(client).apply_pbi_refinement(_request())

    assert error.value.code == "preflight_failed"
    assert client.writes == []
