from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from beehaiive.github.queries.completion import (
    UPDATE_PROJECT_STATUS_MUTATION as UPDATE_PROJECT_STATUS_MUTATION,
)
from beehaiive.github.queries.pbi_refinement import (
    UPDATE_REFINEMENT_ISSUE_MUTATION as UPDATE_REFINEMENT_ISSUE_MUTATION,
)
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementUpdateRequest,
    PbiRefinementUpdateResult,
    is_pbi_refinement_scale_label,
    merge_refinement_sections,
)


# ponytail: keep mutation phases together until retry tests prove safe seams.
class PbiRefinementApplyMixin:
    def apply_pbi_refinement(
        self: Any, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult:
        request.validate()
        try:
            target = self._prepare_pbi_refinement_target(request)
            labels_by_name = self._pbi_refinement_repository_labels(request)
            issue = self._pbi_refinement_issue_state(request)
            project_item = self._pbi_refinement_project_item(request, target, issue)
        except PbiRefinementMutationError:
            raise
        except Exception as exc:
            raise PbiRefinementMutationError(
                "GitHub refinement preflight failed",
                code="preflight_failed",
                status_code=502,
            ) from exc

        if issue.get("state") != "OPEN":
            raise PbiRefinementMutationError(
                "Only an open issue can be refined",
                code="issue_not_open",
                status_code=409,
            )
        issue_url = issue.get("url")
        issue_id = issue.get("id")
        if (
            not isinstance(issue_url, str)
            or not issue_url
            or not isinstance(issue_id, str)
        ):
            raise PbiRefinementMutationError(
                "GitHub issue identity is incomplete",
                code="issue_identity_unavailable",
                status_code=409,
            )

        initial_sub_issues = self._pbi_refinement_sub_issues(issue)
        is_epic = request.effort_label.casefold() == "effort 13 - epic"
        if is_epic and not initial_sub_issues:
            raise PbiRefinementMutationError(
                "Effort 13 Epic requires linked split evidence",
                code="missing_split_evidence",
                status_code=409,
            )
        desired_status = target.backlog_status if is_epic else target.todo_status
        allowed_statuses = {
            target.backlog_status.casefold(),
            target.todo_status.casefold(),
        }
        project_status = project_item.get("status")
        if not isinstance(project_status, str):
            raise PbiRefinementMutationError(
                "PBI Project Status is incomplete",
                code="project_item_status_unavailable",
                status_code=409,
            )
        if project_status.casefold() not in allowed_statuses:
            raise PbiRefinementMutationError(
                "PBI Project Status changed outside Backlog and Todo",
                code="project_status_conflict",
                status_code=409,
            )

        current_body = issue.get("body")
        if current_body is None:
            current_body = ""
        if not isinstance(current_body, str):
            raise PbiRefinementMutationError(
                "GitHub issue body is invalid",
                code="issue_body_unavailable",
                status_code=409,
            )
        try:
            desired_body = merge_refinement_sections(current_body, request.sections)
            actual_label_names = self._pbi_refinement_label_names(issue)
            resolved_labels = [
                self._resolve_pbi_refinement_label(
                    labels_by_name, label, require_description=True
                )
                for label in (
                    request.priority_label,
                    request.effort_label,
                    *request.standard_labels,
                )
            ]
            preserved_labels = [
                self._resolve_pbi_refinement_label(
                    labels_by_name, label, require_description=False
                )
                for label in actual_label_names
                if not is_pbi_refinement_scale_label(label)
            ]
        except PbiRefinementMutationError:
            raise
        except Exception as exc:
            raise PbiRefinementMutationError(
                "Issue labels or body are invalid",
                code="issue_state_invalid",
                status_code=409,
            ) from exc

        expected_labels_by_id = {
            label_id: label_name
            for label_id, label_name in (*preserved_labels, *resolved_labels)
        }
        expected_label_ids = sorted(expected_labels_by_id)
        expected_label_names = {
            name.casefold() for name in expected_labels_by_id.values()
        }
        initial_label_names = {name.casefold() for name in actual_label_names}
        completed_steps: list[str] = []

        def result(
            status: Literal["complete", "partial"],
            current_issue: Mapping[str, Any],
            current_project: Mapping[str, object],
            *,
            pending_step: str | None = None,
            failure_code: str | None = None,
        ) -> PbiRefinementUpdateResult:
            try:
                readback_labels = tuple(
                    sorted(
                        self._pbi_refinement_label_names(current_issue),
                        key=str.casefold,
                    )
                )
            except PbiRefinementMutationError:
                readback_labels = ()
            try:
                linked_sub_issues = self._pbi_refinement_sub_issues(current_issue)
            except PbiRefinementMutationError:
                linked_sub_issues = initial_sub_issues
            return PbiRefinementUpdateResult(
                status=status,
                issue_number=request.pbi_number,
                issue_url=issue_url,
                labels=readback_labels,
                project_item_id=str(current_project.get("item_id", "")),
                project_status=str(current_project.get("status", "")),
                linked_sub_issues=linked_sub_issues,
                completed_steps=tuple(completed_steps),
                pending_step=pending_step,
                failure_code=failure_code,
            )

        needs_issue_update = (
            current_body != desired_body or initial_label_names != expected_label_names
        )
        if needs_issue_update:
            try:
                current_issue = self._pbi_refinement_issue_state(request)
            except Exception:
                return result(
                    "partial",
                    issue,
                    project_item,
                    pending_step="issue_body_and_labels",
                    failure_code="issue_prewrite_readback_failed",
                )
            current_issue_body = current_issue.get("body")
            if current_issue_body is None:
                current_issue_body = ""
            if (
                current_issue.get("state") != "OPEN"
                or current_issue_body != current_body
                or {
                    name.casefold()
                    for name in self._pbi_refinement_label_names(current_issue)
                }
                != initial_label_names
                or self._pbi_refinement_sub_issues(current_issue) != initial_sub_issues
            ):
                raise PbiRefinementMutationError(
                    "Issue changed during refinement validation; "
                    "retry against live state",
                    code="issue_changed_before_write",
                    status_code=409,
                )
            try:
                self._client.execute(
                    UPDATE_REFINEMENT_ISSUE_MUTATION,
                    {
                        "input": {
                            "id": issue_id,
                            "body": desired_body,
                            "labelIds": expected_label_ids,
                        }
                    },
                )
            except Exception:
                try:
                    current_issue = self._pbi_refinement_issue_state(request)
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_update_unconfirmed",
                    )
                if not self._pbi_refinement_issue_matches(
                    current_issue,
                    desired_body,
                    expected_label_names,
                    initial_sub_issues,
                ):
                    return result(
                        "partial",
                        current_issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_update_unconfirmed",
                    )
                issue = current_issue
            else:
                try:
                    issue = self._pbi_refinement_issue_state(request)
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_readback_failed",
                    )
                if not self._pbi_refinement_issue_matches(
                    issue, desired_body, expected_label_names, initial_sub_issues
                ):
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_readback_mismatch",
                    )
        completed_steps.append("issue_body_and_labels")

        try:
            current_project = self._pbi_refinement_project_item(request, target, issue)
            current_issue = self._pbi_refinement_issue_state(request)
        except Exception:
            return result(
                "partial",
                issue,
                project_item,
                pending_step="project_status",
                failure_code="project_prewrite_readback_failed",
            )
        if (
            current_project.get("item_id") != project_item.get("item_id")
            or not isinstance(current_project.get("status"), str)
            or cast(str, current_project.get("status")).casefold()
            not in allowed_statuses
            or not self._pbi_refinement_issue_matches(
                current_issue, desired_body, expected_label_names, initial_sub_issues
            )
        ):
            return result(
                "partial",
                current_issue,
                current_project,
                pending_step="project_status",
                failure_code="project_prewrite_state_changed",
            )
        issue = current_issue
        project_item = current_project

        if project_item["status"] != desired_status:
            target_option_id = (
                target.backlog_option_id
                if desired_status == target.backlog_status
                else target.todo_option_id
            )
            try:
                self._client.execute(
                    UPDATE_PROJECT_STATUS_MUTATION,
                    {
                        "input": {
                            "projectId": target.project_node_id,
                            "itemId": project_item["item_id"],
                            "fieldId": target.status_field_id,
                            "value": {"singleSelectOptionId": target_option_id},
                        }
                    },
                )
            except Exception:
                try:
                    current_project = self._pbi_refinement_project_item(
                        request, target, issue
                    )
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="project_status",
                        failure_code="project_status_unconfirmed",
                    )
                if current_project.get("status") != desired_status:
                    return result(
                        "partial",
                        issue,
                        current_project,
                        pending_step="project_status",
                        failure_code="project_status_unconfirmed",
                    )
                project_item = current_project
            else:
                try:
                    project_item = self._pbi_refinement_project_item(
                        request, target, issue
                    )
                except Exception:
                    return result(
                        "partial",
                        issue,
                        current_project,
                        pending_step="project_status",
                        failure_code="project_status_readback_failed",
                    )
                if project_item.get("status") != desired_status:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="project_status",
                        failure_code="project_status_readback_mismatch",
                    )
        completed_steps.append("project_status")

        try:
            issue = self._pbi_refinement_issue_state(request)
            project_item = self._pbi_refinement_project_item(request, target, issue)
        except Exception:
            return result(
                "partial",
                issue,
                project_item,
                pending_step="final_readback",
                failure_code="final_readback_failed",
            )
        if (
            issue.get("state") != "OPEN"
            or not self._pbi_refinement_issue_matches(
                issue, desired_body, expected_label_names, initial_sub_issues
            )
            or project_item.get("item_id") != current_project.get("item_id")
            or project_item.get("status") != desired_status
        ):
            return result(
                "partial",
                issue,
                project_item,
                pending_step="final_readback",
                failure_code="final_readback_mismatch",
            )
        return result("complete", issue, project_item)


__all__ = ["PbiRefinementApplyMixin"]
