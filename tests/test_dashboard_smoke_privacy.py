import json
import subprocess

import scripts.dashboard_smoke as dashboard_smoke
import scripts.dashboard_smoke_report as dashboard_smoke_report
from scripts.dashboard_smoke import (
    redact_project_id,
    redact_text,
    sanitize_fetches,
    sanitize_report,
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


def test_smoke_checks_do_not_impose_a_wall_clock_timeout(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dashboard_smoke_report.subprocess, "run", run)

    results = dashboard_smoke_report.run_checks((), "owner:1")

    assert len(results) == 6
    assert all("timeout" not in kwargs for kwargs in calls)
