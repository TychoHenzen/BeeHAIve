import json
import subprocess

import pytest

import beehaiive.routing as routing_module
import beehaiive.storage as storage_module
import scripts.dashboard_smoke as dashboard_smoke
from beehaiive.provider import ITEMS_QUERY
from scripts.dashboard_smoke import (
    SmokeFailure,
    build_app,
    redact_project_id,
    redact_text,
    sanitize_fetches,
    sanitize_report,
    terminate_process,
)
from scripts.dashboard_smoke_browser import set_input
from scripts.dashboard_smoke_proof import (
    action_run_target,
    configured_live_timeout,
    response_backed_dashboard_fields,
)


def test_project_identity_redaction_preserves_only_a_safe_number() -> None:
    assert redact_project_id("owner:7") == "o***:7"
    assert redact_project_id("owner:not-a-number") == "o***:<redacted-number>"
    assert redact_project_id("unknown") == "<redacted-project>"


def test_redact_text_removes_secrets_and_project_identity() -> None:
    value = redact_text(
        "key=secret-token project=owner:7",
        ("secret-token",),
        "owner:7",
    )

    assert "secret-token" not in value
    assert "owner:7" not in value
    assert "o***:7" in value


def test_fetch_report_redacts_raw_and_encoded_project_paths() -> None:
    value = sanitize_fetches(
        [
            {"method": "GET", "url": "/projects/owner:7/dashboard", "status": 200},
            {"method": "POST", "url": "/projects/owner%3A7/actions", "status": 200},
        ],
        "owner:7",
    )

    assert value == [
        {
            "method": "GET",
            "url": "/projects/o***:7/dashboard",
            "status": 200,
            "ok": False,
        },
        {
            "method": "POST",
            "url": "/projects/o***:7/actions",
            "status": 200,
            "ok": False,
        },
    ]


def test_report_sanitization_redacts_nested_dashboard_values() -> None:
    value = sanitize_report(
        {
            "project_name": "secret-token",
            "repositories": ["owner:7"],
            "metadata": {"title": "secret-token"},
        },
        ("secret-token",),
        "owner:7",
    )

    assert value == {
        "project_name": "<redacted>",
        "repositories": ["o***:7"],
        "metadata": {"title": "<redacted>"},
    }


def test_write_report_fails_closed_when_sanitization_leaves_a_secret(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setattr(
        dashboard_smoke,
        "sanitize_report",
        lambda *_args: {"value": "secret-token"},
    )
    report_path = tmp_path / "report.json"

    assert not dashboard_smoke.write_report(
        {"value": "ignored"},
        report_path,
        ("secret-token",),
        "owner:7",
    )

    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert json.loads(output) == {
        "report_values_redacted": False,
        "result": "failed",
    }
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "report_values_redacted": False,
        "result": "failed",
    }


def test_build_app_closes_stores_when_construction_fails(monkeypatch, tmp_path) -> None:
    class TrackedStore:
        instances = []

        def __init__(self, database) -> None:
            self.database = database
            self.closed = False
            self.__class__.instances.append(self)

        def close(self) -> None:
            self.closed = True

    def fail_routing_store(*args, **kwargs):
        raise RuntimeError("routing construction failed")

    monkeypatch.setattr(storage_module, "OrchestratorStore", TrackedStore)
    monkeypatch.setattr(routing_module, "RoutingStore", fail_routing_store)

    with pytest.raises(RuntimeError, match="routing construction failed"):
        build_app("fixture", "fixture:1", tmp_path)

    assert len(TrackedStore.instances) == 1
    assert TrackedStore.instances[0].closed is True


def test_terminate_process_reports_a_stuck_browser() -> None:
    class StuckProcess:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float) -> None:
            raise subprocess.TimeoutExpired("browser", timeout)

    with pytest.raises(SmokeFailure, match="did not terminate"):
        terminate_process(StuckProcess())


def test_project_query_bounds_nested_connections() -> None:
    assert "labels(first: 20)" in ITEMS_QUERY
    assert "subIssues(first: 20)" in ITEMS_QUERY
    assert "comments(first: 20)" in ITEMS_QUERY
    assert (
        "closedByPullRequestsReferences(includeClosedPrs: true, "
        "first: 20)" in ITEMS_QUERY
    )
    assert "reviewRequests(first: 20)" in ITEMS_QUERY
    assert "latestReviews(first: 20)" in ITEMS_QUERY


def test_response_dashboard_fields_require_the_requested_project() -> None:
    fields = response_backed_dashboard_fields(
        {"project_id": "owner:8"},
        {},
        "owner:7",
    )

    assert fields["project_id"] is False


def test_action_target_uses_the_run_returned_by_the_action() -> None:
    response = {
        "payload": {
            "result": {
                "run": {
                    "repository": "owner/api",
                    "pbi_number": 7,
                    "run_id": "run-7",
                }
            }
        }
    }

    assert action_run_target(response, "start_writer") == (
        "owner/api",
        7,
        "run-7",
    )


def test_set_input_fails_at_a_missing_control() -> None:
    class MissingInputDevTools:
        def evaluate(self, expression: str):
            return False

    with pytest.raises(SmokeFailure, match="was not rendered"):
        set_input(MissingInputDevTools(), "#project-id", "owner:7")


def test_live_timeout_uses_provider_deadline_or_explicit_value() -> None:
    assert configured_live_timeout(None) == 65.0
    assert configured_live_timeout(90.0) == 90.0
