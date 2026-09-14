from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from beehaiive.github.constants import (
    PROVIDER_REQUEST_TIMEOUT as PROVIDER_REQUEST_TIMEOUT,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.handoff_helpers import (
    _validate_branch_name as _validate_branch_name,
)
from beehaiive.github.queries.completion import (
    CLOSE_ISSUE_MUTATION as CLOSE_ISSUE_MUTATION,
)
from beehaiive.github.queries.completion import (
    UPDATE_PROJECT_STATUS_MUTATION as UPDATE_PROJECT_STATUS_MUTATION,
)
from beehaiive.github.queries.completion import (
    UPDATE_REFS_MUTATION as UPDATE_REFS_MUTATION,
)
from beehaiive.models import HandoffRequest, PullRequestSnapshot


class HandoffCompleteMixin:
    def complete_approved_handoff(
        self: Any,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """Merge only an authorized head, then reconcile issue, ref, and Project."""

        expected_head = expected_head.strip().lower()
        if (
            pull_request_number <= 0
            or not expected_head
            or not pull_request_url.strip()
            or request.base_branch is None
            or request.head_sha is None
            or expected_head != request.head_sha.strip().lower()
        ):
            return {
                "status": "operator_required",
                "reason": "persisted_handoff_identity_incomplete",
            }
        try:
            merge = self._merge_handoff(
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
                authorization,
            )
        except ProviderError as exc:
            return {
                "status": self._provider_failure_status(exc),
                "step": "merge",
                "error_class": type(exc).__name__,
            }
        if merge.get("status") != "merged":
            return {**merge, "step": "merge"}

        prefix = f"{request.repository}#{request.pbi_number}"
        steps: dict[str, object] = {}
        issue_target = {"issue_number": str(request.pbi_number)}

        def issue_is_closed(state: Mapping[str, object]) -> bool:
            return state.get("state") == "CLOSED"

        def issue_is_open(state: Mapping[str, object]) -> bool:
            return state.get("state") == "OPEN"

        def close_issue(state: Mapping[str, object]) -> Mapping[str, object]:
            return self._execute_completion_mutation(
                CLOSE_ISSUE_MUTATION,
                {"input": {"issueId": state["issue_id"]}},
            )

        issue_state = self._ensure_completion_step(
            request,
            "close_issue",
            prefix,
            issue_target,
            lambda: self._issue_completion_state(request),
            issue_is_closed,
            issue_is_open,
            close_issue,
        )
        steps["issue"] = issue_state
        if issue_state.get("status") != "completed":
            return {
                "status": issue_state.get("status"),
                "step": "issue",
                "steps": steps,
            }

        expected_head = expected_head.strip().lower()

        def source_ref_deleted(state: Mapping[str, object]) -> bool:
            return state.get("exists") is False

        def source_ref_matches(state: Mapping[str, object]) -> bool:
            return (
                state.get("exists") is True and state.get("head_sha") == expected_head
            )

        def delete_source_ref(state: Mapping[str, object]) -> Mapping[str, object]:
            return self._execute_completion_mutation(
                UPDATE_REFS_MUTATION,
                {
                    "input": {
                        "repositoryId": state["repository_id"],
                        "refUpdates": [
                            {
                                "name": state["ref_name"],
                                "beforeOid": expected_head,
                                "afterOid": "0" * 40,
                            }
                        ],
                    }
                },
            )

        ref_state = self._ensure_completion_step(
            request,
            "delete_ref",
            f"{request.repository}:{request.branch}:{expected_head}",
            {
                "branch": request.branch,
                "head_sha": expected_head,
            },
            lambda: self._source_ref_completion_state(request, expected_head),
            source_ref_deleted,
            source_ref_matches,
            delete_source_ref,
        )
        steps["source_ref"] = ref_state
        if ref_state.get("status") != "completed":
            return {
                "status": ref_state.get("status"),
                "step": "source_ref",
                "steps": steps,
            }

        project_state = self._project_completion_state(request)
        if project_state.get("valid") is not True:
            return {
                "status": "operator_required",
                "step": "project_status",
                "reason": project_state.get("error", "Project state is unproven"),
                "steps": steps,
            }
        project_target = {
            "project_item_id": cast(str, project_state["item_id"]),
            "field_id": cast(str, project_state["field_id"]),
            "option_id": cast(str, project_state["done_option_id"]),
            "status": "Done",
        }

        def project_status_is_done(state: Mapping[str, object]) -> bool:
            return state.get("status") == "Done"

        def project_status_is_in_progress(state: Mapping[str, object]) -> bool:
            return state.get("status") == "In Progress"

        def set_project_status(state: Mapping[str, object]) -> Mapping[str, object]:
            return self._execute_completion_mutation(
                UPDATE_PROJECT_STATUS_MUTATION,
                {
                    "input": {
                        "projectId": state["project_id"],
                        "itemId": state["item_id"],
                        "fieldId": state["field_id"],
                        "value": {"singleSelectOptionId": state["done_option_id"]},
                    }
                },
            )

        project_result = self._ensure_completion_step(
            request,
            "update_project_status",
            prefix,
            project_target,
            lambda: self._project_completion_state(request),
            project_status_is_done,
            project_status_is_in_progress,
            set_project_status,
        )
        steps["project_status"] = project_result
        if project_result.get("status") != "completed":
            return {
                "status": project_result.get("status"),
                "step": "project_status",
                "steps": steps,
            }
        return {
            **merge,
            "status": "completed",
            "merged": True,
            "pull_request_url": pull_request_url,
            "head_sha": expected_head,
            "steps": steps,
        }

    def update_source_branch(
        self: Any,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        if snapshot.source_head != expected_head:
            raise ProviderError("Pull-request source head changed before update")
        if (
            snapshot.state != "OPEN"
            or snapshot.merged
            or not snapshot.source_branch
            or not expected_head.strip()
            or not repaired_head.strip()
        ):
            raise ProviderError("Pull request is not eligible for source-branch update")
        _validate_branch_name(snapshot.source_branch)
        workspace = Path(worktree).resolve()
        try:
            head_result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
            if (
                head_result.returncode != 0
                or head_result.stdout.strip() != repaired_head
            ):
                raise ProviderError("Repair worktree head does not match repaired head")
            ancestry = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    repaired_head,
                ],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
            if ancestry.returncode != 0:
                raise ProviderError("Repair commit does not preserve source history")
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "push",
                    "--porcelain",
                    f"--force-with-lease=refs/heads/{snapshot.source_branch}:{expected_head}",
                    "origin",
                    f"{repaired_head}:refs/heads/{snapshot.source_branch}",
                ],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("Guarded source-branch update failed") from exc
        if result.returncode != 0:
            raise ProviderError(
                "Guarded source-branch update rejected with exit code "
                f"{result.returncode}"
            )


__all__ = ["HandoffCompleteMixin"]
