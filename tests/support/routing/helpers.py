from pathlib import Path
from types import SimpleNamespace
from typing import cast

from beehaiive.routing import (
    ModelSpec,
    ModelTier,
    RoutingConfig,
    RoutingLimits,
)
from beehaiive.workflow import (
    CheckSuite,
    Constitution,
    GitWorktreeManager,
    WorkflowService,
    WorkflowStore,
)

CONSTITUTION_PATH = Path(__file__).parents[3] / "constitution.json"


def workflow_service_for_attempt(
    tmp_path: Path, check_runner: CheckSuite
) -> tuple[WorkflowService, WorkflowStore]:
    repository = tmp_path / "workflow-repository"
    repository.mkdir()
    store = WorkflowStore()
    worktrees = cast(GitWorktreeManager, SimpleNamespace(repository=repository))
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        None,
        worktrees=worktrees,
        check_runner=check_runner,
    )
    return service, store


def attach_run_workspace(store: WorkflowStore, tmp_path: Path, run_id: str) -> None:
    workspace = tmp_path / f"workspace-{run_id}"
    workspace.mkdir()
    store.acquire_lease(f"dashboard-run:{run_id}", f"codex/{run_id}", str(workspace))


def build_routing_config(**limits: int) -> RoutingConfig:
    return RoutingConfig(
        writer=ModelSpec(ModelTier.LUNA, "cheap-writer", 1.0, 2.0),
        triage=(
            ModelSpec(ModelTier.TERRA, "first-triage", 2.0, 4.0),
            ModelSpec(ModelTier.SOL, "second-triage", 3.0, 6.0),
            ModelSpec(ModelTier.ASTRA, "final-triage", 4.0, 8.0),
        ),
        limits=RoutingLimits(**limits),
    )
