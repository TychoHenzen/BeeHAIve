"""Chromium DevTools transport and browser interaction helpers."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from .dashboard_smoke_types import SmokeFailure


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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


def click_pbi_button(
    devtools: DevTools,
    repository: str,
    pbi_number: int,
    run_id: str,
    label: str,
) -> bool:
    return bool(
        devtools.evaluate(
            f"""
(() => {{
  const pbi = [...document.querySelectorAll(".pbi")].find((node) =>
    node.dataset.repository === {json.dumps(repository)}
    && node.dataset.pbiNumber === {json.dumps(str(pbi_number))}
    && node.dataset.runId === {json.dumps(run_id)}
  );
  const button = [...(pbi?.querySelectorAll("button") || [])]
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
    applied = devtools.evaluate(
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
    if not applied:
        raise SmokeFailure(f"Dashboard input {selector} was not rendered")


def wait_for_status(
    devtools: DevTools,
    fragment: str,
    label: str,
    timeout: float = 15.0,
) -> dict[str, str]:
    wait_until(
        devtools,
        "(document.querySelector('#state-status')?.textContent || '')"
        f".includes({json.dumps(fragment)})",
        label,
        timeout=timeout,
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
