"""Stand-ins for Archon's server and CLI, so tests never touch the real install."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient

from beehaiive.app import create_app
from beehaiive.archon.cli import ArchonCli, ArchonCliError, CliResult
from beehaiive.archon.settings import ArchonSettings
from beehaiive.archon.triggers import Scheduler
from beehaiive.config import CoreConfig
from beehaiive.repos import RepoResolver
from beehaiive.sessions import SessionIndex

LOCAL = "http://127.0.0.1"
ACTION = {"X-BeeHAIve": "1"}

Handler = Callable[[httpx2.Request], httpx2.Response]


@dataclass
class FakeRunner:
    """Answers each command with the first scripted result whose key it contains."""

    answers: dict[str, CliResult] = field(default_factory=dict[str, CliResult])
    calls: list[list[str]] = field(default_factory=list[list[str]])
    cwds: list[Path | None] = field(default_factory=list[Path | None])
    # Raised instead of answering, as run_process does when archon cannot start.
    error: ArchonCliError | None = None

    def __call__(
        self, args: Sequence[str], cwd: Path | None, _timeout: float
    ) -> CliResult:
        command = list(args)
        self.calls.append(command)
        self.cwds.append(cwd)
        if self.error is not None:
            raise self.error
        joined = " ".join(command)
        for key, result in self.answers.items():
            if key in joined:
                return result
        return CliResult(1, "", f"unexpected command: {joined}")


def printed(value: object) -> CliResult:
    return CliResult(0, json.dumps(value), "")


def no_commands(_args: object) -> str | None:
    return None


def not_found(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404, json={"error": "not here"})


@dataclass
class ArchonHarness:
    client: TestClient
    runner: FakeRunner
    scheduler_runner: FakeRunner
    home: Path
    seen: list[httpx2.Request]


def archon_harness(
    tmp_path: Path,
    handler: Handler = not_found,
    *,
    platform: str = "win32",
) -> ArchonHarness:
    home = tmp_path / "archon-home"
    home.mkdir()
    settings = ArchonSettings(
        home=home, url="http://archon.test", binary=home / "archon"
    )
    seen: list[httpx2.Request] = []

    def recorded(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    runner = FakeRunner()
    scheduler_runner = FakeRunner()
    projects = tmp_path / "projects"
    projects.mkdir()
    index = SessionIndex(
        projects, timedelta(hours=1), resolver=RepoResolver(no_commands)
    )
    app = create_app(
        CoreConfig(projects, archon=settings),
        index,
        archon_http=httpx2.AsyncClient(
            transport=httpx2.MockTransport(recorded), base_url=settings.url
        ),
        archon_cli=ArchonCli(settings.binary, runner=runner),
        scheduler=Scheduler(
            settings.binary, runner=scheduler_runner, platform=platform
        ),
    )
    # No context manager: the lifespan would start the session index thread.
    client = TestClient(app, base_url=LOCAL)
    return ArchonHarness(client, runner, scheduler_runner, home, seen)
