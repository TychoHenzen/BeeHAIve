import subprocess

import pytest

import beehaiive.routing as routing_module
import beehaiive.storage as storage_module
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
