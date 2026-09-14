from dataclasses import replace

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
)
from beehaiive.pbi_creation import (
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationTarget,
)


class ApiProvider:
    def __init__(self) -> None:
        self.pbi_prepare_calls = 0
        self.pbi_create_calls = 0

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id=project_id,
            name="Planning",
            repositories=(
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot("owner/api", 1, "API one"),
                        PbiSnapshot("owner/api", 2, "API two"),
                    ),
                ),
            ),
        )

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        self.pbi_prepare_calls += 1
        return PbiCreationTarget(
            project_node_id="project-node",
            repository_node_id="repository-node",
            status_field_id="status-field",
            backlog_option_id="backlog-option",
            backlog_status="Backlog",
            label_ids=tuple(f"label-{label}" for label in request.labels),
        )

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint,
    ) -> PbiCreationResult:
        self.pbi_create_calls += 1
        progress = replace(
            progress,
            issue_create_started=False,
            issue_id="issue-node",
            issue_number=3,
            issue_url="https://example.test/issues/3",
            project_item_id="project-item",
            completed_steps=(
                "issue_created",
                "labels_applied",
                "project_added",
                "status_backlog",
            ),
        )
        checkpoint(progress)
        return PbiCreationResult(
            issue_id="issue-node",
            issue_number=3,
            issue_url="https://example.test/issues/3",
            labels=request.labels,
            project_item_id="project-item",
            project_status="Backlog",
            completed_steps=progress.completed_steps,
        )
