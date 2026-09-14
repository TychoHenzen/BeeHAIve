from __future__ import annotations

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


def test_nonpassing_current_head_checks_block_all_mutations() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient(check_conclusion="FAILURE")
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result == {
            "status": "operator_required",
            "step": "merge",
            "reason": "current_head_checks_not_passing",
        }
        assert client.rest_calls == []
        assert client.calls == []
        assert store.actions_for_project("owner:7") == []
    finally:
        store.close()


def test_draft_pull_request_blocks_merge_mutation() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.is_draft = True
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result == {
            "status": "operator_required",
            "reason": "pull_request_draft_or_source_repository_unproven",
            "step": "merge",
        }
        assert client.rest_calls == []
        assert client.calls == []
    finally:
        store.close()


def test_pull_request_from_another_repository_blocks_all_mutations() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.source_repository = "fork/api"
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "operator_required"
        assert result["reason"] == "pull_request_source_repository_mismatch"
        assert client.rest_calls == []
        assert client.calls == []
    finally:
        store.close()


def test_persisted_head_mismatch_blocks_provider_io() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, "d" * 40, _authorization()
        )

        assert result == {
            "status": "operator_required",
            "reason": "persisted_handoff_identity_incomplete",
        }
        assert client.calls == []
        assert client.rest_calls == []
    finally:
        store.close()


def test_source_ref_head_drift_stops_before_deletion_and_project_update() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.ref_head = "d" * 40
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "operator_required"
        assert result["step"] == "source_ref"
        assert client.calls == ["merge", "close_issue"]
        assert client.project_status == "In Progress"
    finally:
        store.close()


def test_source_ref_compare_and_delete_preserves_concurrent_push() -> None:
    store = OrchestratorStore()
    try:
        client = CompletionGraphQLClient()
        client.move_ref_before_delete = True
        provider = GitHubProjectProvider("owner", 7, "token", client=client)

        result = provider.complete_approved_handoff(
            _request(store), 8, PULL_REQUEST_URL, HEAD, _authorization()
        )

        assert result["status"] == "operator_required"
        assert result["step"] == "source_ref"
        assert client.ref_exists is True
        assert client.ref_head == "d" * 40
        assert client.project_status == "In Progress"
        assert client.calls == ["merge", "close_issue", "delete_ref"]
    finally:
        store.close()
