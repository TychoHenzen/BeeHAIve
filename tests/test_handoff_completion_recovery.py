from __future__ import annotations

from beehaiive import provider as provider_module
from beehaiive.models import HandoffRequest
from beehaiive.provider import GitHubProjectProvider
from beehaiive.storage import OrchestratorStore
from tests.support.completion_graph_q_l_client import CompletionGraphQLClient

HEAD = "a" * 40

PULL_REQUEST_URL = "https://github.com/owner/api/pull/8"


def _request(store: OrchestratorStore) -> HandoffRequest:
    return HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=47,
        title="Completion flow",
        branch="codex/47-completion",
        base_branch="main",
        body="Implementation summary",
        run_id="run-47",
        head_sha=HEAD,
        mutation_audit=store,
    )


def _authorization() -> dict[str, object]:
    return {
        "pull_request_id": "owner/api#8",
        "cycle_id": "review-cycle-1",
        "head_sha": HEAD,
        "approved_by_human": False,
    }


def test_transient_graphql_issue_close_failure_retries_once() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.close_issue_failures.append(
            provider_module.GitHubOutcomeUnknownError(
                "transient GraphQL server error", status_code=503
            )
        )
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "completed"
        assert client.calls == [
            "merge",
            "close_issue",
            "close_issue",
            "delete_ref",
            "project_done",
        ]
        close_actions = [
            action
            for action in store.actions_for_project("owner:7")
            if action["kind"] == "github.close_issue"
        ]
        assert sorted(action["request"]["attempt"] for action in close_actions) == [
            1,
            2,
        ]
        assert sorted(action["status"] for action in close_actions) == [
            "failed",
            "succeeded",
        ]
    finally:
        store.close()


def test_absent_source_ref_is_confirmed_without_delete_mutation() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.ref_exists = False
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "completed"
        assert client.calls == ["merge", "close_issue", "project_done"]
        assert all(
            action["status"] == "succeeded"
            for action in store.actions_for_project("owner:7")
        )
    finally:
        store.close()


def test_restart_after_unavailable_ref_readback_does_not_repeat_delete() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.fail_ref_readback_once = True
        provider = GitHubProjectProvider("owner", 7, "token", client=client)
        request = _request(store)

        deferred = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert deferred["status"] == "deferred"
        assert deferred["step"] == "source_ref"
        assert client.project_status == "In Progress"

        resumed = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )

        assert resumed["status"] == "completed"
        assert client.calls == ["merge", "close_issue", "delete_ref", "project_done"]
    finally:
        store.close()


def test_transient_merge_failure_retries_once_after_negative_readback() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.merge_failures = [
            provider_module.GitHubOutcomeUnknownError(
                "transient server error", status_code=503
            )
        ]
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "completed"
        assert client.calls == [
            "merge",
            "merge",
            "close_issue",
            "delete_ref",
            "project_done",
        ]
        merge_actions = [
            action
            for action in store.actions_for_project("owner:7")
            if action["kind"] == "github.merge_pull_request"
        ]
        assert sorted(action["request"]["attempt"] for action in merge_actions) == [
            1,
            2,
        ]
    finally:
        store.close()


def test_store_resolves_exact_persisted_pull_request_handoff() -> None:
    store = OrchestratorStore()
    try:
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO projects(project_id, name, updated_at) VALUES (?, ?, ?)",
                ("owner:7", "Project", "now"),
            )
            connection.execute(
                "INSERT INTO repositories(project_id, name) VALUES (?, ?)",
                ("owner:7", "owner/api"),
            )
            connection.execute(
                """
                INSERT INTO pbis(
                    project_id, repository_name, number, title, stage, branch,
                    pull_request_url, handoff_base_branch, handoff_body,
                    handoff_head_sha, handoff_verification_evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "owner:7",
                    "owner/api",
                    47,
                    "Completion flow",
                    "pull_request",
                    "codex/47-completion",
                    PULL_REQUEST_URL,
                    "main",
                    "Implementation summary",
                    HEAD,
                    '{"outcome":"pass"}',
                ),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, project_id, repository_name, pbi_number, status,
                    attempt, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("run-47", "owner:7", "owner/api", 47, "completed", 1, "now"),
            )
            connection.execute(
                """
                INSERT INTO handoffs(
                    run_id, project_id, repository_name, pbi_number, branch,
                    pull_request_url, pull_request_number, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run-47",
                    "owner:7",
                    "owner/api",
                    47,
                    "codex/47-completion",
                    PULL_REQUEST_URL,
                    8,
                    "now",
                ),
            )

        request, url = store.handoff_for_pull_request("owner/api", 8)

        assert request.pbi_number == 47
        assert request.run_id == "run-47"
        assert request.head_sha == HEAD
        assert request.mutation_audit is store
        assert url == PULL_REQUEST_URL
    finally:
        store.close()
