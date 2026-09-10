"""Run a credential-safe browser proof of the BeeHAIve dashboard."""

# pyright: basic
# CDP responses and dashboard payloads are validated dynamically at their boundaries.

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from beehaiive.models import (  # noqa: E402
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.provider import GitHubProjectProvider, ProviderError  # noqa: E402

FIXTURE_PROJECT_ID = "fixture:1"
FIXTURE_API_KEY = "fixture-api-key"


class SmokeFailure(RuntimeError):
    """Raised when a smoke assertion cannot be proved."""


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


@contextmanager
def isolated_module_environment(directory: Path):
    """Keep module-created SQLite state outside the repository."""

    names = (
        "BEEHAIIVE_STATE_DB",
        "BEEHAIIVE_ROUTING_DB",
        "BEEHAIIVE_REVIEW_DB",
        "BEEHAIIVE_WORKFLOW_DB",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ["BEEHAIIVE_STATE_DB"] = str(directory / "module-state.db")
    os.environ["BEEHAIIVE_ROUTING_DB"] = str(directory / "module-routing.db")
    os.environ["BEEHAIIVE_REVIEW_DB"] = str(directory / "module-reviews.db")
    os.environ["BEEHAIIVE_WORKFLOW_DB"] = str(directory / "module-workflow.db")
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_app(
    mode: str, project_id: str, directory: Path
) -> tuple[Any, list[Any], Any]:
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

            module_stores = getattr(main.app.state, "beehaiive_stores", ())
            runtime_stores.extend(store for store in module_stores if store is not None)
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
    return app, runtime_stores, provider


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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
    raise SmokeFailure("The isolated dashboard service did not become ready")


class DevTools:
    """Small standard-library WebSocket client for Chromium DevTools Protocol."""

    def __init__(self, websocket_url: str) -> None:
        parsed = urlsplit(websocket_url)
        if parsed.hostname is None or parsed.port is None:
            raise SmokeFailure("The browser returned an invalid DevTools URL")
        self.socket = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=5
        )
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(4096)
            if len(response) > 16_384:
                raise SmokeFailure("The browser DevTools handshake was too large")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise SmokeFailure("The browser rejected the DevTools connection")
        self.next_id = 0

    def close(self) -> None:
        with suppress(OSError):
            self.socket.close()

    def command(
        self, method: str, params: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        self.next_id += 1
        message_id = self.next_id
        payload: dict[str, object] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._send_frame(json.dumps(payload).encode("utf-8"))
        while True:
            message = json.loads(self._receive_text())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error = message["error"]
                raise SmokeFailure(f"CDP {method} failed: {error}")
            return dict(message.get("result", {}))

    def evaluate(self, expression: str) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in result:
            raise SmokeFailure(
                f"Browser evaluation failed: {result['exceptionDetails']}"
            )
        remote = result.get("result", {})
        if isinstance(remote, Mapping):
            return remote.get("value")
        return None

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65_536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _receive_text(self) -> str:
        while True:
            first, second = struct.unpack("!BB", self._receive_exact(2))
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._receive_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._receive_exact(8))[0]
            mask = self._receive_exact(4) if second & 0x80 else None
            payload = self._receive_exact(length)
            if mask is not None:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise SmokeFailure("The browser closed the DevTools connection")
            if opcode == 0x1:
                return payload.decode("utf-8")

    def _receive_exact(self, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = self.socket.recv(length - len(data))
            if not chunk:
                raise SmokeFailure("The DevTools connection ended unexpectedly")
            data += chunk
        return data


def browser_executable(requested: str | None) -> Path:
    if requested:
        resolved = shutil.which(requested) or requested
        path = Path(resolved)
        if path.is_file():
            return path
        raise SmokeFailure(f"Browser executable not found: {requested}")

    candidates: list[Path] = []
    for variable in ("ProgramFiles(x86)", "PROGRAMFILES(X86)", "PROGRAMFILES"):
        root = os.environ.get(variable)
        if root:
            base = Path(root)
            candidates.extend(
                (
                    base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    base / "Google" / "Chrome" / "Application" / "chrome.exe",
                )
            )
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
        candidates.extend(
            (
                base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                base / "Google" / "Chrome" / "Application" / "chrome.exe",
            )
        )
    candidates.extend(
        Path(value)
        for value in (
            shutil.which("msedge") or "",
            shutil.which("microsoft-edge") or "",
            shutil.which("google-chrome") or "",
            shutil.which("chromium") or "",
        )
        if value
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SmokeFailure(
        "No Chromium-compatible browser found. Pass --browser with Edge or Chrome."
    )


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError as error:
            if process.poll() is None:
                raise SmokeFailure(
                    "The browser process could not be terminated"
                ) from error
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError as error:
                if process.poll() is None:
                    raise SmokeFailure(
                        "The browser process could not be killed"
                    ) from error
                return
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise SmokeFailure("The browser process did not terminate") from error
        if process.poll() is None:
            raise SmokeFailure("The browser process did not terminate")


def start_browser(
    base_url: str,
    project_id: str,
    browser: str | None,
    directory: Path,
    timeout: float = 20.0,
) -> tuple[subprocess.Popen[bytes], DevTools]:
    executable = browser_executable(browser)
    debug_port = find_free_port()
    profile = directory / "browser-profile"
    command = [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--no-first-run",
        "--no-sandbox",
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        "about:blank",
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        deadline = time.monotonic() + timeout
        target: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SmokeFailure("The browser exited before opening a DevTools page")
            try:
                with urlopen(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=1
                ) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                target = next(
                    item
                    for item in targets
                    if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
                )
                break
            except (OSError, URLError, StopIteration, json.JSONDecodeError):
                time.sleep(0.05)
        if target is None:
            raise SmokeFailure("The browser DevTools endpoint did not become ready")

        devtools = DevTools(str(target["webSocketDebuggerUrl"]))
        devtools.command("Page.enable")
        devtools.command("Runtime.enable")
        devtools.command(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
(() => {
  if (window.__beehaiiveSmokeFetches) return;
  window.__beehaiiveSmokeFetches = [];
  window.__beehaiiveSmokePageStartedAt = Date.now();
  const nativeFetch = window.fetch;
  window.fetch = async function(input, init) {
    const method = (init && init.method) || (input && input.method) || "GET";
    const url = typeof input === "string" ? input : input.url;
    const started_at = Date.now();
    let request = null;
    try {
      if (init && init.body) request = JSON.parse(init.body);
    } catch (_) {}
    try {
      const response = await nativeFetch.apply(this, arguments);
      let payload = null;
      try { payload = await response.clone().json(); } catch (_) {}
      window.__beehaiiveSmokeFetches.push({
        method, url, status: response.status, ok: response.ok,
        request, payload, started_at, completed_at: Date.now()
      });
      return response;
    } catch (error) {
      window.__beehaiiveSmokeFetches.push({
        method, url, status: null, ok: false, request, payload: null,
        started_at, completed_at: Date.now()
      });
      throw error;
    }
  };
})();
"""
            },
        )
        page_url = (
            f"{base_url.rstrip('/')}/dashboard?project={quote(project_id, safe='')}"
        )
        devtools.command("Page.navigate", {"url": page_url})
        return process, devtools
    except Exception:
        terminate_process(process)
        raise


def wait_until(
    devtools: DevTools, expression: str, label: str, timeout: float = 15.0
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = devtools.evaluate(expression)
        if value:
            return value
        time.sleep(0.05)
    raise SmokeFailure(f"Timed out waiting for {label}")


def status_snapshot(devtools: DevTools) -> dict[str, str]:
    value = devtools.evaluate(
        """
(() => {
  const node = document.querySelector("#state-status");
  return {
    message: node?.textContent?.trim() || "",
    class_name: node?.className || ""
  };
})()
"""
    )
    if not isinstance(value, Mapping):
        return {"message": "", "class_name": ""}
    return {
        "message": str(value.get("message", "")),
        "class_name": str(value.get("class_name", "")),
    }


def click_button(devtools: DevTools, label: str) -> bool:
    return bool(
        devtools.evaluate(
            f"""
(() => {{
  const button = [...document.querySelectorAll("button")]
    .find((node) => node.textContent.trim() === {json.dumps(label)});
  if (!button) return false;
  button.click();
  return true;
}})()
"""
        )
    )


def click_selector(devtools: DevTools, selector: str) -> bool:
    return bool(
        devtools.evaluate(
            f"""
(() => {{
  const node = document.querySelector({json.dumps(selector)});
  if (!node) return false;
  node.click();
  return true;
}})()
"""
        )
    )


def click_repository_button(devtools: DevTools, repository: str, label: str) -> bool:
    return bool(
        devtools.evaluate(
            f"""
(() => {{
  const repo = [...document.querySelectorAll(".repo")]
    .find((node) => node.querySelector("h2")?.textContent.includes(
      {json.dumps(repository)}
    ));
  const button = [...(repo?.querySelectorAll("button") || [])]
    .find((node) => node.textContent.trim() === {json.dumps(label)});
  if (!button) return false;
  button.click();
  return true;
}})()
"""
        )
    )


def has_button(devtools: DevTools, label: str) -> bool:
    return bool(
        devtools.evaluate(
            f"""
(() => [...document.querySelectorAll("button")]
  .some((node) => node.textContent.trim() === {json.dumps(label)}))()
"""
        )
    )


def set_input(
    devtools: DevTools, selector: str, value: str, change: bool = False
) -> None:
    event = "change" if change else "input"
    devtools.evaluate(
        f"""
(() => {{
  const input = document.querySelector({json.dumps(selector)});
  if (!input) return false;
  input.value = {json.dumps(value)};
  input.dispatchEvent(new Event({json.dumps(event)}, {{ bubbles: true }}));
  return true;
}})()
"""
    )


def wait_for_status(devtools: DevTools, fragment: str, label: str) -> dict[str, str]:
    wait_until(
        devtools,
        "(document.querySelector('#state-status')?.textContent || '')"
        f".includes({json.dumps(fragment)})",
        label,
    )
    return status_snapshot(devtools)


def wait_for_action(
    devtools: DevTools, action: str, timeout: float = 15.0
) -> dict[str, str]:
    value = wait_until(
        devtools,
        f"""
(() => {{
  const text = document.querySelector("#state-status")?.textContent?.trim() || "";
  return text.startsWith({json.dumps(f"{action} succeeded.")})
    || text.startsWith({json.dumps(f"{action} failed:")}) ? text : "";
}})()
""",
        f"{action} action outcome",
        timeout=timeout,
    )
    snapshot = status_snapshot(devtools)
    snapshot["message"] = str(value)
    return snapshot


def dashboard_snapshot(devtools: DevTools) -> dict[str, Any]:
    value = devtools.evaluate(
        """
(() => {
  const text = (node) => node?.textContent?.trim() || "";
  const summary = Object.fromEntries(
    [...document.querySelectorAll("#summary .card")]
      .map((card) => [
        text(card.querySelector(".label")),
        text(card.querySelector(".value")),
      ])
      .filter(([label]) => label),
  );
  return {
    project_name: text(document.querySelector(".project-meta h2")),
    updated: text(document.querySelector(".project-meta .muted"))
      || text(document.querySelector("#state-status")),
    status: text(document.querySelector("#state-status")),
    counts: summary,
    repositories: [...document.querySelectorAll(".repo h2")].map(text),
    pbis: [...document.querySelectorAll(".pbi-title")].map(text),
    pipeline_stages: [
      ...document.querySelectorAll(".pbi .progress li.current"),
    ].map(text),
    metadata_sections: [...document.querySelectorAll(".pbi .details h3")].map(text),
    metadata_values: [...document.querySelectorAll(".pbi .details section")]
      .flatMap((section) => {
        const title = text(section.querySelector("h3"));
        return [...section.querySelectorAll("li, .muted")]
          .map(text)
          .filter(Boolean)
          .map((value) => `${title}: ${value}`);
      }),
    dashboard_text: text(document.querySelector("#dashboard")),
  };
})()
"""
    )
    if not isinstance(value, Mapping):
        return {
            "project_name": "",
            "updated": "",
            "status": "",
            "counts": {},
            "repositories": [],
            "pbis": [],
            "pipeline_stages": [],
            "metadata_sections": [],
            "metadata_values": [],
            "dashboard_text": "",
        }
    return dict(value)


def required_dashboard_fields(snapshot: Mapping[str, Any]) -> dict[str, bool]:
    counts = snapshot.get("counts")
    count_values = counts if isinstance(counts, Mapping) else {}
    required_counts = (
        "Projects",
        "Repositories",
        "PBIs",
        "Subtasks",
        "Writers",
        "Readers",
        "Active runs",
        "Failed runs",
        "Active runs",
        "Failed runs",
    )
    return {
        "project_name": bool(str(snapshot.get("project_name", "")).strip()),
        "counts": all(label in count_values for label in required_counts),
        "repositories": bool(snapshot.get("repositories")),
        "pbis": bool(snapshot.get("pbis")),
        "pipeline_stage": bool(snapshot.get("pipeline_stages")),
        "metadata": bool(snapshot.get("metadata_sections")),
        "updated_at": str(snapshot.get("updated", "")).startswith(
            ("Updated ", "Updated: ")
        ),
    }


def dashboard_payload(devtools: DevTools) -> Mapping[str, Any]:
    for item in browser_fetches(devtools):
        url = str(item.get("url", ""))
        payload = item.get("payload")
        if (
            item.get("method") == "GET"
            and item.get("status") == 200
            and "/projects/" in url
            and url.endswith("/dashboard")
            and isinstance(payload, Mapping)
        ):
            return payload
    raise SmokeFailure("The browser recorded no successful dashboard response")


def response_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def response_metadata_lines(payload: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for repository in response_mappings(payload.get("repositories")):
        for pbi in response_mappings(repository.get("pbis")):
            for title, key, formatter in (
                (
                    "Subtasks",
                    "subtasks",
                    lambda item: (
                        f"{item.get('id') or 'subtask'}: {item.get('title') or ''}"
                    ),
                ),
                (
                    "Pull requests",
                    "pull_requests",
                    lambda item: (
                        f"#{item.get('number')}: "
                        f"{item.get('review_decision') or 'review pending'}"
                    ),
                ),
                (
                    "Readers",
                    "readers",
                    lambda item: (
                        f"{
                            ('PR #' + str(item['pull_request']) + ' ')
                            if item.get('pull_request')
                            else ''
                        }"
                        f"{item.get('id') or item.get('name') or 'reader'}: "
                        f"{item.get('status') or 'pending'}"
                    ),
                ),
            ):
                values = response_mappings(pbi.get(key))
                if values:
                    lines.extend(f"{title}: {formatter(item)}" for item in values)
                else:
                    lines.append(f"{title}: None")

            reviewers = pbi.get("reviewers")
            reviewer_values = (
                reviewers.items() if isinstance(reviewers, Mapping) else ()
            )
            reviewer_lines = [
                f"{name}: {result.get('status') or 'pending'}"
                + (f" · {result['comment']}" if result.get("comment") else "")
                for name, raw_result in reviewer_values
                if isinstance(raw_result, Mapping)
                for result in (raw_result,)
            ]
            lines.extend(f"Reviewer results: {line}" for line in reviewer_lines)
            if not reviewer_lines:
                lines.append("Reviewer results: None")

            escalation = pbi.get("escalation")
            current = escalation if isinstance(escalation, Mapping) else {}
            tier = current.get("current_tier", current.get("current"))
            consecutive = current.get("consecutive", 0)
            lines.append(f"Escalation: Tier {tier}; {consecutive} consecutive failures")
            for entry in response_values(pbi.get("escalation_log")):
                lines.append(
                    "Escalation: "
                    + json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                )

            activity = response_values(pbi.get("activity"))
            if activity:
                for raw_entry in reversed(activity):
                    entry = raw_entry if isinstance(raw_entry, Mapping) else {}
                    transition = (
                        f" {entry['from_stage']} -> {entry['to_stage']}"
                        if entry.get("from_stage") and entry.get("to_stage")
                        else ""
                    )
                    timestamp = entry.get("created_at", entry.get("time", ""))
                    description = entry.get("action", entry.get("type", "event"))
                    lines.append(
                        f"Activity: {timestamp} {description}{transition}".strip()
                    )
            else:
                lines.append("Activity: None")
    return lines


def response_values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value


def response_backed_dashboard_fields(
    payload: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, bool]:
    counts = payload.get("counts")
    rendered_counts = snapshot.get("counts")
    expected_counts = {
        "Projects": counts.get("projects") if isinstance(counts, Mapping) else None,
        "Repositories": (
            f"{counts.get('active_repositories', 0)} / {counts.get('repositories', 0)}"
            if isinstance(counts, Mapping)
            else None
        ),
        "PBIs": counts.get("pbis") if isinstance(counts, Mapping) else None,
        "Subtasks": counts.get("subtasks") if isinstance(counts, Mapping) else None,
        "Writers": counts.get("writers") if isinstance(counts, Mapping) else None,
        "Readers": counts.get("readers") if isinstance(counts, Mapping) else None,
        "Active runs": (
            counts.get("active_runs") if isinstance(counts, Mapping) else None
        ),
        "Failed runs": (
            counts.get("failed_runs") if isinstance(counts, Mapping) else None
        ),
    }
    count_values = rendered_counts if isinstance(rendered_counts, Mapping) else {}
    count_match = all(
        label in count_values and str(count_values[label]) == str(expected)
        for label, expected in expected_counts.items()
    )
    expected_repositories: list[str] = []
    expected_pbis: list[str] = []
    expected_stages: list[str] = []
    for repository in response_mappings(payload.get("repositories")):
        name = repository.get("name")
        if isinstance(name, str):
            state = "active" if repository.get("active") is True else "inactive"
            expected_repositories.append(f"{name} {state}")
        for pbi in response_mappings(repository.get("pbis")):
            title = pbi.get("title")
            if isinstance(title, str):
                expected_pbis.append(title)
            for stage in response_mappings(pbi.get("stage_progress")):
                if stage.get("status") == "current" and isinstance(
                    stage.get("label"), str
                ):
                    expected_stages.append(str(stage["label"]))
    rendered_repositories = snapshot.get("repositories")
    rendered_pbis = snapshot.get("pbis")
    rendered_stages = snapshot.get("pipeline_stages")
    metadata_values = snapshot.get("metadata_values")
    rendered_metadata = (
        [str(value) for value in metadata_values]
        if isinstance(metadata_values, list)
        else []
    )
    expected_metadata = response_metadata_lines(payload)
    updated = str(snapshot.get("updated", ""))
    status = str(snapshot.get("status", ""))
    updated_at = payload.get("updated_at")
    return {
        "project_name": payload.get("name") == snapshot.get("project_name"),
        "counts": count_match,
        "repositories": rendered_repositories == expected_repositories,
        "pbis": rendered_pbis == expected_pbis,
        "pipeline_stage": rendered_stages == expected_stages,
        "metadata": rendered_metadata == expected_metadata,
        "updated_at": (
            isinstance(updated_at, str)
            and updated == f"Updated: {updated_at}"
            and status == f"Updated {updated_at}."
        ),
    }


def action_log_snapshot(devtools: DevTools) -> dict[str, Any]:
    value = devtools.evaluate(
        """
(() => {
  const log = document.querySelector("#action-log");
  return {
    visible: Boolean(log && !log.hidden),
    rows: [...document.querySelectorAll("#actions .action-row")]
      .map((row) => row.textContent?.trim() || "")
  };
})()
"""
    )
    if not isinstance(value, Mapping):
        return {"visible": False, "rows": []}
    rows = value.get("rows")
    return {
        "visible": bool(value.get("visible")),
        "rows": [str(row) for row in rows] if isinstance(rows, list) else [],
    }


def browser_fetches(devtools: DevTools) -> list[dict[str, Any]]:
    value = devtools.evaluate("window.__beehaiiveSmokeFetches || []")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def dashboard_fetch_count(devtools: DevTools) -> int:
    return sum(
        1
        for item in browser_fetches(devtools)
        if item.get("method") == "GET"
        and "/projects/" in str(item.get("url", ""))
        and str(item.get("url", "")).endswith("/dashboard")
        and item.get("status") == 200
    )


def wait_for_action_response(devtools: DevTools, action: str) -> dict[str, Any]:
    expression = f"""
(() => {{
  const records = window.__beehaiiveSmokeFetches || [];
  return records.filter((record) =>
    record.method === "POST"
    && record.url.includes("/actions")
    && record.request?.action === {json.dumps(action)}
    && record.payload
  ).at(-1) || null;
}})()
"""
    value = wait_until(devtools, expression, f"{action} action response")
    if not isinstance(value, Mapping):
        raise SmokeFailure(f"The {action} action returned no JSON response")
    return dict(value)


def page_strings(devtools: DevTools) -> tuple[str, str]:
    value = devtools.evaluate(
        "[document.documentElement.innerText || '', "
        "document.documentElement.outerHTML || '']"
    )
    if not isinstance(value, list) or len(value) != 2:
        return "", ""
    return str(value[0]), str(value[1])


def clear_credentials(devtools: DevTools) -> bool:
    return bool(
        devtools.evaluate(
            """
(() => {
  window.localStorage.removeItem("beehaiive-api-key");
  const input = document.querySelector("#api-key");
  if (input) input.value = "";
  return window.localStorage.getItem("beehaiive-api-key") === null
    && (!input || input.value === "");
})()
"""
        )
    )


def credential_surface_snapshot(
    devtools: DevTools, expected_api_key: str
) -> dict[str, bool]:
    value = devtools.evaluate(
        f"""
(() => {{
  const input = document.querySelector("#api-key");
  const inputValue = input?.value || "";
  const storedValue = window.localStorage.getItem("beehaiive-api-key") || "";
  const expected = {json.dumps(expected_api_key)};
  const visibleText = document.documentElement.innerText || "";
  const markup = document.documentElement.outerHTML || "";
  return {{
    input_value_present: Boolean(inputValue),
    storage_value_present: Boolean(storedValue),
    configured_key_in_password_input: Boolean(expected)
      && input?.type === "password"
      && inputValue === expected,
    configured_key_in_local_storage: Boolean(expected)
      && storedValue === expected,
    input_value_leaked_to_visible_page: Boolean(inputValue)
      && visibleText.includes(inputValue),
    input_value_leaked_to_markup: Boolean(inputValue)
      && markup.includes(inputValue),
    storage_value_leaked_to_visible_page: Boolean(storedValue)
      && visibleText.includes(storedValue),
    storage_value_leaked_to_markup: Boolean(storedValue)
      && markup.includes(storedValue)
  }};
}})()
"""
    )
    if not isinstance(value, Mapping):
        return {
            "input_value_present": False,
            "storage_value_present": False,
            "configured_key_in_password_input": False,
            "configured_key_in_local_storage": False,
            "input_value_leaked_to_visible_page": False,
            "input_value_leaked_to_markup": False,
            "storage_value_leaked_to_visible_page": False,
            "storage_value_leaked_to_markup": False,
        }
    return {
        key: bool(value.get(key))
        for key in (
            "input_value_present",
            "storage_value_present",
            "configured_key_in_password_input",
            "configured_key_in_local_storage",
            "input_value_leaked_to_visible_page",
            "input_value_leaked_to_markup",
            "storage_value_leaked_to_visible_page",
            "storage_value_leaked_to_markup",
        )
    }


def record_action(
    devtools: DevTools,
    outcomes: dict[str, dict[str, Any]],
    name: str,
    expected: str | None = None,
    before_action_count: int = 0,
    timeout: float = 15.0,
) -> None:
    action = name.split("_", 1)[0]
    pending = status_snapshot(devtools)
    if not pending["message"].startswith(f"{action} pending"):
        raise SmokeFailure(
            f"{name} did not expose a pending state: {pending['message']!r}"
        )
    result = wait_for_action(devtools, action, timeout=timeout)
    response = wait_for_action_response(devtools, action)
    payload = response.get("payload")
    if not isinstance(payload, Mapping):
        raise SmokeFailure(f"The {name} action response had no JSON payload")
    action_result = payload.get("action")
    if not isinstance(action_result, Mapping):
        raise SmokeFailure(f"The {name} action response omitted action state")
    expected_status = (
        "failed" if expected and expected.startswith("failed") else "succeeded"
    )
    if action_result.get("status") != expected_status:
        raise SmokeFailure(
            f"{name} returned action status {action_result.get('status')!r}, "
            f"expected {expected_status!r}"
        )
    if not isinstance(payload.get("state"), Mapping):
        raise SmokeFailure(f"The {name} action response omitted dashboard state")
    if expected_status == "succeeded" and not isinstance(
        payload.get("result"), Mapping
    ):
        raise SmokeFailure(f"The {name} action response omitted its result")
    if expected_status == "failed" and payload.get("result") is not None:
        raise SmokeFailure(
            f"The {name} failure response unexpectedly returned a result"
        )
    visible_actions = action_log_snapshot(devtools)
    if (
        not visible_actions["visible"]
        or len(visible_actions["rows"]) <= before_action_count
    ):
        raise SmokeFailure(f"The {name} action was not rendered in the action log")
    visible_dashboard = dashboard_snapshot(devtools)
    if not visible_dashboard.get("dashboard_text"):
        raise SmokeFailure(f"The {name} action left no visible dashboard state")
    outcomes[name] = {
        "pending_message": pending["message"],
        "pending_class_name": pending["class_name"],
        "result_message": result["message"],
        "result_class_name": result["class_name"],
        "http_status": response.get("status"),
        "action_status": action_result.get("status"),
        "returned_state": True,
        "result_present": payload.get("result") is not None,
        "visible_action_log": {
            "visible": visible_actions["visible"],
            "count": len(visible_actions["rows"]),
        },
        "visible_dashboard": {
            "project_name": visible_dashboard.get("project_name"),
            "updated": visible_dashboard.get("updated"),
            "repository_count": len(visible_dashboard.get("repositories", [])),
            "pbi_count": len(visible_dashboard.get("pbis", [])),
            "pipeline_stage_count": len(visible_dashboard.get("pipeline_stages", [])),
        },
    }
    if expected is not None and not result["message"].startswith(
        f"{action} {expected}"
    ):
        raise SmokeFailure(
            f"{name} returned {result['message']!r}, expected {expected!r}"
        )


def run_fixture_actions(devtools: DevTools) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    set_input(devtools, "#api-key", FIXTURE_API_KEY)
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_selector(devtools, "#start"):
        raise SmokeFailure("The dashboard sync button was not rendered")
    record_action(devtools, outcomes, "start_sync", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_repository_button(devtools, "owner/api", "Start writer"):
        raise SmokeFailure("The dashboard start-writer action was not rendered")
    record_action(devtools, outcomes, "start_writer", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Record approval"):
        raise SmokeFailure("The dashboard approval action was not rendered")
    record_action(devtools, outcomes, "approve", "succeeded.", before)

    devtools.evaluate("window.prompt = () => 'Use the deterministic fixture';")
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Request clarification"):
        raise SmokeFailure("The dashboard clarification action was not rendered")
    record_action(devtools, outcomes, "clarify", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Stop"):
        raise SmokeFailure("The dashboard stop action was not rendered")
    record_action(devtools, outcomes, "stop", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_repository_button(devtools, "owner/empty", "Start writer"):
        raise SmokeFailure("The dashboard failure fixture was not rendered")
    record_action(devtools, outcomes, "start_empty", "failed:", before)
    return outcomes


def run_live_actions(
    devtools: DevTools, api_key: str, timeout: float = 60.0
) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    set_input(devtools, "#api-key", api_key)
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_selector(devtools, "#start"):
        raise SmokeFailure("The dashboard sync button was not rendered")
    record_action(
        devtools, outcomes, "start_sync", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not has_button(devtools, "Start writer") or not click_button(
        devtools, "Start writer"
    ):
        raise SmokeFailure("The live dashboard did not render the start-writer action")
    record_action(
        devtools, outcomes, "start_writer", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not has_button(devtools, "Record approval") or not click_button(
        devtools, "Record approval"
    ):
        raise SmokeFailure("The live dashboard did not render the approval action")
    record_action(
        devtools, outcomes, "approve", before_action_count=before, timeout=timeout
    )

    devtools.evaluate("window.prompt = () => 'Live smoke clarification';")
    before = len(action_log_snapshot(devtools)["rows"])
    if not has_button(devtools, "Request clarification") or not click_button(
        devtools, "Request clarification"
    ):
        raise SmokeFailure("The live dashboard did not render the clarification action")
    record_action(
        devtools, outcomes, "clarify", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not has_button(devtools, "Stop") or not click_button(devtools, "Stop"):
        raise SmokeFailure("The live dashboard did not render the stop action")
    record_action(
        devtools, outcomes, "stop", before_action_count=before, timeout=timeout
    )
    return outcomes


def run_browser_smoke(
    devtools: DevTools,
    base_url: str,
    project_id: str,
    mode: str,
    allow_mutations: bool,
    provider: Any,
    secrets_to_redact: tuple[str, ...],
    server_output: io.StringIO,
) -> dict[str, Any]:
    report: dict[str, Any] = {"passed": False, "actions": {}}
    report["provider_identity"] = provider_identity_probe(project_id)
    initial_status = wait_for_status(devtools, "Updated ", "initial dashboard refresh")
    summary, dashboard = page_strings(devtools)
    initial_view = dashboard_snapshot(devtools)
    initial_payload = dashboard_payload(devtools)
    required_fields = required_dashboard_fields(initial_view)
    response_fields = response_backed_dashboard_fields(initial_payload, initial_view)
    missing_fields = [name for name, present in required_fields.items() if not present]
    missing_response_fields = [
        name for name, present in response_fields.items() if not present
    ]
    if missing_fields:
        raise SmokeFailure(
            "The dashboard omitted required rendered fields: "
            + ", ".join(missing_fields)
        )
    if missing_response_fields:
        raise SmokeFailure(
            "The dashboard did not match response fields: "
            + ", ".join(missing_response_fields)
        )
    api_key_value = devtools.evaluate("document.querySelector('#api-key')?.value || ''")
    if api_key_value:
        raise SmokeFailure("The initial read-only dashboard requested an API key")
    for expected in (
        "Projects",
        "Repositories",
        "PBIs",
        "Subtasks",
        "Writers",
        "Readers",
    ):
        if expected not in summary:
            raise SmokeFailure(f"The dashboard summary omitted {expected}")
    if mode == "fixture":
        for expected in (
            "Fixture Project",
            "Dashboard proof PBI",
            "Check API",
            "Tier terra",
        ):
            if expected not in dashboard:
                raise SmokeFailure(f"The fixture dashboard omitted {expected}")
    report["initial_refresh"] = {
        "ui": initial_status,
        "required_fields": required_fields,
        "response_backed_fields": response_fields,
        "project_name": initial_view["project_name"],
        "updated": initial_view["updated"],
        "counts": initial_view["counts"],
        "repositories": initial_view["repositories"][:5],
        "pbis": initial_view["pbis"][:5],
        "pipeline_stages": initial_view["pipeline_stages"][:5],
        "metadata_sections": initial_view["metadata_sections"][:10],
    }

    polling_baseline = dashboard_fetch_count(devtools)
    polling_delay = wait_until(
        devtools,
        f"""
(() => {{
  const records = (window.__beehaiiveSmokeFetches || [])
    .filter((record) => record.method === "GET"
      && record.url.includes("/projects/")
      && record.url.endsWith("/dashboard")
      && record.status === 200).length;
  const successful = (window.__beehaiiveSmokeFetches || [])
    .filter((record) => record.method === "GET"
      && record.url.includes("/projects/")
      && record.url.endsWith("/dashboard")
      && record.status === 200);
  const candidate = successful.at(-1);
  const pageStartedAt = window.__beehaiiveSmokePageStartedAt || 0;
  const delay = candidate ? candidate.started_at - pageStartedAt : 0;
  return records > {polling_baseline} && delay >= 4500 ? delay : 0;
}})()
""",
        "dashboard polling refresh",
        timeout=8.0,
    )
    if not isinstance(polling_delay, (int, float)):
        raise SmokeFailure("The dashboard polling timestamp was not recorded")
    report["polling"] = {
        "successful_dashboard_gets": dashboard_fetch_count(devtools),
        "new_successful_get_after_initial_refresh": True,
        "first_poll_delay_ms": round(float(polling_delay)),
    }
    discoveries_before_rejection = provider.discoveries

    invalid_project = "not-allowed:0"
    set_input(devtools, "#project-id", invalid_project, change=True)
    rejected_status = wait_for_status(
        devtools, "Project is not authorized", "unallowlisted project rejection"
    )
    if provider.discoveries != discoveries_before_rejection:
        raise SmokeFailure("The unallowlisted request reached the provider")
    report["unallowlisted_project"] = {
        "project_id": redact_project_id(invalid_project),
        "ui": rejected_status,
        "provider_access": "blocked before provider discovery",
    }

    set_input(devtools, "#project-id", project_id, change=True)
    wait_for_status(devtools, "Updated ", "dashboard refresh after rejection")
    if mode == "fixture":
        report["actions"] = run_fixture_actions(devtools)
    elif allow_mutations:
        if not os.environ.get("BEEHAIIVE_API_KEY"):
            raise SmokeFailure("Live mutation proof needs BEEHAIIVE_API_KEY")
        report["actions"] = run_live_actions(devtools, os.environ["BEEHAIIVE_API_KEY"])
    else:
        report["actions"] = {"skipped": "pass --allow-mutations for live action proof"}

    expected_api_key = (
        FIXTURE_API_KEY
        if mode == "fixture"
        else os.environ.get("BEEHAIIVE_API_KEY", "")
        if allow_mutations
        else ""
    )
    credential_surface = credential_surface_snapshot(devtools, expected_api_key)
    if expected_api_key and not (
        credential_surface["configured_key_in_password_input"]
        and credential_surface["configured_key_in_local_storage"]
    ):
        raise SmokeFailure(
            "The configured API key was not confined to the password input and storage"
        )
    if not expected_api_key and (
        credential_surface["input_value_present"]
        or credential_surface["storage_value_present"]
    ):
        raise SmokeFailure("An unexpected API key remained in the browser")
    if any(
        credential_surface[key]
        for key in (
            "input_value_leaked_to_visible_page",
            "input_value_leaked_to_markup",
            "storage_value_leaked_to_visible_page",
            "storage_value_leaked_to_markup",
        )
    ):
        raise SmokeFailure("An API key appeared in rendered dashboard content")
    visible_text, page_html = page_strings(devtools)
    sensitive_values = tuple(value for value in secrets_to_redact if value)
    if any(secret in visible_text for secret in sensitive_values):
        raise SmokeFailure("A credential appeared in the dashboard page")
    if any(secret in page_html for secret in sensitive_values):
        raise SmokeFailure("A credential appeared in the dashboard page")
    service_output = server_output.getvalue()
    redacted_service_output = redact_text(service_output, sensitive_values, project_id)
    if any(secret in service_output for secret in sensitive_values):
        raise SmokeFailure("The isolated service output contained a credential")
    storage_cleared = clear_credentials(devtools)
    if not storage_cleared:
        raise SmokeFailure("The browser did not clear the API key from local storage")
    report["credential_safety"] = {
        "api_key_or_provider_token_in_visible_page": False,
        "api_key_or_provider_token_in_page_markup": False,
        "service_output": {
            "captured": True,
            "redacted": True,
            "lines": len(redacted_service_output.splitlines()),
            "contains_secret": False,
        },
        "browser_storage_cleared": storage_cleared,
        "credential_surface": credential_surface,
        "report_values_redacted": True,
    }
    report["http"] = sanitize_fetches(browser_fetches(devtools), project_id)
    report["service_url"] = base_url
    report["project_id"] = redact_project_id(project_id)
    report["passed"] = True
    return report


def sanitize_fetches(value: Any, project_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    redacted_project = redact_project_id(project_id)
    encoded_project = quote(project_id, safe="")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url", ""))
        url = url.replace(project_id, redacted_project).replace(
            encoded_project, redacted_project
        )
        result.append(
            {
                "method": str(item.get("method", "GET")),
                "url": url,
                "status": item.get("status"),
                "ok": bool(item.get("ok", False)),
            }
        )
    return result


def redact_project_id(project_id: str) -> str:
    owner, separator, number = project_id.partition(":")
    if not separator:
        return "<redacted-project>"
    visible_owner = owner[:1] if owner else "?"
    visible_number = number if number.isdigit() else "<redacted-number>"
    return f"{visible_owner}***:{visible_number}"


def redact_text(
    value: str, secrets_to_redact: tuple[str, ...], project_id: str | None = None
) -> str:
    result = value
    for secret in secrets_to_redact:
        if secret:
            result = result.replace(secret, "<redacted>")
    if project_id:
        result = result.replace(project_id, redact_project_id(project_id))
        result = result.replace(
            quote(project_id, safe=""), redact_project_id(project_id)
        )
    return result


def sanitize_report(
    value: Any,
    secrets_to_redact: tuple[str, ...],
    project_id: str | None = None,
) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets_to_redact, project_id)
    if isinstance(value, Mapping):
        return {
            sanitize_report(key, secrets_to_redact, project_id): sanitize_report(
                item, secrets_to_redact, project_id
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_report(item, secrets_to_redact, project_id) for item in value]
    if isinstance(value, tuple):
        return tuple(
            sanitize_report(item, secrets_to_redact, project_id) for item in value
        )
    return value


def run_checks(
    secrets_to_redact: tuple[str, ...], project_id: str | None
) -> list[dict[str, Any]]:
    test_environment = os.environ.copy()
    for name in tuple(test_environment):
        if name.startswith(("BEEHAIIVE_", "GITHUB_")) or name == "GH_TOKEN":
            test_environment.pop(name)
    javascript_tests = sorted(REPOSITORY_ROOT.glob("tests/*.test.mjs"))
    commands = [
        ("python_tests", ["uv", "run", "pytest", "-q"]),
        ("javascript_tests", ["node", "--test", *map(str, javascript_tests)]),
        (
            "python_coverage",
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "--cov=main",
                "--cov=beehaiive",
                "--cov-report=term-missing",
                "--cov-fail-under=100",
            ],
        ),
        ("python_format", ["uv", "run", "ruff", "format", "--check", "."]),
        ("python_lint", ["uv", "run", "ruff", "check", "."]),
        ("python_type_check", ["uv", "run", "pyright"]),
    ]
    results: list[dict[str, Any]] = []
    for name, command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=test_environment,
                timeout=300,
                check=False,
            )
            output = f"{completed.stdout}\n{completed.stderr}".strip()
            result: dict[str, Any] = {
                "name": name,
                "command": " ".join(command),
                "passed": completed.returncode == 0,
                "exit_code": completed.returncode,
            }
            if completed.returncode != 0:
                result["tail"] = redact_text(
                    output[-1_000:], secrets_to_redact, project_id
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            result = {
                "name": name,
                "command": " ".join(command),
                "passed": False,
                "exit_code": None,
                "tail": redact_text(str(error), secrets_to_redact, project_id),
            }
        results.append(result)
    return results


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--project", help="Exact OWNER:NUMBER for live mode")
    parser.add_argument("--browser", help="Path or command name for Edge or Chrome")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.project:
        parser.error("--project OWNER:NUMBER is required in live mode")
    return args


def write_report(
    report: dict[str, Any],
    path: Path | None,
    secrets_to_redact: tuple[str, ...] = (),
    project_id: str | None = None,
) -> bool:
    safe_report = sanitize_report(report, secrets_to_redact, project_id)
    serialized = json.dumps(safe_report, ensure_ascii=True, indent=2, sort_keys=True)
    report_values_redacted = not any(
        secret and secret in serialized for secret in secrets_to_redact
    ) and not (
        project_id
        and (project_id in serialized or quote(project_id, safe="") in serialized)
    )
    if not report_values_redacted:
        serialized = json.dumps(
            {"report_values_redacted": False, "result": "failed"},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{serialized}\n", encoding="utf-8")
        print(serialized)
        return False
    browser = safe_report.get("browser")
    credential_safety = (
        browser.get("credential_safety") if isinstance(browser, Mapping) else None
    )
    if isinstance(credential_safety, dict):
        credential_safety["report_values_redacted"] = True
    serialized = json.dumps(safe_report, ensure_ascii=True, indent=2, sort_keys=True)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{serialized}\n", encoding="utf-8")
    print(serialized)
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_id = FIXTURE_PROJECT_ID if args.mode == "fixture" else str(args.project)
    live_api_key = os.environ.get("BEEHAIIVE_API_KEY", "")
    secrets_to_redact = tuple(
        value
        for value in (
            FIXTURE_API_KEY,
            live_api_key,
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GH_TOKEN", ""),
        )
        if value
    )
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "project_id": redact_project_id(project_id),
        "checks": [],
        "browser": {"passed": False},
        "result": "failed",
    }
    if args.skip_tests:
        report["checks"] = [{"skipped": True, "reason": "--skip-tests"}]
    else:
        report["checks"] = run_checks(secrets_to_redact, project_id)

    runtime_stores: list[Any] = []
    server: Any = None
    server_thread: threading.Thread | None = None
    browser_process: subprocess.Popen[bytes] | None = None
    devtools: DevTools | None = None
    cleanup_errors: list[str] = []
    directory = Path(tempfile.mkdtemp(prefix="beehaiive-dashboard-smoke-"))
    try:
        app, runtime_stores, provider = build_app(args.mode, project_id, directory)
        server, server_thread, base_url, server_output = start_server(app)
        browser_process, devtools = start_browser(
            base_url, project_id, args.browser, directory
        )
        report["browser"] = run_browser_smoke(
            devtools,
            base_url,
            project_id,
            args.mode,
            args.allow_mutations,
            provider,
            secrets_to_redact,
            server_output,
        )
    except Exception as error:
        report["browser"] = {
            "passed": False,
            "error": redact_text(str(error), secrets_to_redact, project_id),
        }
    finally:
        if devtools is not None:
            try:
                devtools.close()
            except Exception as error:
                cleanup_errors.append(str(error))
        if browser_process is not None:
            try:
                terminate_process(browser_process)
            except Exception as error:
                cleanup_errors.append(str(error))
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                cleanup_errors.append("The isolated service thread did not terminate")
        cleanup_errors.extend(close_stores(runtime_stores))
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except OSError as error:
                cleanup_errors.append(str(error))
    report["cleanup"] = {
        "passed": not cleanup_errors,
        "errors": [
            redact_text(error, secrets_to_redact, project_id)
            for error in cleanup_errors
        ],
    }

    checks_passed = (
        all(result.get("passed", False) for result in report["checks"])
        and report["cleanup"]["passed"]
    )
    report["result"] = (
        "passed" if checks_passed and report["browser"].get("passed") else "failed"
    )
    report_written_safely = write_report(
        report, args.report, secrets_to_redact, project_id
    )
    return 0 if report["result"] == "passed" and report_written_safely else 1


if __name__ == "__main__":
    raise SystemExit(main())
