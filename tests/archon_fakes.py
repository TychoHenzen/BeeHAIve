"""Stand-ins for Archon's server and CLI, so tests never touch the real install."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient

from beehaiive.app import create_app
from beehaiive.archon.cli import ArchonCli, ArchonCliError, CliResult
from beehaiive.archon.pipeline import PipelineReader
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
    """Answers each command with the first scripted result whose key it contains.

    Without one, a `handler` answers when it returns a result, for a fake that
    keeps state between commands.
    """

    answers: dict[str, CliResult] = field(default_factory=dict[str, CliResult])
    calls: list[list[str]] = field(default_factory=list[list[str]])
    cwds: list[Path | None] = field(default_factory=list[Path | None])
    # Raised instead of answering, as run_process does when archon cannot start.
    error: ArchonCliError | None = None
    handler: Callable[[list[str]], CliResult | None] | None = None

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
        handled = self.handler(command) if self.handler is not None else None
        if handled is not None:
            return handled
        return CliResult(1, "", f"unexpected command: {joined}")


def printed(value: object) -> CliResult:
    return CliResult(0, json.dumps(value), "")


def refused(error: str) -> CliResult:
    """A refusal as Archon prints it in --json mode."""
    return CliResult(1, json.dumps({"ok": False, "error": error}), "")


@dataclass
class QueueFire:
    """One fire Archon received: its receipt, and the start request it made."""

    receipt_id: str
    source: str
    received_at: str
    resource: str
    # The request's id, or None when Archon refused the fire and made none.
    request_id: str | None


@dataclass
class TriggerQueue:
    """Archon's trigger receipts and start requests, kept between commands.

    It answers `trigger list`, `trigger inspect`, `trigger withdraw` and
    `workflow status` the way Archon 0.14 does in --json mode. A receipt shows
    its request's current status, as Archon's does, so a withdrawn start reads
    as withdrawn on the next inspect.
    """

    fires: list[QueueFire] = field(default_factory=list[QueueFire])
    requests: dict[str, str] = field(default_factory=dict[str, str])
    runs: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    # Requests another binding's fire admits just before a withdraw reaches them.
    drained: set[str] = field(default_factory=set[str])
    # Requests whose withdraw Archon refuses while they stay queued.
    stuck: set[str] = field(default_factory=set[str])

    def add(
        self,
        source: str,
        request: str | None,
        resource: str = "pbi:github.com/o/repo:refine",
        request_id: str | None = None,
    ) -> QueueFire:
        """Add a fire older than every fire so far. `request` is the status of
        the start it made, or None for a fire Archon refused."""
        number = len(self.fires)
        hour, minute = 12 - number // 60, 59 - number % 60
        made = QueueFire(
            receipt_id=f"rcpt-{number:04d}",
            source=source,
            received_at=f"2026-11-26T{hour:02d}:{minute:02d}:00Z",
            resource=resource,
            request_id=None if request is None else request_id or f"req-{number:04d}",
        )
        if made.request_id is not None and request is not None:
            self.requests[made.request_id] = request
        self.fires.append(made)
        return made

    def statuses(self, source: str) -> list[str]:
        return [
            self.requests[fire.request_id]
            for fire in self.fires
            if fire.source == source and fire.request_id is not None
        ]

    def _disposition(self, request_id: str | None) -> dict[str, object] | None:
        status = self.requests.get(request_id or "")
        if request_id is None or status is None or status == "withdrawn":
            return None
        if status == "admitted":
            return {"status": status, "requestId": request_id, "runId": request_id}
        blocker = {"kind": "run", "id": "run-live"}
        return {"status": status, "requestId": request_id, "blocker": blocker}

    def _receipt(self, fire: QueueFire) -> dict[str, object]:
        entry = {
            "receiptId": fire.receipt_id,
            "bindingId": fire.source,
            "status": "complete" if fire.request_id else "rejected",
            "error": None if fire.request_id else "resource_capacity_conflict",
            "intent": {"resource": fire.resource, "capacity": 1, "overlap": "queue"},
            "requestStatus": self.requests.get(fire.request_id or ""),
            "disposition": self._disposition(fire.request_id),
        }
        return {
            "id": fire.receipt_id,
            "sourceInstanceId": fire.source,
            "receivedAt": fire.received_at,
            "outcome": "matched",
            "bindings": [entry],
        }

    def _inspect(self, wanted: str) -> CliResult:
        for fire in self.fires:
            if fire.receipt_id == wanted:
                return printed(self._receipt(fire))
        if wanted in self.requests:
            return printed({"id": wanted, "status": self.requests[wanted]})
        return refused("Trigger receipt or request not found.")

    def _withdraw(self, request_id: str) -> CliResult:
        if request_id in self.drained:
            self.requests[request_id] = "admitted"
        if self.requests.get(request_id) != "queued" or request_id in self.stuck:
            return refused("Only untouched queued requests can be withdrawn.")
        self.requests[request_id] = "withdrawn"
        return printed({"requestId": request_id, "withdrawn": True})

    def answer(self, command: list[str]) -> CliResult | None:
        verb = command[1:3]
        if verb == ["trigger", "list"]:
            rows = [
                {
                    "id": fire.receipt_id,
                    "sourceInstanceId": fire.source,
                    "outcome": "matched",
                    "receivedAt": fire.received_at,
                }
                for fire in self.fires
            ]
            return printed(rows)
        if verb == ["trigger", "inspect"]:
            return self._inspect(command[3])
        if verb == ["trigger", "withdraw"]:
            return self._withdraw(command[3])
        if verb == ["workflow", "status"]:
            return printed({"runs": self.runs, "scopeFallback": False})
        return None


def no_commands(_args: object) -> str | None:
    return None


def not_found(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404, json={"error": "not here"})


@dataclass
class ArchonHarness:
    client: TestClient
    runner: FakeRunner
    scheduler_runner: FakeRunner
    pipeline_runner: FakeRunner
    home: Path
    seen: list[httpx2.Request]


def archon_harness(
    tmp_path: Path,
    handler: Handler = not_found,
    *,
    platform: str = "win32",
    clock: Callable[[], float] | None = None,
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
    pipeline_runner = FakeRunner()
    pipeline = PipelineReader(
        home, runner=pipeline_runner, node="node", clock=clock or time.monotonic
    )
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
        pipeline=pipeline,
    )
    # No context manager: the lifespan would start the session index thread.
    client = TestClient(app, base_url=LOCAL)
    return ArchonHarness(client, runner, scheduler_runner, pipeline_runner, home, seen)
