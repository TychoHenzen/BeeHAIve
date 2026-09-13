from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beehaiive import provider as provider_module
from beehaiive.models import HandoffRequest
from beehaiive.provider import GitHubProjectProvider
from beehaiive.storage import OrchestratorStore

HEAD = "a" * 40
MERGE_COMMIT = "c" * 40
PULL_REQUEST_URL = "https://github.com/owner/api/pull/8"
MERGE_UUID = "12345678-1234-5678-1234-567812345678"


class CompletionGraphQLClient:
    def __init__(
        self, *, check_conclusion: str = "SUCCESS", queued: bool = False
    ) -> None:
        self.merged = False
        self.is_draft = False
        self.source_repository = "owner/api"
        self.source_head: str | None = HEAD
        self.ref_head = HEAD
        self.issue_state = "OPEN"
        self.ref_exists = True
        self.project_status = "In Progress"
        self.check_conclusion = check_conclusion
        self.queued = queued
        self.queue_ready = False
        self.omit_merge_uuid_once = False
        self.move_ref_before_delete = False
        self.calls: list[str] = []
        self.rest_calls: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.merge_failures: list[Exception] = []
        self.close_issue_failures: list[Exception] = []
        self.fail_ref_readback_once = False

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if query == provider_module.PULL_REQUEST_STATE_QUERY:
            return {
                "repository": {
                    "pullRequest": {
                        "id": "PR_node_8",
                        "number": 8,
                        "url": PULL_REQUEST_URL,
                        "state": "CLOSED" if self.merged else "OPEN",
                        "merged": self.merged,
                        "isDraft": self.is_draft,
                        "mergeCommit": {"oid": MERGE_COMMIT} if self.merged else None,
                        "headRefName": "codex/47-completion",
                        "headRepository": {"nameWithOwner": self.source_repository},
                        "headRef": {
                            "name": "codex/47-completion",
                            "target": {"oid": self.source_head},
                        },
                        "baseRefName": "main",
                        "baseRef": {"name": "main", "target": {"oid": "b" * 40}},
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                    }
                }
            }
        if query == provider_module.PULL_REQUEST_CHECKS_QUERY:
            return {
                "repository": {
                    "pullRequest": {
                        "state": "OPEN",
                        "headRef": {"target": {"oid": HEAD}},
                        "statusCheckRollup": {
                            "commit": {"oid": HEAD},
                            "state": self.check_conclusion,
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "ci",
                                        "status": "COMPLETED",
                                        "conclusion": self.check_conclusion,
                                        "isRequired": True,
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            },
                        },
                    }
                }
            }
        if query == provider_module.ISSUE_COMPLETION_QUERY:
            return {
                "repository": {
                    "issue": {"id": "ISSUE_47", "number": 47, "state": self.issue_state}
                }
            }
        if query == provider_module.CLOSE_ISSUE_MUTATION:
            self.calls.append("close_issue")
            if self.close_issue_failures:
                raise self.close_issue_failures.pop(0)
            self.issue_state = "CLOSED"
            return {
                "closeIssue": {
                    "issue": {"id": "ISSUE_47", "number": 47, "state": "CLOSED"}
                }
            }
        if query == provider_module.BRANCH_REF_QUERY:
            if not self.ref_exists and self.fail_ref_readback_once:
                self.fail_ref_readback_once = False
                raise provider_module.GitHubOutcomeUnknownError("readback unavailable")
            ref = (
                {
                    "id": "REF_47",
                    "name": "refs/heads/codex/47-completion",
                    "target": {"oid": self.ref_head},
                }
                if self.ref_exists
                else None
            )
            return {"repository": {"id": "REPOSITORY_NODE", "ref": ref}}
        if query == provider_module.UPDATE_REFS_MUTATION:
            self.calls.append("delete_ref")
            if self.move_ref_before_delete:
                self.move_ref_before_delete = False
                self.ref_head = "d" * 40
            input_record = variables.get("input")
            assert isinstance(input_record, Mapping)
            updates = input_record.get("refUpdates")
            assert isinstance(updates, list) and updates
            update = updates[0]
            assert isinstance(update, Mapping)
            if (
                input_record.get("repositoryId") != "REPOSITORY_NODE"
                or update.get("name") != "refs/heads/codex/47-completion"
                or update.get("beforeOid") != self.ref_head
                or update.get("afterOid") != "0" * 40
            ):
                raise provider_module.ProviderError("source ref changed before delete")
            self.ref_exists = False
            return {"updateRefs": {"clientMutationId": None}}
        if query == provider_module.COMPLETION_PROJECT_QUERY:
            option_id = (
                "done-option" if self.project_status == "Done" else "progress-option"
            )
            return {
                "user": {
                    "projectV2": {
                        "id": "PROJECT_NODE",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "status-field",
                                    "name": "Status",
                                    "options": [
                                        {
                                            "id": "progress-option",
                                            "name": "In Progress",
                                        },
                                        {"id": "done-option", "name": "Done"},
                                    ],
                                }
                            ]
                        },
                        "items": {
                            "nodes": [
                                {
                                    "id": "PROJECT_ITEM_47",
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 47,
                                        "repository": {"nameWithOwner": "owner/api"},
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": self.project_status,
                                                "optionId": option_id,
                                                "field": {
                                                    "id": "status-field",
                                                    "name": "Status",
                                                },
                                            }
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        if query == provider_module.UPDATE_PROJECT_STATUS_MUTATION:
            self.calls.append("project_done")
            self.project_status = "Done"
            return {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "PROJECT_ITEM_47"}
                }
            }
        raise AssertionError(f"Unexpected GraphQL query: {query[:60]}")

    def request_rest(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        self.rest_calls.append((method, path, payload))
        if method == "PUT":
            self.calls.append("merge")
            if self.merge_failures:
                raise self.merge_failures.pop(0)
            if self.queued:
                details: dict[str, object] = {
                    "uuid": MERGE_UUID,
                    "expected_head_sha": HEAD,
                    "merge_action": "default",
                    "merge_method": "merge",
                }
                if self.omit_merge_uuid_once:
                    self.omit_merge_uuid_once = False
                    details.pop("uuid")
                return 202, {
                    "status": "pending",
                    "details": details,
                }
            self.merged = True
            return 200, {"status": "merged", "details": {"sha": MERGE_COMMIT}}
        if method == "GET" and self.queue_ready:
            self.merged = True
            return 200, {
                "status": "merged",
                "details": {
                    "uuid": MERGE_UUID,
                    "expected_head_sha": HEAD,
                    "merge_action": "default",
                    "merge_method": "merge",
                    "sha": MERGE_COMMIT,
                },
            }
        return 200, {
            "status": "pending",
            "details": {
                "uuid": MERGE_UUID,
                "expected_head_sha": HEAD,
                "merge_action": "default",
                "merge_method": "merge",
            },
        }


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
