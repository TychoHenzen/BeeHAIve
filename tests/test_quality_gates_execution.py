from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from beehaiive.agent import (
    CodexExecModelExecutor,
    safe_worker_environment,
)
from beehaiive.quality_gates import MANIFEST_NAME, RepositoryGateSuite
from tests.support.quality_gates.helpers import make_gate, write_manifest

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_repository_gate_suite_reports_bounded_redacted_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    secret = "gate-secret-for-redaction"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    write_manifest(
        workspace,
        [
            make_gate("pass", [sys.executable, "-c", "print('ready')"]),
            make_gate("failure", [sys.executable, "-c", "import sys; sys.exit(7)"]),
            make_gate(
                "timeout",
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.05,
            ),
            make_gate("missing", ["beehaiive-test-executable-not-installed"]),
            make_gate(
                "environment",
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('GITHUB_TOKEN', 'not-present'))",
                ],
            ),
            make_gate("large-output", [sys.executable, "-c", "print('x' * 100000)"]),
            make_gate(
                "codeql",
                [],
                required=False,
                external_only=True,
                category="security",
            ),
        ],
    )

    results = RepositoryGateSuite().run(workspace)
    by_name = {result.name: result for result in results}

    assert [result.name for result in results] == [
        "pass",
        "failure",
        "timeout",
        "missing",
        "environment",
        "large-output",
        "codeql",
    ]
    assert by_name["pass"].status == "passed"
    assert by_name["failure"].status == "failed"
    assert by_name["failure"].exit_code == 7
    assert by_name["timeout"].status == "timed_out"
    assert by_name["timeout"].exit_code is not None
    assert by_name["missing"].status == "unavailable"
    assert by_name["environment"].stdout.strip() == "not-present"
    assert by_name["large-output"].stdout.startswith("x")
    assert len(by_name["large-output"].stdout) <= 4_000
    assert by_name["codeql"].status == "external_only"
    assert by_name["codeql"].passed is False
    assert by_name["codeql"].required is False
    assert secret not in json.dumps([result.as_dict() for result in results])


def test_repository_gate_suite_redacts_secret_child_output(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    secret = "gate-secret-for-redaction"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    original_environment = safe_worker_environment()
    monkeypatch.setattr(
        "beehaiive.workflows.repository_gate_suite.safe_worker_environment",
        lambda: {**original_environment, "GITHUB_TOKEN": secret},
    )
    write_manifest(
        workspace,
        [
            make_gate(
                "redaction",
                [
                    sys.executable,
                    "-c",
                    "import os; print('GITHUB_TOKEN=' + os.environ['GITHUB_TOKEN'])",
                ],
            )
        ],
    )

    assert secret not in (workspace / MANIFEST_NAME).read_text(encoding="utf-8")
    result = RepositoryGateSuite().run(workspace)[0]

    assert result.stdout.strip() == "GITHUB_TOKEN=[redacted]"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process containment")
def test_windows_gate_runner_does_not_load_checkout_startup_hooks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    marker = workspace / "sitecustomize-loaded.txt"
    (workspace / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('loaded')",
        encoding="utf-8",
    )
    write_manifest(
        workspace, [make_gate("windows-no-hook", ["cmd.exe", "/c", "exit 0"])]
    )

    result = RepositoryGateSuite().run(workspace)

    assert result[0].status == "passed"
    assert not marker.exists()


def test_timed_out_gate_terminates_child_process_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    marker = workspace / "child-finished.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(3); "
        f"Path({str(marker)!r}).write_text('finished')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(5)"
    )
    write_manifest(
        workspace,
        [
            make_gate(
                "process-tree",
                [sys.executable, "-c", parent],
                timeout_seconds=0.1,
            )
        ],
    )

    result = RepositoryGateSuite().run(workspace)
    time.sleep(3.5)

    assert result[0].status == "timed_out"
    assert not marker.exists()


def test_completed_gate_terminates_child_process_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    marker = workspace / "child-finished.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('finished')"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL)"
    )
    write_manifest(
        workspace,
        [make_gate("completed-process-tree", [sys.executable, "-c", parent])],
    )

    result = RepositoryGateSuite().run(workspace)
    time.sleep(1.5)

    assert result[0].status == "passed"
    assert not marker.exists()


def test_windows_termination_ignores_a_process_that_exited_before_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def taskkill(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 128, "", "")

    monkeypatch.setattr("beehaiive.agent.os.name", "nt")
    monkeypatch.setattr("beehaiive.agent.subprocess.run", taskkill)
    CodexExecModelExecutor._terminate_process(cast(Any, process))

    assert calls[0][1]["check"] is False
