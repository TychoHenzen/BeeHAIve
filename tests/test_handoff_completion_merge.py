from __future__ import annotations

from beehaiive.models import HandoffRequest
from beehaiive.provider import GitHubProjectProvider
from beehaiive.storage import OrchestratorStore
from tests.support.completion_graph_q_l_client import CompletionGraphQLClient

HEAD = "a" * 40

MERGE_COMMIT = "c" * 40

PULL_REQUEST_URL = "https://github.com/owner/api/pull/8"

MERGE_UUID = "12345678-1234-5678-1234-567812345678"


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


def _record_merge_audit(
    store: OrchestratorStore,
    request: HandoffRequest,
    merge_commit_sha: str,
) -> None:
    action_id = store.begin_handoff_mutation(
        request,
        "merge_pull_request",
        f"{request.repository}#8:{HEAD}",
        {
            "pull_request_number": "8",
            "pull_request_url": PULL_REQUEST_URL,
            "head_sha": HEAD,
            "branch": request.branch,
            "base_branch": "main",
            "review_cycle_id": "review-cycle-1",
            "approved_by_human": "false",
        },
    )
    store.finish_handoff_mutation(
        action_id,
        "succeeded",
        {
            "status": "merged",
            "pull_request_id": "owner/api#8",
            "merge_action": "default",
            "head_sha": HEAD,
            "merge_commit_sha": merge_commit_sha,
        },
    )


def test_completion_merges_then_reconciles_issue_ref_and_project() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "completed"
        assert result["pull_request_id"] == "owner/api#8"
        assert result["merge_action"] == "default"
        assert result["merge_commit_sha"] == MERGE_COMMIT
        assert client.calls == ["merge", "close_issue", "delete_ref", "project_done"]
        assert client.ref_exists is False
        assert client.issue_state == "CLOSED"
        assert client.project_status == "Done"
        assert client.rest_calls[0][2] == {"sha": HEAD, "merge_action": "default"}
        actions = store.actions_for_project("owner:7")
        assert all(action["status"] == "succeeded" for action in actions)
        assert {action["kind"] for action in actions} == {
            "github.merge_pull_request",
            "github.close_issue",
            "github.delete_ref",
            "github.update_project_status",
        }
    finally:
        store.close()


def test_merge_queue_completion_resumes_from_the_audited_uuid() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient(queued=True)
        provider = GitHubProjectProvider("owner", 7, "token", client=client)
        request = _request(store)

        pending = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, _authorization()
        )
        assert pending["status"] == "pending"
        assert client.issue_state == "OPEN"
        assert client.calls == ["merge"]

        client.queue_ready = True
        completed = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )

        assert completed["status"] == "completed"
        assert client.calls == ["merge", "close_issue", "delete_ref", "project_done"]
        assert client.rest_calls[-1][0] == "GET"
    finally:
        store.close()


def test_interrupted_merge_request_recovers_missing_uuid_once() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient(queued=True)
        client.omit_merge_uuid_once = True
        provider = GitHubProjectProvider("owner", 7, "token", client=client)
        request = _request(store)

        deferred = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, _authorization()
        )
        assert deferred["status"] == "deferred"
        assert deferred["reason"] == "merge_request_identity_missing"
        no_authorization = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )
        assert no_authorization["status"] == "operator_required"
        assert no_authorization["reason"] == "current_review_authorization_required"
        assert len(client.rest_calls) == 1

        changed_cycle = _authorization()
        changed_cycle["cycle_id"] = "review-cycle-2"
        stale_authorization = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, changed_cycle
        )
        assert stale_authorization["status"] == "operator_required"
        assert stale_authorization["reason"] == "previous_merge_target_changed"
        assert len(client.rest_calls) == 1

        client.queue_ready = True
        completed = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert completed["status"] == "completed"
        assert [call[0] for call in client.rest_calls] == ["PUT", "PUT", "GET"]
        assert all(
            call[2] == {"sha": HEAD, "merge_action": "default"}
            for call in client.rest_calls[:2]
        )
        merge_actions = [
            action
            for action in store.actions_for_project("owner:7")
            if action["kind"] == "github.merge_pull_request"
        ]
        assert sorted(action["request"]["attempt"] for action in merge_actions) == [
            1,
            2,
        ]
        assert sorted(action["status"] for action in merge_actions) == [
            "failed",
            "succeeded",
        ]
    finally:
        store.close()


def test_already_merged_pull_request_must_match_audited_repository() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.merged = True
        client.source_repository = "fork/api"
        request = _request(store)
        _record_merge_audit(store, request, MERGE_COMMIT)
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )

        assert result["status"] == "operator_required"
        assert result["step"] == "merge"
        assert result["reason"] == "merged_without_matching_authorization_audit"
        assert client.calls == []
        assert client.issue_state == "OPEN"
    finally:
        store.close()


def test_already_merged_commit_must_match_audit_result() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.merged = True
        request = _request(store)
        _record_merge_audit(store, request, "e" * 40)
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )

        assert result["status"] == "operator_required"
        assert result["step"] == "merge"
        assert result["reason"] == "merged_commit_audit_conflicts_with_readback"
        assert client.calls == []
        assert client.issue_state == "OPEN"
    finally:
        store.close()


def test_already_merged_async_request_is_verified_before_reconciliation() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.merged = True
        client.queue_ready = True
        request = _request(store)
        action_id = store.begin_handoff_mutation(
            request,
            "merge_pull_request",
            f"{request.repository}#8:{HEAD}",
            {
                "pull_request_number": "8",
                "pull_request_url": PULL_REQUEST_URL,
                "head_sha": HEAD,
                "branch": request.branch,
                "base_branch": "main",
                "review_cycle_id": "review-cycle-1",
                "approved_by_human": "false",
            },
        )
        store.finish_handoff_mutation(
            action_id,
            "succeeded",
            {"merge_request_id": MERGE_UUID, "status": "pending"},
        )
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            request, 8, PULL_REQUEST_URL, HEAD, None
        )

        assert result["status"] == "completed"
        assert client.rest_calls[0][0] == "GET"
        assert all(call[0] != "PUT" for call in client.rest_calls)
        merge_action = next(
            action
            for action in store.actions_for_project("owner:7")
            if action["kind"] == "github.merge_pull_request"
        )
        assert merge_action["result"]["merge_commit_sha"] == MERGE_COMMIT
    finally:
        store.close()
