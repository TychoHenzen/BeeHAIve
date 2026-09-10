"""Application and process lifetime ownership for the dashboard smoke."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.provider import GitHubProjectProvider, ProviderError

from .dashboard_smoke_browser import (
    DevTools,
    find_free_port,
    start_browser,
    terminate_process,
)
from .dashboard_smoke_types import FIXTURE_API_KEY, FIXTURE_PROJECT_ID, SmokeFailure


class FixtureProvider:
    """Deterministic provider used by the local browser smoke."""

    def __init__(self) -> None:
        self.snapshot = fixture_snapshot()
        self.discoveries = 0

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        if project_id != FIXTURE_PROJECT_ID:
            raise ProviderError(f"Fixture provider is not configured for {project_id}")
        self.discoveries += 1
        return self.snapshot

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(
            branch=request.branch,
            pull_request_url=f"https://example.test/{request.repository}/pull/1",
            pull_request_number=1,
        )

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)


class DiscoveryCountingProvider:
    """Count provider discovery calls while delegating the live provider."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.discoveries = 0

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        self.discoveries += 1
        return self.provider.discover_project(project_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)


class NoCallGraphQLClient:
    """Fail if a provider identity probe attempts a network query."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self.calls += 1
        raise SmokeFailure("The provider identity probe reached GraphQL")


def provider_identity_probe(project_id: str) -> dict[str, Any]:
    """Prove a mismatched provider identity fails before GraphQL access."""

    owner, separator, number_text = project_id.partition(":")
    if not separator or not owner or not number_text.isdigit():
        raise SmokeFailure("The provider identity probe received an invalid project")
    client = NoCallGraphQLClient()
    provider = GitHubProjectProvider(
        owner, int(number_text), "provider-identity-probe-token", client=client
    )
    mismatched_project = f"{owner}:{int(number_text) + 1}"
    try:
        provider.discover_project(mismatched_project)
    except ProviderError:
        if client.calls:
            raise SmokeFailure(
                "The mismatched provider identity reached GraphQL"
            ) from None
        return {"rejected_before_graphql": True, "graphql_calls": client.calls}
    raise SmokeFailure("The provider identity probe accepted a mismatched project")


def fixture_snapshot() -> ProjectSnapshot:
    """Return state that exercises project, repository, PBI, and action views."""

    return ProjectSnapshot(
        project_id=FIXTURE_PROJECT_ID,
        name="Fixture Project",
        repositories=(
            RepositorySnapshot(
                name="owner/api",
                pbis=(
                    PbiSnapshot(
                        repository="owner/api",
                        number=1,
                        title="Dashboard proof PBI",
                        stage=Stage.REFINE,
                        metadata={
                            "subtasks": [
                                {"id": "fixture-subtask", "title": "Check API"}
                            ],
                            "readers": [
                                {"id": "security", "status": "pass"},
                                {"id": "tests", "status": "pending"},
                            ],
                            "reviewers": {
                                "security": {"status": "pass"},
                                "tests": {"status": "pending"},
                            },
                            "escalation": {
                                "current": 1,
                                "current_tier": "terra",
                                "consecutive": 1,
                            },
                            "escalation_log": [{"tier": "terra"}],
                            "activity": [
                                {"time": "fixture-time", "action": "Started review"}
                            ],
                        },
                    ),
                ),
            ),
            RepositorySnapshot(name="owner/empty"),
        ),
    )


@dataclass
class ApplicationResources:
    """The app and stores created specifically for one smoke run."""

    app: Any
    stores: list[Any]
    provider: Any


@contextmanager
def isolated_module_environment(directory: Path) -> Iterator[None]:
    """Keep module-created SQLite state outside the repository."""

    names = (
        "BEEHAIIVE_STATE_DB",
        "BEEHAIIVE_ROUTING_DB",
        "BEEHAIIVE_REVIEW_DB",
        "BEEHAIIVE_WORKFLOW_DB",
        "BEEHAIIVE_SKIP_PRODUCTION_APP",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ["BEEHAIIVE_STATE_DB"] = str(directory / "module-state.db")
    os.environ["BEEHAIIVE_ROUTING_DB"] = str(directory / "module-routing.db")
    os.environ["BEEHAIIVE_REVIEW_DB"] = str(directory / "module-reviews.db")
    os.environ["BEEHAIIVE_WORKFLOW_DB"] = str(directory / "module-workflow.db")
    os.environ["BEEHAIIVE_SKIP_PRODUCTION_APP"] = "1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_app(mode: str, project_id: str, directory: Path) -> ApplicationResources:
    """Build an isolated fixture or live-provider application."""

    from beehaiive import EnvironmentGitHubProvider, Orchestrator
    from beehaiive.review import ReviewStore
    from beehaiive.routing import ModelRouter, RoutingStore
    from beehaiive.storage import OrchestratorStore

    if mode == "fixture":
        provider: Any = FixtureProvider()
        api_key = FIXTURE_API_KEY
    else:
        if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
            raise SmokeFailure(
                "Live mode needs GITHUB_TOKEN or GH_TOKEN from the environment"
            )
        owner = os.environ.get("GITHUB_PROJECT_OWNER")
        number = os.environ.get("GITHUB_PROJECT_NUMBER")
        if not owner or not number:
            raise SmokeFailure(
                "Live mode needs GITHUB_PROJECT_OWNER and GITHUB_PROJECT_NUMBER"
            )
        configured_project_id = f"{owner}:{number}"
        if project_id != configured_project_id:
            raise SmokeFailure(
                "Live project must match GITHUB_PROJECT_OWNER:GITHUB_PROJECT_NUMBER"
            )
        provider = DiscoveryCountingProvider(EnvironmentGitHubProvider())
        api_key = os.environ.get("BEEHAIIVE_API_KEY")

    runtime_stores: list[Any] = []
    try:
        state_store = OrchestratorStore(directory / "state.db")
        runtime_stores.append(state_store)
        routing_store = RoutingStore(directory / "routing.db")
        runtime_stores.append(routing_store)
        review_store = ReviewStore(directory / "reviews.db")
        runtime_stores.append(review_store)
        model_router = ModelRouter(routing_store)
        orchestrator = Orchestrator(state_store, provider, model_router)
        with isolated_module_environment(directory):
            import main

            app = main.create_app(
                orchestrator=orchestrator,
                api_key=api_key,
                allowed_project_ids={project_id},
                review_store=review_store,
                routing_store=routing_store,
                model_router=model_router,
            )
    except Exception as error:
        cleanup_errors = close_stores(runtime_stores)
        if cleanup_errors:
            details = ", ".join(cleanup_errors)
            raise SmokeFailure(
                f"Application setup failed and cleanup failed: {details}"
            ) from error
        raise
    return ApplicationResources(app=app, stores=runtime_stores, provider=provider)


def start_server(
    app: Any, timeout: float = 15.0
) -> tuple[Any, threading.Thread, str, io.StringIO]:
    import uvicorn

    port = find_free_port()
    output = io.StringIO()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            log_config=None,
        )
    )

    def run_server() -> None:
        with redirect_stdout(output), redirect_stderr(output):
            server.run()

    thread = threading.Thread(target=run_server, name="beehaiive-smoke-server")
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base_url}/dashboard", timeout=1) as response:
                if response.status == 200:
                    return server, thread, base_url, output
        except (OSError, URLError):
            pass
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    if thread.is_alive():
        raise SmokeFailure("The isolated service thread did not terminate")
    raise SmokeFailure("The isolated dashboard service did not become ready")


def close_stores(stores: list[Any]) -> list[str]:
    errors: list[str] = []
    closed: set[int] = set()
    for store in reversed(stores):
        if id(store) in closed:
            continue
        closed.add(id(store))
        close = getattr(store, "close", None)
        if not callable(close):
            errors.append(f"Resource has no close method: {type(store).__name__}")
            continue
        try:
            store.close()
        except Exception as error:
            errors.append(str(error))
    return errors


@dataclass
class SmokeEnvironment:
    """Own every resource created by one browser proof."""

    mode: str
    project_id: str
    browser: str | None
    directory: Path = field(
        default_factory=lambda: Path(
            tempfile.mkdtemp(prefix="beehaiive-dashboard-smoke-")
        )
    )
    resources: ApplicationResources | None = None
    server: Any = None
    server_thread: threading.Thread | None = None
    base_url: str = ""
    server_output: io.StringIO | None = None
    browser_process: subprocess.Popen[bytes] | None = None
    devtools: DevTools | None = None

    def start(self) -> None:
        self.resources = build_app(self.mode, self.project_id, self.directory)
        self.server, self.server_thread, self.base_url, self.server_output = (
            start_server(self.resources.app)
        )
        self.browser_process, self.devtools = start_browser(
            self.base_url, self.project_id, self.browser, self.directory
        )

    @property
    def provider(self) -> Any:
        if self.resources is None:
            raise SmokeFailure("The smoke environment has not started")
        return self.resources.provider

    def close(self) -> list[str]:
        errors: list[str] = []
        if self.devtools is not None:
            try:
                self.devtools.close()
            except Exception as error:
                errors.append(str(error))
            self.devtools = None
        if self.browser_process is not None:
            try:
                terminate_process(self.browser_process)
            except Exception as error:
                errors.append(str(error))
            self.browser_process = None
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=5)
            if self.server_thread.is_alive():
                errors.append("The isolated service thread did not terminate")
            self.server_thread = None
            self.server = None
        if self.resources is not None:
            errors.extend(close_stores(self.resources.stores))
            self.resources = None
        if self.directory.exists():
            try:
                shutil.rmtree(self.directory)
            except OSError as error:
                errors.append(str(error))
        return errors
