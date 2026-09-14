from __future__ import annotations

from dataclasses import replace
from threading import Event

from beehaiive.pbi_creation import (
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationTarget,
    PbiCreationValidationError,
)


class FakePbiCreationProvider:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.create_calls = 0
        self.issue_posts = 0
        self.fail_after_issue = False
        self.unknown_create = False
        self.crash_after_create_marker = False
        self.reject_labels = False
        self.issue_exists = False
        self.entered: Event | None = None
        self.release: Event | None = None

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        self.prepare_calls += 1
        if request.repository != "owner/repo":
            raise AssertionError("unexpected repository")
        if self.reject_labels and request.labels:
            raise PbiCreationValidationError("Unknown label", code="unknown_label")
        return PbiCreationTarget(
            project_node_id="project-id",
            repository_node_id="repository-id",
            status_field_id="status-field",
            backlog_option_id="backlog-option",
            backlog_status="Backlog",
            label_ids=tuple(f"label-{name}" for name in request.labels),
        )

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint,
    ) -> PbiCreationResult:
        self.create_calls += 1
        current = progress
        if current.issue_id is None:
            self.issue_posts += 1
            current = replace(
                current,
                issue_create_started=True,
                current_step="create_issue",
            )
            checkpoint(current)
            if self.crash_after_create_marker:
                raise SystemExit("simulated process stop")
            if self.entered is not None and self.release is not None:
                self.entered.set()
                if not self.release.wait(timeout=5):
                    raise RuntimeError("test did not release blocked create")
            self.issue_exists = True
            if self.unknown_create:
                raise RuntimeError("private-provider-detail")
            current = replace(
                current,
                issue_create_started=False,
                issue_id="issue-node-id",
                issue_number=37,
                issue_url="https://example.test/issues/37",
                current_step="issue_created",
                completed_steps=(*current.completed_steps, "issue_created"),
            )
            checkpoint(current)
            if self.fail_after_issue:
                self.fail_after_issue = False
                raise RuntimeError("private-provider-detail")
        elif not self.issue_exists:
            raise AssertionError("retry lost the previously created issue")

        for step in ("labels_applied", "project_added", "status_backlog"):
            if step not in current.completed_steps:
                current = replace(
                    current,
                    current_step=step,
                    completed_steps=(*current.completed_steps, step),
                    project_item_id="project-item-id"
                    if step == "project_added"
                    else current.project_item_id,
                )
                checkpoint(current)
        return PbiCreationResult(
            issue_id="issue-node-id",
            issue_number=37,
            issue_url="https://example.test/issues/37",
            labels=request.labels,
            project_item_id="project-item-id",
            project_status="Backlog",
            completed_steps=current.completed_steps,
        )
