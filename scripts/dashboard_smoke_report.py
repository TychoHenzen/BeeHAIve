"""Credential-safe report sanitization and check execution."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


ReportSanitizer = Callable[[Any, tuple[str, ...], str | None], Any]


def write_report(
    report: dict[str, Any],
    path: Path | None,
    secrets_to_redact: tuple[str, ...] = (),
    project_id: str | None = None,
    sanitizer: ReportSanitizer = sanitize_report,
) -> bool:
    safe_report = sanitizer(report, secrets_to_redact, project_id)
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
