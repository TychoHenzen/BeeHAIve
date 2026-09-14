import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubProjectProvider,
)
from tests.support.orchestrator.check_graph_ql_client import (
    CheckGraphQLClient as CheckGraphQLClient,
)
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)
from tests.support.orchestrator.nested_metadata_graph_ql_client import (
    NestedMetadataGraphQLClient as NestedMetadataGraphQLClient,
)


def test_github_provider_paginates_current_pull_request_checks() -> None:
    client = CheckGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    issue = {
        "repository": {"nameWithOwner": "owner/api"},
        "number": 1,
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "subIssues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "closedByPullRequestsReferences": {
            "nodes": [
                {
                    "number": 9,
                    "state": "OPEN",
                    "headRef": {"target": {"oid": "head-1"}},
                    "reviewRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "latestReviews": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }

    completed = provider._complete_issue_metadata(issue)
    metadata = provider_module._dashboard_metadata(completed)
    checks = metadata["pull_requests"][0]["checks"]  # type: ignore[index]

    assert client.cursors == [None, "checks-1"]
    assert checks["head_sha"] == "head-1"  # type: ignore[index]
    assert checks["verdict"] == "passing"  # type: ignore[index]
    assert [context["kind"] for context in checks["contexts"]] == [  # type: ignore[index]
        "check_run",
        "status_context",
    ]
    assert metadata["checks"]["verdict"] == "passing"  # type: ignore[index]


def test_github_provider_requires_current_head_evidence() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=CheckGraphQLClient(observed_head=None)
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")
    missing_head = provider._complete_pull_request_checks("owner", "api", 9, None)

    assert checks["head_sha"] is None
    assert checks["verdict"] == "unproven"
    assert missing_head["verdict"] == "unproven"


def test_github_provider_rejects_head_change_during_check_pagination() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(page_observed_head="head-2"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["head_sha"] == "head-2"
    assert checks["verdict"] == "unproven"


def test_github_provider_rejects_pull_request_closed_during_check_poll() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(observed_state="MERGED"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["verdict"] == "unproven"


def test_github_provider_check_error_stays_unproven() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=CheckGraphQLClient(error="GitHub GraphQL rate limit exceeded"),
    )

    checks = provider._complete_pull_request_checks("owner", "api", 9, "head-1")

    assert checks["verdict"] == "unproven"
    assert checks["head_sha"] is None
    assert checks["error"] == "GitHub GraphQL rate limit exceeded"


def test_github_provider_does_not_merge_active_pull_request_check_verdicts() -> None:
    metadata = provider_module._dashboard_metadata(
        {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": 9,
                        "state": "OPEN",
                        "checks": {
                            "head_sha": "head-9",
                            "verdict": "passing",
                            "contexts": [],
                        },
                    },
                    {
                        "number": 10,
                        "state": "OPEN",
                        "checks": {
                            "head_sha": "head-10",
                            "verdict": "unproven",
                            "contexts": [],
                        },
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    )

    checks = metadata["checks"]
    assert checks["verdict"] == "unproven"  # type: ignore[index]
    assert [
        check["head_sha"]
        for check in checks["pull_requests"]  # type: ignore[index]
    ] == ["head-9", "head-10"]
    assert [
        check["number"]
        for check in checks["pull_requests"]  # type: ignore[index]
    ] == [9, 10]


def test_github_provider_marks_no_active_pull_request_unproven() -> None:
    metadata = provider_module._dashboard_metadata(
        {"closedByPullRequestsReferences": {"nodes": []}}
    )

    assert metadata["checks"] == {"verdict": "unproven", "pull_requests": []}


def test_github_provider_ignores_incomplete_issue_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=NestedMetadataGraphQLClient()
    )
    issue = {"repository": {}, "number": 1}

    assert provider._complete_issue_metadata(issue) is issue


def test_github_provider_reuses_existing_branch_and_pull_request() -> None:
    client = HandoffGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Closes #1",
        run_id="run-1",
    )

    first = provider.create_handoff(request)
    second = provider.create_handoff(request)

    assert first == second
    assert client.ref_creations == 1
    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 1
    assert client.pull_request_bases == ["main"]
