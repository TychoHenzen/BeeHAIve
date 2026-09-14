from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from beehaiive.workflow import (
    CheckResult,
    CommandCheck,
    Constitution,
    DeterministicCheckRunner,
    WorkflowError,
    WorkflowRole,
)
from tests.support.workflow.fixture_check import FixtureCheck as FixtureCheck
from tests.support.workflow.raising_check import RaisingCheck as RaisingCheck

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_constitution_and_check_runner_validate_evidence(tmp_path: Path) -> None:
    constitution = Constitution.load(CONSTITUTION_PATH)
    writer_rules = constitution.rules_for(WorkflowRole.WRITER)
    assert writer_rules == constitution.rules_for("writer")
    assert len(writer_rules) == 6
    with pytest.raises(TypeError):
        constitution.sections["project"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        constitution.roles[WorkflowRole.WRITER] = ()  # type: ignore[index]
    with pytest.raises(WorkflowError, match="Unknown workflow role"):
        constitution.rules_for("unknown")

    passing = FixtureCheck("pass")
    empty = FixtureCheck("empty", evidence=" ")

    class MismatchCheck:
        name = "mismatch"

        def run(self, workspace: Path) -> CheckResult:
            del workspace
            return CheckResult("other", True, "wrong name")

    results = DeterministicCheckRunner(
        [passing, empty, MismatchCheck(), RaisingCheck()]
    ).run(tmp_path)
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[2].evidence.startswith("Check failed to run")
    assert results[3].evidence.startswith("Check failed to run")

    with pytest.raises(WorkflowError, match="At least one"):
        DeterministicCheckRunner([])
    with pytest.raises(WorkflowError, match="needs a name"):
        DeterministicCheckRunner([FixtureCheck(" ")])
    with pytest.raises(WorkflowError, match="unique"):
        DeterministicCheckRunner([FixtureCheck("same"), FixtureCheck("same")])

    command_ok = CommandCheck("command", (sys.executable, "-c", "print('ok')"))
    command_fail = CommandCheck(
        "command-fail", (sys.executable, "-c", "import sys; sys.exit(3)")
    )
    assert command_ok.run(tmp_path).passed is True
    assert command_fail.run(tmp_path).passed is False
    with pytest.raises(WorkflowError, match="no command"):
        CommandCheck("empty", ()).run(tmp_path)


def test_constitution_rejects_invalid_documents(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(tmp_path / "missing.json")
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(invalid_json)

    cases = [
        ("not-object", [], "JSON object"),
        ("bad-version", {"version": 0}, "version"),
        ("bad-sections", {"version": 1, "sections": []}, "sections"),
        (
            "empty-rules",
            {"version": 1, "sections": {"project": []}},
            "non-empty",
        ),
        (
            "non-string-rule",
            {"version": 1, "sections": {"project": [1]}},
            "strings",
        ),
        (
            "empty-rule",
            {"version": 1, "sections": {"project": [""]}},
            "required",
        ),
        (
            "long-rule",
            {"version": 1, "sections": {"project": ["x" * 1_001]}},
            "at most",
        ),
        (
            "bad-roles",
            {"version": 1, "sections": {"project": ["rule"]}, "roles": []},
            "roles",
        ),
        (
            "unknown-role",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"unknown": ["project"]},
            },
            "Unknown workflow role",
        ),
        (
            "unknown-section",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"writer": ["engineering"]},
            },
            "unknown section",
        ),
    ]
    for name, document, message in cases:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(WorkflowError, match=message):
            Constitution.load(path)

    missing_role = tmp_path / "missing-role.json"
    missing_role.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"planner": ["project"]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="No constitution rules"):
        Constitution.load(missing_role).rules_for("writer")

    blank_section = tmp_path / "blank-section.json"
    blank_section.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"": ["rule"]},
                "roles": {"planner": [""]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="section names"):
        Constitution.load(blank_section)
