from beehaiive.provider import (
    GitHubProjectProvider,
)
from tests.support.orchestrator.helpers import (
    readiness_connection as _readiness_connection,
)
from tests.support.orchestrator.readiness_graph_ql_client import (
    ReadinessGraphQLClient as ReadinessGraphQLClient,
)


def test_github_provider_projects_child_facts_and_dependency_pages() -> None:
    children = [
        {
            "number": 2,
            "title": "Ready",
            "issue_state": "OPEN",
            "state_reason": "REOPENED",
            "project_status": "Todo",
        },
        {
            "number": 3,
            "title": "Incomplete",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "In Progress",
        },
        {
            "number": 4,
            "title": "Blocked",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "Todo",
        },
        {
            "number": 5,
            "title": "Rejected",
            "issue_state": "CLOSED",
            "state_reason": "NOT_PLANNED",
            "project_status": "Done",
        },
        {
            "number": 6,
            "title": "Completed",
            "issue_state": "CLOSED",
            "state_reason": "COMPLETED",
            "project_status": "Done",
        },
        {
            "number": 7,
            "title": "Unknown",
            "issue_state": "OPEN",
            "state_reason": None,
            "project_status": "Todo",
        },
    ]
    first_page = [
        {
            "id": 1_000 + index,
            "node_id": f"I_kwDO{index}",
            "number": 1_000 + index,
            "html_url": f"https://example.test/issues/{1_000 + index}",
            "title": f"Completed blocker {index}",
            "state": "closed",
            "state_reason": "completed",
        }
        for index in range(100)
    ]
    final_blocker = {
        "id": 1_100,
        "node_id": "I_kwDO1100",
        "number": 1_100,
        "html_url": "https://example.test/issues/1100",
        "title": "Open blocker",
        "state": "open",
        "state_reason": None,
    }
    client = ReadinessGraphQLClient(
        children,
        dependency_pages={4: [first_page, [final_blocker]]},
        rest_statuses={7: 403},
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")
    parent = discovered.repositories[0].pbis[0]
    metadata = parent.metadata
    projected = {child["number"]: child for child in metadata["subtasks"]}  # type: ignore[index]
    readiness = metadata["dependency_readiness"]  # type: ignore[assignment]

    assert {number: child["readiness"] for number, child in projected.items()} == {
        2: "ready",
        3: "incomplete",
        4: "blocked",
        5: "rejected",
        6: "completed",
        7: "unknown",
    }
    assert readiness["status"] == "unknown"  # type: ignore[index]
    assert readiness["counts"] == {  # type: ignore[index]
        "ready": 1,
        "incomplete": 1,
        "blocked": 1,
        "rejected": 1,
        "completed": 1,
        "unknown": 1,
    }
    assert metadata["issue_state"] == "OPEN"
    assert metadata["project_status"] == "In Progress"
    assert projected[2]["issue_state"] == "OPEN"
    assert projected[2]["state_reason"] == "REOPENED"
    assert projected[2]["project_status"] == "Todo"
    assert projected[2]["blocked_by"] == []
    assert projected[2]["dependency_read_complete"] is True
    assert projected[4]["readiness_reasons"] == ["blocked_by_open:#1100"]
    assert len(projected[4]["blocked_by"]) == 101
    assert projected[7]["dependency_read_error"] == "permission_denied"
    assert [
        path.rsplit("page=", 1)[1]
        for method, path in client.rest_calls
        if method == "GET" and "/issues/4/" in path
    ] == ["1", "2"]
    assert all(method == "GET" for method, _ in client.rest_calls)
    assert not any("mutation" in query.casefold() for query in client.queries)


def test_github_provider_keeps_missing_child_project_status_unknown() -> None:
    client = ReadinessGraphQLClient(
        [
            {
                "number": 2,
                "title": "Not in a Project status",
                "issue_state": "OPEN",
                "state_reason": None,
                "project_status": None,
            }
        ]
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    metadata = provider.discover_project("owner:7").repositories[0].pbis[0].metadata
    child = metadata["subtasks"][0]  # type: ignore[index]

    assert child["project_status"] is None  # type: ignore[index]
    assert child["readiness"] == "unknown"  # type: ignore[index]
    assert child["readiness_reasons"] == ["project_status_unknown"]  # type: ignore[index]


def test_github_provider_does_not_drop_a_malformed_child_from_the_aggregate() -> None:
    client = ReadinessGraphQLClient(
        [
            {
                "number": 2,
                "title": "Ready-looking child",
                "issue_state": "OPEN",
                "state_reason": None,
                "project_status": "Todo",
            }
        ],
        extra_subissues=[
            {
                "number": 8,
                "title": None,
                "state": "OPEN",
                "stateReason": None,
                "labels": _readiness_connection([]),
            }
        ],
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    metadata = provider.discover_project("owner:7").repositories[0].pbis[0].metadata

    assert metadata["subtasks"][0]["readiness"] == "ready"  # type: ignore[index]
    assert metadata["dependency_readiness"]["status"] == "unknown"  # type: ignore[index]
    assert "child_response_incomplete" in metadata["dependency_readiness"]["reasons"]  # type: ignore[index]
