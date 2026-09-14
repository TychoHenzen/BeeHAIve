from __future__ import annotations

import json
import sys
from pathlib import Path

from beehaiive.agent import (
    _gate_summary,
    redact_worker_text,
)
from beehaiive.quality_gates import MANIFEST_NAME, RepositoryGateSuite
from beehaiive.storage import MAX_AGENT_DIAGNOSTIC_LENGTH
from beehaiive.workflow import (
    CheckResult,
    GateResult,
)
from tests.support.quality_gates.helpers import make_gate, write_manifest

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_invalid_or_missing_manifest_runs_no_declared_command(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    marker = workspace / "must-not-run.txt"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    invalid = make_gate("would-run", command)
    invalid["shell"] = True
    write_manifest(workspace, [invalid])

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


def test_huge_timeout_is_reported_as_invalid_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    write_manifest(
        workspace,
        [
            make_gate(
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
