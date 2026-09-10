"""Capture redacted dashboard state evidence from the deterministic browser proof."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from beehaiive.models import Stage
from scripts.dashboard_smoke_browser import (
    DevTools,
    click_button,
    click_selector,
    set_input,
    wait_for_action,
    wait_for_action_response,
    wait_for_status,
    wait_until,
)
from scripts.dashboard_smoke_runtime import SmokeEnvironment
from scripts.dashboard_smoke_types import (
    FIXTURE_API_KEY,
    FIXTURE_PROJECT_ID,
    SmokeFailure,
)

SCREENSHOT_DIRECTORY = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def configure_view(devtools: DevTools) -> None:
    """Use a stable viewport and remove fixture identifiers before capture."""

    devtools.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 1440,
            "height": 1100,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )
    redacted = devtools.evaluate(
        """
(() => {
  const replacements = [
    ["Fixture Project", "Demo Project"],
    ["fixture:1", "redacted:2"],
    ["owner/api", "redacted/repository"],
    ["owner/empty", "redacted/empty"],
    ["Dashboard proof PBI", "Bounded demo PBI"],
  ];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    for (const [source, target] of replacements) {
      node.nodeValue = node.nodeValue.split(source).join(target);
    }
    node = walker.nextNode();
  }
  for (const node of document.querySelectorAll("[data-repository]")) {
    node.dataset.repository = "redacted/repository";
  }
  for (const node of document.querySelectorAll("[data-run-id]")) {
    node.dataset.runId = "run-redacted";
  }
  const projectInput = document.querySelector("#project-id");
  if (projectInput) projectInput.value = "redacted:2";
  const apiInput = document.querySelector("#api-key");
  if (apiInput) apiInput.value = "";
  return true;
})()
"""
    )
    if redacted is not True:
        raise SmokeFailure("The screenshot redaction script did not run")


def capture(devtools: DevTools, filename: str) -> None:
    """Write one full-page PNG without exposing browser or service logs."""

    result = devtools.command(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": True},
    )
    data = result.get("data")
    if not isinstance(data, str):
        raise SmokeFailure(f"The browser returned no PNG for {filename}")
    SCREENSHOT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    (SCREENSHOT_DIRECTORY / filename).write_bytes(base64.b64decode(data))


def sync_dashboard(devtools: DevTools) -> None:
    wait_for_status(devtools, "Updated ", "initial screenshot dashboard")
    set_input(devtools, "#api-key", FIXTURE_API_KEY)
    if not click_selector(devtools, "#start"):
        raise SmokeFailure("The screenshot dashboard did not render sync")
    wait_for_action(devtools, "start")
    wait_for_action_response(devtools, "start")


def reload_dashboard(devtools: DevTools) -> None:
    devtools.command("Page.reload", {"ignoreCache": True})
    wait_until(
        devtools,
        "Boolean(document.querySelector('.repo'))",
        "dashboard reload",
    )


def start_writer(devtools: DevTools) -> tuple[str, int, str]:
    if not click_button(devtools, "Start writer"):
        raise SmokeFailure("The screenshot dashboard did not render Start writer")
    wait_for_action(devtools, "start")
    response = wait_for_action_response(devtools, "start")
    payload = response.get("payload")
    if not isinstance(payload, Mapping):
        raise SmokeFailure("The screenshot start response omitted its payload")
    payload_map = cast(Mapping[str, object], payload)
    result = payload_map.get("result")
    if not isinstance(result, Mapping):
        raise SmokeFailure("The screenshot start response omitted its result")
    result_map = cast(Mapping[str, object], result)
    run = result_map.get("run")
    if not isinstance(run, Mapping):
        raise SmokeFailure("The screenshot start response omitted its run")
    run_map = cast(Mapping[str, object], run)
    repository = run_map.get("repository")
    pbi_number = run_map.get("pbi_number")
    run_id = run_map.get("run_id")
    if (
        not isinstance(repository, str)
        or not isinstance(pbi_number, int)
        or not isinstance(run_id, str)
    ):
        raise SmokeFailure("The screenshot start response omitted run identity")
    return repository, pbi_number, run_id


def capture_configured_active_completed() -> None:
    environment = SmokeEnvironment("fixture", FIXTURE_PROJECT_ID, None)
    try:
        environment.start()
        if environment.devtools is None or environment.resources is None:
            raise SmokeFailure("The screenshot environment did not start")
        devtools = environment.devtools
        sync_dashboard(devtools)
        configure_view(devtools)
        capture(devtools, "dashboard-configured.png")

        reload_dashboard(devtools)
        set_input(devtools, "#api-key", FIXTURE_API_KEY)
        _, _, run_id = start_writer(devtools)
        configure_view(devtools)
        capture(devtools, "active-demo.png")

        reload_dashboard(devtools)
        store = environment.resources.stores[0]
        run = store.get_run(run_id)
        if run is None or run.lease_token is None:
            raise SmokeFailure("The screenshot run has no active lease")
        run = store.advance(run_id, Stage.IMPLEMENT, run.lease_token)
        store.complete_agent_run(
            run_id,
            "Repository: redacted/repository\nBranch: codex/demo\nTracked files: 42",
            run.lease_token or "",
        )
        wait_until(
            devtools,
            "(document.querySelector('.pbi')?.textContent || '').includes('Result:')",
            "completed screenshot result",
        )
        configure_view(devtools)
        capture(devtools, "completed-demo.png")
    finally:
        errors = environment.close()
        if errors:
            raise SmokeFailure("Screenshot environment cleanup failed")


def capture_stopped() -> None:
    environment = SmokeEnvironment("fixture", FIXTURE_PROJECT_ID, None)
    try:
        environment.start()
        if environment.devtools is None:
            raise SmokeFailure("The stopped screenshot environment did not start")
        devtools = environment.devtools
        sync_dashboard(devtools)
        start_writer(devtools)
        if not click_button(devtools, "Stop"):
            raise SmokeFailure("The screenshot dashboard did not render Stop")
        wait_for_action(devtools, "stop")
        wait_for_action_response(devtools, "stop")
        configure_view(devtools)
        capture(devtools, "stopped-demo.png")
    finally:
        errors = environment.close()
        if errors:
            raise SmokeFailure("Stopped screenshot cleanup failed")


def main() -> None:
    capture_configured_active_completed()
    capture_stopped()
    for path in sorted(SCREENSHOT_DIRECTORY.glob("*.png")):
        print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
