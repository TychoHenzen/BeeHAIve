from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DOCS), **kwargs)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/dashboard":
            self.path = "/dashboard.html"
        super().do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def browser_executable() -> str | None:
    configured = os.environ.get("BEEHAIIVE_PLAYWRIGHT_BROWSER")
    candidates = [
        configured,
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/microsoft-edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


@pytest.fixture
def dashboard_page() -> Iterator[tuple[Page, str]]:
    executable = browser_executable()
    with sync_playwright() as playwright:
        try:
            launch_options = {"headless": True}
            if executable is not None:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError:
            pytest.skip(
                "Set BEEHAIIVE_PLAYWRIGHT_BROWSER or run playwright install chromium"
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            yield page, base_url
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def work_item(page: Page, title: str):
    return page.get_by_test_id("work-item").filter(has_text=title)


@pytest.mark.e2e
def test_guided_demo_covers_plan_to_ship(dashboard_page) -> None:
    page, base_url = dashboard_page
    page.goto(f"{base_url}/dashboard?demo=true")

    expect(page.locator("#project-switcher")).to_contain_text("beehaive")
    expect(page.get_by_role("heading", name="Queue")).to_be_visible()
    for heading in ("Needs you", "Agents", "Recent activity", "Recent deliveries"):
        expect(page.get_by_role("heading", name=heading)).to_be_visible()
    expect(page.locator("#deliveries-output")).to_contain_text("Harden session expiry")
    page.get_by_role("button", name="omelette", exact=True).click()
    expect(page.locator("#queue-table")).to_contain_text("Harden session expiry")
    expect(page.locator("#queue-table")).not_to_contain_text("Add account recovery")
    page.get_by_role("button", name="All projects", exact=True).click()
    assert page.locator("#api-key").count() == 0
    assert page.locator(".graph-editor").count() == 0
    assert page.evaluate("localStorage.getItem('beehaiive-api-key')") is None

    item = work_item(page, "Add account recovery")
    item.get_by_test_id("run-lifecycle").click()
    expect(page.locator("#state-status")).to_contain_text(
        "autonomous lifecycle completed."
    )
    expect(page.locator("#queue-table")).not_to_contain_text("Add account recovery")
    page.locator("#filters").get_by_role(
        "button", name=re.compile(r"^Archived")
    ).click()
    archived_item = work_item(page, "Add account recovery")
    expect(archived_item).to_contain_text("Delivered")
    archived_item.get_by_test_id("inspect-work").click()
    expect(page.locator("#details-pane")).to_contain_text(
        "Placeholder lifecycle completed"
    )
    expect(page.locator("#details-pane")).to_contain_text("Skill handoffs")
    for skill in (
        "refine-backlog-item",
        "next-ticket",
        "submit-draft-pr",
        "review-pr-branch",
        "fix-pr-review",
        "complete-pr",
    ):
        expect(page.locator("#details-pane")).to_contain_text(skill)


@pytest.mark.e2e
def test_guided_demo_recovers_from_operator_question(dashboard_page) -> None:
    page, base_url = dashboard_page
    page.goto(f"{base_url}/dashboard?demo=true")

    item = work_item(page, "Choose deployment target")
    expect(
        page.get_by_text("Which environment should receive the preview build?")
    ).to_be_visible()
    page.get_by_test_id("answer-attention").click()
    inspector = page.locator("#details-pane")
    inspector.get_by_label("Answer this question", exact=True).fill("preview")
    inspector.get_by_test_id("answer-question").click()

    expect(item).to_contain_text("Build")
    expect(page.locator("#state-status")).to_contain_text("answer_question succeeded.")


@pytest.mark.e2e
def test_real_mode_uses_server_owned_auth_for_actions(dashboard_page) -> None:
    page, base_url = dashboard_page
    state = {
        "project_id": "owner:1",
        "name": "Server project",
        "updated_at": "now",
        "repositories": [
            {
                "name": "owner/app",
                "active": True,
                "writer": {"status": "idle"},
                "pbis": [
                    {
                        "number": 1,
                        "title": "Server-backed work",
                        "stage": "backlog",
                        "stage_label": "Plan",
                        "status": "idle",
                        "active": True,
                        "claimable": True,
                        "run_id": None,
                        "stage_progress": [],
                        "operator_questions": [],
                        "subtasks": [],
                    }
                ],
            }
        ],
        "actions": [],
    }
    observed_headers: list[dict[str, str]] = []

    def api(route) -> None:
        request = route.request
        if request.method == "POST":
            observed_headers.append(dict(request.headers))
            body = request.post_data_json
            assert body["action"] == "start"
            assert body["pbi_number"] == 1
            state["repositories"][0]["pbis"][0].update(
                {
                    "status": "active",
                    "stage": "refine",
                    "stage_label": "Refine",
                    "claimable": False,
                    "run_id": "run-1",
                }
            )
            state["repositories"][0]["writer"] = {
                "status": "active",
                "current_pbi": 1,
            }
            payload = {
                "action": {"kind": "start", "status": "succeeded"},
                "result": {"run": {"run_id": "run-1"}},
                "state": state,
            }
        else:
            payload = state
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/projects/**", api)
    page.goto(f"{base_url}/dashboard?project=owner:1")
    item = work_item(page, "Server-backed work")
    item.get_by_test_id("start-work").click()
    expect(item).to_contain_text("Refine")
    assert observed_headers
    headers = observed_headers[0]
    assert headers.get("x-beehaive-dashboard") == "1"
    assert "x-api-key" not in headers


@pytest.mark.e2e
def test_evidence_log_shows_latest_server_actions(dashboard_page) -> None:
    page, base_url = dashboard_page
    actions = [
        {
            "kind": "start",
            "status": "failed",
            "repository": "owner/app",
            "pbi_number": index,
            "created_at": f"2026-09-09T20:00:{index:02d}Z",
            "error": "latest launch detail" if index == 9 else f"old-{index}",
        }
        for index in range(9, 0, -1)
    ]
    state = {
        "project_id": "owner:1",
        "name": "Server project",
        "updated_at": "now",
        "counts": {},
        "repositories": [],
        "actions": actions,
    }

    def api(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(state),
        )

    page.route("**/projects/**", api)
    page.goto(f"{base_url}/dashboard?project=owner:1")
    page.get_by_role("button", name="Evidence log", exact=True).click()
    expect(page.locator("#activity-output")).to_contain_text("latest launch detail")
    expect(page.locator("#activity-output")).not_to_contain_text("old-1")


@pytest.mark.e2e
def test_agents_exposes_retry_for_blocked_autonomous_work(dashboard_page) -> None:
    page, base_url = dashboard_page
    state = {
        "project_id": "owner:1",
        "name": "Server project",
        "updated_at": "now",
        "counts": {},
        "repositories": [
            {
                "name": "owner/app",
                "active": True,
                "writer": {"status": "idle"},
                "pbis": [
                    {
                        "number": 1,
                        "title": "Blocked autonomous work",
                        "status": "failed",
                        "autonomous_status": "blocked",
                        "last_error": "Launch failed with context",
                    }
                ],
            }
        ],
        "actions": [],
    }

    def api(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(state),
        )

    page.route("**/projects/**", api)
    page.goto(f"{base_url}/dashboard?project=owner:1")
    page.get_by_role("button", name="Agents", exact=True).click()
    expect(page.locator("#agents-output")).to_contain_text("Blocked autonomous work")
    expect(
        page.locator("#agents-output").get_by_test_id("retry-lifecycle")
    ).to_be_visible()


@pytest.mark.e2e
def test_real_mode_wires_autonomous_lifecycle_endpoint(dashboard_page) -> None:
    page, base_url = dashboard_page
    state = {
        "project_id": "owner:1",
        "name": "Server project",
        "updated_at": "now",
        "repositories": [
            {
                "name": "owner/app",
                "active": True,
                "writer": {"status": "idle"},
                "pbis": [
                    {
                        "number": 1,
                        "title": "Autonomous lifecycle work",
                        "stage": "backlog",
                        "status": "idle",
                        "active": True,
                        "claimable": True,
                        "run_id": None,
                        "operator_questions": [],
                        "subtasks": [],
                    }
                ],
            }
        ],
        "actions": [],
    }
    observed: list[dict[str, object]] = []

    def api(route) -> None:
        request = route.request
        if request.method == "POST":
            assert request.url.endswith("/autonomous-runs")
            observed.append(
                {"headers": dict(request.headers), "body": request.post_data_json}
            )
            pbi = state["repositories"][0]["pbis"][0]
            pbi.update(
                {
                    "status": "active",
                    "claimable": False,
                    "run_id": "auto-1",
                }
            )
            state["actions"].append({"kind": "autonomous_start", "status": "succeeded"})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "run_id": "auto-1",
                        "project_id": "owner:1",
                        "repository": "owner/app",
                        "pbi_number": 1,
                        "status": "running",
                    }
                ),
            )
            return
        if request.url.endswith("/autonomous-runs/auto-1"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "run_id": "auto-1",
                        "project_id": "owner:1",
                        "repository": "owner/app",
                        "pbi_number": 1,
                        "status": "completed",
                        "current_step": None,
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(state),
        )

    page.route("**/projects/**", api)
    page.goto(f"{base_url}/dashboard?project=owner:1")
    item = work_item(page, "Autonomous lifecycle work")
    item.get_by_test_id("run-lifecycle").click()
    expect(item).to_contain_text("Running")
    expect(page.locator("#activity-output")).to_contain_text(
        "Autonomous lifecycle started"
    )
    page.get_by_role("button", name="Agents", exact=True).click()
    expect(page.locator("#agents-output")).to_contain_text("autonomous")
    assert len(observed) == 1
    assert observed[0]["body"] == {
        "repository": "owner/app",
        "pbi_number": 1,
        "approved": True,
    }
    headers = observed[0]["headers"]
    assert headers.get("x-beehaive-dashboard") == "1"
    assert "x-api-key" not in headers


@pytest.mark.e2e
def test_guided_demo_can_stop_and_retry_work(dashboard_page) -> None:
    page, base_url = dashboard_page
    page.goto(f"{base_url}/dashboard?demo=true")

    item = work_item(page, "Add account recovery")
    item.get_by_test_id("start-work").click()
    item.get_by_test_id("inspect-work").click()
    inspector = page.locator("#details-pane")
    inspector.get_by_test_id("detail-tab-session").click()
    expect(inspector).to_contain_text("Session")
    inspector.get_by_test_id("detail-tab-checks").click()
    page.locator("#details-pane").get_by_test_id("stop-work").click()

    expect(item).to_contain_text("Blocked")
    expect(inspector).to_contain_text("Failure")
    expect(inspector).to_contain_text("Stopped by operator")
    page.get_by_role("button", name="Evidence log", exact=True).click()
    expect(page.locator("#activity-output")).to_contain_text(
        "Reason: Stopped by operator"
    )
    page.locator("#details-pane").get_by_test_id("retry-work").click()
    expect(item).to_contain_text("Running")


@pytest.mark.e2e
def test_sidebar_pages_and_settings_are_real_views(dashboard_page) -> None:
    page, base_url = dashboard_page
    page.goto(f"{base_url}/dashboard?demo=true#mission")

    for page_name, nav_name, title in (
        ("queue", "Queue", "Queue"),
        ("agents", "Agents", "Agents"),
        ("deliveries", "Deliveries", "Deliveries"),
        ("workflow-graphs", "Workflow graphs", "Workflow graphs"),
        ("projects", "Projects", "Projects"),
        ("evidence", "Evidence log", "Evidence log"),
        ("settings", "Settings", "Settings"),
    ):
        page.get_by_role("button", name=nav_name, exact=True).click()
        expect(page).to_have_url(re.compile(rf"#{re.escape(page_name)}$"))
        expect(page.locator("#page-title")).to_have_text(title)

    expect(page.locator("#settings-project-id")).to_have_value("demo:1")
    expect(page.locator("#settings-workflow-id")).to_have_value("demo-flow")
    assert page.locator("#project-id").count() == 0
    assert page.locator("#api-key").count() == 0
    expect(page.locator("#settings-page")).to_contain_text("Server managed")
    page.locator("#settings-max-workers").fill("3")
    page.locator("#settings-poll-interval").fill("30")
    page.get_by_test_id("scheduler-configure").click()
    expect(page.locator("#scheduler-feedback")).to_contain_text(
        "Scheduler settings applied."
    )
    expect(page.locator("#scheduler-card")).to_contain_text("3 workers")

    page.get_by_role("button", name="Workflow graphs", exact=True).click()
    expect(page.locator("#graph-output")).to_contain_text("Revision 1")
    expect(page.locator("#graph-output")).to_contain_text("claim")
    page.locator("#workflow-create-id").fill("new-flow")
    page.get_by_role("button", name="Create workflow draft", exact=True).click()
    expect(page.locator("#workflow-create-feedback")).to_contain_text(
        "Workflow draft created and evaluated."
    )
    assert page.locator("#workflow-select option").count() == 3
    expect(page.locator("#graph-output")).to_contain_text("new-flow")
    page.locator("#workflow-select").select_option("demo-flow")
    assert page.locator("#graph-output textarea").count() == 0
    for testid in ("graph-evaluate", "graph-review", "graph-activate"):
        page.get_by_test_id(testid).click()
        expect(page.locator("#state-status")).to_contain_text(
            f"graph_{testid.removeprefix('graph-')} succeeded."
        )
