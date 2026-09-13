from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from beehaiive.agent import (
    CodexExecModelExecutor,
    _gate_summary,
    redact_worker_text,
)
from beehaiive.quality_gates import MANIFEST_NAME, RepositoryGateSuite
from beehaiive.storage import MAX_AGENT_DIAGNOSTIC_LENGTH
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    GateResult,
    WorkflowService,
    WorkflowStore,
)
from main import create_app

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def _gate(
    name: str,
    argv: list[str],
    *,
    required: bool = True,
    external_only: bool = False,
    timeout_seconds: int | float = 10,
    category: str = "tests",
) -> dict[str, object]:
    return {
        "name": name,
        "argv": argv,
        "timeout_seconds": timeout_seconds,
        "category": category,
        "required": required,
        "external_only": external_only,
    }


def _write_manifest(workspace: Path, gates: list[dict[str, object]]) -> None:
    (workspace / MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "gates": gates}), encoding="utf-8"
    )


def _repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Quality Gate Tests"),
    ):
        subprocess.run(("git", *arguments), cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=path, check=True, capture_output=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path


def test_repository_gate_suite_reports_bounded_redacted_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    secret = "gate-secret-for-redaction"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    _write_manifest(
        workspace,
        [
            _gate("pass", [sys.executable, "-c", "print('ready')"]),
            _gate("failure", [sys.executable, "-c", "import sys; sys.exit(7)"]),
            _gate(
                "timeout",
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.05,
            ),
            _gate("missing", ["beehaiive-test-executable-not-installed"]),
            _gate(
                "redaction",
                [sys.executable, "-c", f"print('GITHUB_TOKEN={secret}')"],
            ),
            _gate(
                "environment",
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('GITHUB_TOKEN', 'not-present'))",
                ],
            ),
            _gate("large-output", [sys.executable, "-c", "print('x' * 100000)"]),
            _gate(
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
        "redaction",
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
    assert by_name["redaction"].stdout.strip() == "GITHUB_TOKEN=[redacted]"
    assert by_name["environment"].stdout.strip() == "not-present"
    assert by_name["large-output"].stdout.startswith("x")
    assert len(by_name["large-output"].stdout) <= 4_000
    assert by_name["codeql"].status == "external_only"
    assert by_name["codeql"].passed is False
    assert by_name["codeql"].required is False
    assert secret not in json.dumps([result.as_dict() for result in results])


def test_invalid_or_missing_manifest_runs_no_declared_command(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    marker = workspace / "must-not-run.txt"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    invalid = _gate("would-run", command)
    invalid["shell"] = True
    _write_manifest(workspace, [invalid])

    invalid_result = RepositoryGateSuite().run(workspace)
    assert len(invalid_result) == 1
    assert invalid_result[0].status == "invalid_configuration"
    assert not marker.exists()

    (workspace / MANIFEST_NAME).unlink()
    missing_result = RepositoryGateSuite().run(workspace)
    assert missing_result[0].status == "configuration_missing"
    assert not marker.exists()


def test_deeply_nested_manifest_is_invalid_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    nested = "[" * 2_000 + "0" + "]" * 2_000
    (workspace / MANIFEST_NAME).write_text(
        '{"version":1,"gates":' + nested + "}", encoding="utf-8"
    )

    result = RepositoryGateSuite().run(workspace)

    assert len(result) == 1
    assert result[0].status == "invalid_configuration"


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
    _write_manifest(workspace, [_gate("windows-no-hook", ["cmd.exe", "/c", "exit 0"])])

    result = RepositoryGateSuite().run(workspace)

    assert result[0].status == "passed"
    assert not marker.exists()


def test_huge_timeout_is_reported_as_invalid_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    _write_manifest(
        workspace,
        [
            _gate(
                "huge-timeout",
                [sys.executable, "-c", "pass"],
                timeout_seconds=10**400,
            )
        ],
    )

    result = RepositoryGateSuite().run(workspace)

    assert len(result) == 1
    assert result[0].status == "invalid_configuration"
    assert "supported range" in (result[0].error or "")


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
    _write_manifest(
        workspace,
        [
            _gate(
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
    _write_manifest(
        workspace,
        [_gate("completed-process-tree", [sys.executable, "-c", parent])],
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


def test_gate_summary_keeps_all_manifest_results_as_valid_json() -> None:
    checks = tuple(
        CheckResult(
            f"gate-{index:02d}-" + "x" * 54,
            False,
            "not included in the worker summary",
            status="configuration_missing",
            category="c" * 32,
            required=True,
            argv=("a" * 4_096,),
            exit_code=None,
            stdout="x" * 4_000,
            stderr="y" * 4_000,
            error="z" * 4_000,
        )
        for index in range(32)
    )
    gate = GateResult("model_call", False, checks, "Resolve required gates")

    encoded = json.dumps(
        {"quality_gate": _gate_summary(gate)}, sort_keys=True, separators=(",", ":")
    )
    bounded = redact_worker_text(encoded, max_length=MAX_AGENT_DIAGNOSTIC_LENGTH)
    decoded = json.loads(bounded)

    assert bounded == encoded
    assert len(encoded) <= MAX_AGENT_DIAGNOSTIC_LENGTH
    summaries = decoded["quality_gate"]["checks"]
    assert len(summaries) == 32
    assert set(summaries[0]) == {
        "name",
        "status",
        "category",
        "required",
        "exit_code",
    }


def test_model_call_api_persists_gates_and_only_blocks_required_failures(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    _write_manifest(
        repository,
        [
            _gate("local", [sys.executable, "-c", "print('ready')"]),
            _gate(
                "actionlint",
                [],
                required=False,
                external_only=True,
                category="security",
            ),
        ],
    )
    subprocess.run(("git", "add", "."), cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ("git", "commit", "-m", "add gate contract"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        None,
        check_runner=RepositoryGateSuite(),
    )
    lease = service.acquire_workspace("worker", "codex/gates", tmp_path / "worktree")
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    headers = {
        "X-API-Key": "test-key",
        "X-Workflow-Lease-Token": lease.lease_token or "",
    }

    passed = client.post(
        "/workflow/model-calls", json={"lease_id": lease.lease_id}, headers=headers
    )
    assert passed.status_code == 200
    passed_result = passed.json()
    assert passed_result["allowed"] is True
    assert passed_result["checks"][1]["status"] == "external_only"
    assert passed_result["checks"][1]["passed"] is False
    assert (
        service.store.latest_gate(lease.lease_id, "model_call").as_dict()
        == passed_result
    )

    _write_manifest(
        Path(lease.worktree_path),
        [
            _gate(
                "required-failure", [sys.executable, "-c", "import sys; sys.exit(4)"]
            ),
            _gate(
                "codeql",
                [],
                required=False,
                external_only=True,
                category="security",
            ),
        ],
    )
    blocked = client.post(
        "/workflow/model-calls", json={"lease_id": lease.lease_id}, headers=headers
    )
    assert blocked.status_code == 200
    blocked_result = blocked.json()
    assert blocked_result["allowed"] is False
    assert blocked_result["checks"][0]["status"] == "failed"
    assert blocked_result["checks"][1]["status"] == "external_only"
    assert "required-failure" in blocked_result["required_action"]
    latest = service.store.latest_gate(lease.lease_id, "model_call")
    assert latest is not None and latest.as_dict() == blocked_result
    service.discard_workspace(lease.lease_id, "test complete")
    store.close()
