from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import beehaiive.workflow_generator as generator_module
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectColumn, ProjectSnapshot
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflow_generator import (
    WorkflowGenerationService,
    _complete_generated_definition,
    _decode_definition,
    _infer_parameter_type,
    _run_codex,
    _skill_description,
    build_generator_prompt,
    discover_skill_catalog,
)
from tests.support import SnapshotOnlyProvider, skills_config
from tests.workflow_support import status_parameter

CONTRACT_PHRASES = (
    "item_repository_is",
    "always omits value",
    "Every state is reachable",
    "Literal skills must resolve",
    "const parameter",
)


class TodoSnapshotProvider(SnapshotOnlyProvider):
    def fetch_snapshot(self) -> ProjectSnapshot:
        return ProjectSnapshot(
            fetched_at="2026-09-29T10:00:00+00:00",
            rate_limited_until=None,
            columns=(
                ProjectColumn(status="Todo", items=()),
                ProjectColumn(status="No status", items=()),
            ),
        )


def patch_codex_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return generator_module.subprocess.CompletedProcess(
            [], returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)


def todo_service(database: SnapshotDatabase) -> ProjectSnapshotService:
    return ProjectSnapshotService(
        TodoSnapshotProvider(), database, minimum_refresh_seconds=0
    )


def test_generation_preserves_operator_parameter_rows() -> None:
    operator_parameters = [status_parameter("backlog", "Backlog")]
    generated = [
        {"name": "backlog", "type": "text", "const": False, "value": None},
        {"name": "generated", "type": "text", "const": False, "value": None},
    ]
    completed = _complete_generated_definition(
        {"parameters": generated},
        name="Delivery",
        operator_prompt="Wait for {backlog}.",
        parameters=operator_parameters,
    )

    assert completed["parameters"][:1] == operator_parameters
    assert completed["parameters"][1]["name"] == "generated"


def test_generator_prompt_includes_the_workflow_contract() -> None:
    parameters = [status_parameter("backlog", "Backlog")]
    prompt = build_generator_prompt(
        name="Delivery",
        operator_prompt="Wait for {backlog}.",
        parameters=parameters,
        skills=(),
        statuses=("Backlog",),
        labels=(),
    )
    for phrase in CONTRACT_PHRASES:
        assert phrase in prompt


def test_codex_runner_has_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def timed_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("codex", 300)

    monkeypatch.setattr(subprocess, "run", timed_out)

    with pytest.raises(RuntimeError, match="timed out"):
        _run_codex(["codex"], "prompt", tmp_path / "result.json")


def test_skill_catalog_skips_duplicates_and_missing_roots(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    duplicate = tmp_path / "duplicate"
    (skills / "alpha").mkdir(parents=True)
    (skills / "plain").mkdir()
    (duplicate / "alpha").mkdir(parents=True)
    alpha_text = "---\ndescription: Alpha skill\n---\n"
    duplicate_text = "---\ndescription: duplicate\n---\n"
    (skills / "alpha" / "SKILL.md").write_text(alpha_text, encoding="utf-8")
    (skills / "plain" / "SKILL.md").write_text("plain", encoding="utf-8")
    (duplicate / "alpha" / "SKILL.md").write_text(duplicate_text, encoding="utf-8")
    catalog = discover_skill_catalog((tmp_path / "missing", skills, duplicate))
    assert catalog == (
        {"name": "alpha", "description": "Alpha skill"},
        {"name": "plain", "description": ""},
    )
    assert _skill_description(tmp_path / "missing" / "SKILL.md") == ""


def test_parameter_types_are_inferred_from_parameter_names() -> None:
    assert _infer_parameter_type("status_name") == "status"
    assert _infer_parameter_type("label_name") == "label"
    assert _infer_parameter_type("skill_name") == "skill"
    assert _infer_parameter_type("repository_name") == "repository"
    assert _infer_parameter_type("item_type") == "item_type"
    assert _infer_parameter_type("is_ready") == "boolean"
    assert _infer_parameter_type("retry_count") == "number"
    assert _infer_parameter_type("description") == "text"


def test_decode_definition_unwraps_fences_and_rejects_non_objects() -> None:
    assert _decode_definition('```json\n{"definition": {"id": 1}}\n```') == {"id": 1}
    with pytest.raises(RuntimeError, match="valid JSON"):
        _decode_definition("not-json")
    with pytest.raises(RuntimeError, match="workflow object"):
        _decode_definition("[]")


def test_generation_service_reads_project_metadata_and_runner_output(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(":memory:")
    generator = WorkflowGenerationService(
        skills_config(tmp_path),
        todo_service(database),
        runner=lambda _command, _prompt, _result_path: "direct output",
    )
    assert generator.get("missing") is None
    assert generator._project_metadata() == (("Todo",), ())
    assert generator._invoke("prompt") == "direct output"
    database.close()


def test_generation_service_rejects_empty_runner_output(tmp_path: Path) -> None:
    database = SnapshotDatabase(":memory:")
    empty_generator = WorkflowGenerationService(
        skills_config(tmp_path),
        todo_service(database),
        runner=lambda _command, _prompt, _result_path: "",
    )
    with pytest.raises(RuntimeError, match="structured output"):
        empty_generator._invoke("prompt")
    database.close()


def test_codex_runner_reads_stdout_then_the_result_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_codex_run(monkeypatch, stdout="stdout result")
    result_path = tmp_path / "result.json"
    assert _run_codex(["codex"], "prompt", result_path) == "stdout result"
    result_path.write_text("file result", encoding="utf-8")
    assert _run_codex(["codex"], "prompt", result_path) == "file result"


def test_codex_runner_reports_a_failed_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_codex_run(monkeypatch, returncode=1, stderr="failed")
    with pytest.raises(RuntimeError, match="failed"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")


def test_codex_runner_reports_empty_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_codex_run(monkeypatch)
    with pytest.raises(RuntimeError, match="structured output"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")


def test_codex_runner_reports_a_timeout_from_the_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("codex", 300)

    monkeypatch.setattr(generator_module.subprocess, "run", raise_timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")


def test_generator_infers_missing_prompt_parameters_and_duplicate_rows(
    tmp_path: Path,
) -> None:
    prompt = build_generator_prompt(
        name="Delivery",
        operator_prompt="Use {new_status}, {is_ready}, and {retry_count}.",
        parameters=(),
        skills=(),
        statuses=(),
        labels=(),
    )
    assert '"new_status"' in prompt
    completed = _complete_generated_definition(
        {
            "parameters": [
                1,
                {"name": "existing", "type": "text"},
                {"name": "new", "type": "text"},
            ]
        },
        name="Delivery",
        operator_prompt="Use {missing}.",
        parameters=({"name": "existing", "type": "text"},),
    )
    assert [parameter["name"] for parameter in completed["parameters"]] == [
        "existing",
        "new",
        "missing",
    ]
    front_matter = tmp_path / "front-matter.md"
    front_matter.write_text("---\nname: no description\n---\n", encoding="utf-8")
    assert _skill_description(front_matter) == ""
