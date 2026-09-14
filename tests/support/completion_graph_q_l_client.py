from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beehaiive import provider as provider_module

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
