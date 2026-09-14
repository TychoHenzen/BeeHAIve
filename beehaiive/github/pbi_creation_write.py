from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.queries.completion import (
    UPDATE_PROJECT_STATUS_MUTATION as UPDATE_PROJECT_STATUS_MUTATION,
)
from beehaiive.github.queries.pbi_refinement import (
    ADD_ISSUE_LABELS_MUTATION as ADD_ISSUE_LABELS_MUTATION,
)
from beehaiive.github.queries.pbi_refinement import (
    ADD_PROJECT_ITEM_MUTATION as ADD_PROJECT_ITEM_MUTATION,
)
from beehaiive.pbi_creation import (
    PbiCreationError,
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationTarget,
)


class PbiCreationWriteMixin:
    def create_pbi(
        self: Any,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        """Create once, reconcile known issue state, and verify the Project item."""

        owner, name = self._repository_parts(request.repository)
        current = progress

        def save_progress(
            step: str,
            *,
            completed: bool = False,
            **changes: object,
        ) -> None:
            nonlocal current
            completed_steps = list(current.completed_steps)
            if completed and step not in completed_steps:
                completed_steps.append(step)
            current = replace(
                current,
                current_step=step,
                completed_steps=tuple(completed_steps),
                **changes,
            )
            checkpoint(current)

        if current.issue_id is None:
            if current.issue_create_started:
                raise PbiCreationError(
                    "Issue creation outcome is unknown",
                    code="outcome_unknown",
                    status_code=202,
                    unknown_outcome=True,
                )
            save_progress("create_issue", issue_create_started=True)
            status, created = self._rest_request(
                "POST",
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues",
                {
                    "title": request.title,
                    "body": request.body,
                    "labels": list(request.labels),
                },
            )
            if status != 201:
                save_progress("create_issue", issue_create_started=False)
                raise PbiCreationError(
                    "GitHub rejected issue creation",
                    code="issue_create_rejected",
                    status_code=502,
                )
            issue_id = created.get("node_id")
            issue_number = created.get("number")
            issue_url = created.get("html_url") or created.get("url")
            if (
                not isinstance(issue_id, str)
                or not issue_id
                or type(issue_number) is not int
                or issue_number <= 0
                or not isinstance(issue_url, str)
                or not issue_url
            ):
                raise PbiCreationError(
                    "GitHub did not return a durable issue identity",
                    code="outcome_unknown",
                    status_code=202,
                    unknown_outcome=True,
                )
            save_progress(
                "issue_created",
                completed=True,
                issue_create_started=False,
                issue_id=issue_id,
                issue_number=issue_number,
                issue_url=issue_url,
            )

        if current.issue_number is None or current.issue_id is None:
            raise PbiCreationError(
                "Saved issue identity is incomplete",
                code="issue_identity_incomplete",
                status_code=502,
            )

        issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
        self._validate_pbi_creation_issue(request, current, issue)
        labels = _nodes(issue.get("labels"))
        present_label_names = {
            label.get("name") for label in labels if isinstance(label.get("name"), str)
        }
        missing_labels = [
            (label, label_id)
            for label, label_id in zip(request.labels, target.label_ids, strict=True)
            if label not in present_label_names
        ]
        if missing_labels:
            save_progress("apply_labels")
            self._client.execute(
                ADD_ISSUE_LABELS_MUTATION,
                {
                    "input": {
                        "labelableId": current.issue_id,
                        "labelIds": [label_id for _, label_id in missing_labels],
                    }
                },
            )
            issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
            self._validate_pbi_creation_issue(request, current, issue)
            labels = _nodes(issue.get("labels"))
            present_label_names = {
                label.get("name")
                for label in labels
                if isinstance(label.get("name"), str)
            }
        if not set(request.labels).issubset(present_label_names):
            raise PbiCreationError(
                "Requested labels did not read back",
                code="labels_unconfirmed",
                status_code=502,
            )
        save_progress("labels_applied", completed=True)

        project_item = self._pbi_creation_project_item(
            request, target, current.issue_id, current.issue_number
        )
        if project_item is None:
            save_progress("add_to_project")
            self._client.execute(
                ADD_PROJECT_ITEM_MUTATION,
                {
                    "input": {
                        "projectId": target.project_node_id,
                        "contentId": current.issue_id,
                    }
                },
            )
            project_item = self._pbi_creation_project_item(
                request, target, current.issue_id, current.issue_number
            )
        if project_item is None:
            raise PbiCreationError(
                "Project membership did not read back",
                code="project_membership_unconfirmed",
                status_code=502,
            )
        item_id = project_item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise PbiCreationError(
                "Project item identity is incomplete",
                code="project_item_identity_incomplete",
                status_code=502,
            )
        if current.project_item_id is not None and current.project_item_id != item_id:
            raise PbiCreationError(
                "Project item identity changed during reconciliation",
                code="project_item_identity_conflict",
                status_code=409,
            )
        save_progress("project_added", completed=True, project_item_id=item_id)

        if project_item.get("status") != target.backlog_status:
            save_progress("set_backlog")
            self._client.execute(
                UPDATE_PROJECT_STATUS_MUTATION,
                {
                    "input": {
                        "projectId": target.project_node_id,
                        "itemId": item_id,
                        "fieldId": target.status_field_id,
                        "value": {"singleSelectOptionId": target.backlog_option_id},
                    }
                },
            )
            project_item = self._pbi_creation_project_item(
                request, target, current.issue_id, current.issue_number
            )
        if project_item is None or project_item.get("status") != target.backlog_status:
            raise PbiCreationError(
                "Project Backlog status did not read back",
                code="project_status_unconfirmed",
                status_code=502,
            )
        save_progress("status_backlog", completed=True)

        issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
        self._validate_pbi_creation_issue(request, current, issue)
        actual_labels = tuple(
            sorted(
                {
                    str(label["name"])
                    for label in _nodes(issue.get("labels"))
                    if isinstance(label.get("name"), str)
                }
            )
        )
        project_item = self._pbi_creation_project_item(
            request, target, current.issue_id, current.issue_number
        )
        if (
            project_item is None
            or project_item.get("item_id") != item_id
            or project_item.get("status") != target.backlog_status
        ):
            raise PbiCreationError(
                "Final Project state did not read back",
                code="project_state_unconfirmed",
                status_code=502,
            )
        return PbiCreationResult(
            issue_id=current.issue_id,
            issue_number=current.issue_number,
            issue_url=current.issue_url or str(issue.get("url", "")),
            labels=actual_labels,
            project_item_id=item_id,
            project_status=target.backlog_status,
            completed_steps=current.completed_steps,
        )


__all__ = ["PbiCreationWriteMixin"]
