import subprocess
from pathlib import Path

from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    ModelRouter,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore
from beehaiive.workflow import (
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from tests.conftest import FakeProvider
from tests.support.agent.passing_workflow_check import (
    PassingWorkflowCheck as PassingWorkflowCheck,
)


def agent_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Agent demo",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "Demo PBI",
                        stage=Stage.REFINE,
                    ),
                ),
            ),
        ),
    )


def service_with_run(
    executor: CodexExecModelExecutor,
) -> tuple[Orchestrator, OrchestratorStore, RoutingStore, RunState]:
    store = OrchestratorStore()
    routing_store = RoutingStore()
    service = Orchestrator(
        store,
        FakeProvider(agent_snapshot()),
        ModelRouter(routing_store),
        executor,
    )
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    return service, store, routing_store, run


def make_git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Agent Tests"),
    ):
        result = subprocess.run(
            ("git", *arguments), cwd=path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr or result.stdout
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=path, check=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path


def configure_test_remote(repository: Path, root: Path) -> Path:
    remote = root / "owner" / "api.git"
    remote.parent.mkdir()
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    subprocess.run(
        ("git", "remote", "add", "origin", str(remote)),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "push", "origin", "master"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return remote


def workflow_service_for(
    tmp_path: Path, repository: Path
) -> tuple[WorkflowService, WorkflowStore]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(Path(__file__).resolve().parents[3] / "constitution.json"),
        [PassingWorkflowCheck()],
    )
    return service, store
