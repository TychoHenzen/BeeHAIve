"""Run a credential-safe browser proof of the BeeHAIve dashboard."""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.dashboard_smoke_browser import DevTools, terminate_process
from scripts.dashboard_smoke_proof import run_browser_smoke
from scripts.dashboard_smoke_report import (
    redact_project_id,
    redact_text,
    run_checks,
    sanitize_fetches,
    sanitize_report,
)
from scripts.dashboard_smoke_report import (
    write_report as _write_report,
)
from scripts.dashboard_smoke_runtime import (
    ApplicationResources,
    DiscoveryCountingProvider,
    FixtureProvider,
    SmokeEnvironment,
    build_app,
    close_stores,
    fixture_snapshot,
    isolated_module_environment,
    provider_identity_probe,
    start_server,
)
from scripts.dashboard_smoke_types import (
    FIXTURE_API_KEY,
    FIXTURE_PROJECT_ID,
    SmokeFailure,
)

__all__ = [
    "ApplicationResources",
    "DevTools",
    "DiscoveryCountingProvider",
    "FIXTURE_API_KEY",
    "FIXTURE_PROJECT_ID",
    "FixtureProvider",
    "SmokeEnvironment",
    "SmokeFailure",
    "build_app",
    "close_stores",
    "fixture_snapshot",
    "isolated_module_environment",
    "main",
    "parse_args",
    "provider_identity_probe",
    "redact_project_id",
    "redact_text",
    "run_checks",
    "run_browser_smoke",
    "sanitize_fetches",
    "sanitize_report",
    "start_server",
    "terminate_process",
    "write_report",
]


def write_report(
    report: dict[str, Any],
    path: Path | None,
    secrets_to_redact: tuple[str, ...] = (),
    project_id: str | None = None,
) -> bool:
    return _write_report(
        report,
        path,
        secrets_to_redact,
        project_id,
        sanitizer=sanitize_report,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--project", help="Exact OWNER:NUMBER for live mode")
    parser.add_argument("--browser", help="Path or command name for Edge or Chrome")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument(
        "--live-timeout",
        type=float,
        help="Maximum seconds for live provider refresh and actions",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.project:
        parser.error("--project OWNER:NUMBER is required in live mode")
    if args.live_timeout is not None and args.live_timeout <= 0:
        parser.error("--live-timeout must be greater than zero")
    return args


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

    environment = SmokeEnvironment(args.mode, project_id, args.browser)
    cleanup_errors: list[str] = []
    try:
        environment.start()
        if environment.devtools is None or environment.server_output is None:
            raise SmokeFailure("The smoke environment did not start completely")
        report["browser"] = run_browser_smoke(
            environment.devtools,
            environment.base_url,
            project_id,
            args.mode,
            args.allow_mutations,
            environment.provider,
            secrets_to_redact,
            environment.server_output,
            live_timeout=args.live_timeout,
        )
    except Exception as error:
        report["browser"] = {
            "passed": False,
            "error": redact_text(str(error), secrets_to_redact, project_id),
        }
    finally:
        cleanup_errors.extend(environment.close())

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
