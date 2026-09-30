from __future__ import annotations

import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import beehaiive.workflow_generator as generator_module
import beehaiive.workflows as workflow_module
from beehaiive.app import create_app
from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectColumn, ProjectSnapshot
from beehaiive.project import ProjectDataError
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
from beehaiive.workflows import (
    WorkflowStore,
    assign_layered_layout,
    validate_workflow,
)


class EmptyProvider:
    def fetch_snapshot(self) -> Any:
        raise ProjectDataError("project metadata unavailable")


def definition(skill: str = "next-ticket") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "Delivery",
        "description": "Run a skill for a backlog issue.",
        "source_prompt": "Wait for a backlog item, then run the skill.",
        "auto_reset_on_stall": True,
        "max_steps_per_pass": 20,
        "parameters": [
            {"name": "backlog", "type": "status", "const": True, "value": "Backlog"}
        ],
        "initial": "wait",
        "states": [
            {
                "id": "wait",
                "title": "Wait",
                "action": "wait_for_work",
                "max_visits": 3,
            },
            {
                "id": "run",
                "title": "Run",
                "action": "run_skill",
                "skill": skill,
                "prompt": "Work the held item.",
                "outcomes": ["done", "blocked"],
                "max_visits": 3,
            },
            {
                "id": "escalate",
                "title": "Escalate",
                "action": "escalate",
                "max_visits": 3,
            },
        ],
        "transitions": [
            {
                "from": "wait",
                "to": "run",
                "priority": 1,
                "conditions": [{"kind": "item_status_is", "value": "{backlog}"}],
            },
            {
                "from": "run",
                "to": "wait",
                "priority": 1,
                "conditions": [{"kind": "outcome_is", "value": "done"}],
            },
            {
                "from": "run",
                "to": "escalate",
                "priority": 2,
                "conditions": [{"kind": "outcome_is", "value": "blocked"}],
            },
            {
                "from": "escalate",
                "to": "wait",
                "priority": 1,
                "conditions": [{"kind": "always"}],
            },
        ],
    }


def test_validator_checks_reachability_types_and_skill_resolution(
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\ndescription: implement one ticket\n---\n", encoding="utf-8"
    )

    valid = validate_workflow(
        definition(), skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    assert valid.valid
    assert not valid.errors

    invalid = definition(skill="missing")
    invalid["states"][1]["skill"] = "{backlog}"
    invalid["states"][1]["outcomes"] = ["done", "blocked", "unused"]
    result = validate_workflow(
        invalid, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    messages = [issue.message for issue in result.errors]
    assert not result.valid
    assert any("type 'skill'" in message for message in messages)
    assert any("has no matching edge" in message for message in messages)


def test_validator_rejects_malformed_schema_and_non_text_prompt_placeholders(
    tmp_path: Path,
) -> None:
    malformed = validate_workflow(None)
    assert not malformed.valid
    assert any(issue.location == "definition" for issue in malformed.errors)

    missing = definition()
    del missing["transitions"]
    missing_result = validate_workflow(missing)
    assert any(
        issue.location == "definition.transitions" for issue in missing_result.errors
    )

    unknown = definition()
    unknown["unexpected"] = True
    unknown_result = validate_workflow(unknown)
    assert any("unknown field" in issue.message for issue in unknown_result.errors)

    wrong_type = definition()
    wrong_type["max_steps_per_pass"] = "20"
    wrong_type_result = validate_workflow(wrong_type)
    assert any(
        issue.location == "max_steps_per_pass" for issue in wrong_type_result.errors
    )

    prompt = definition()
    prompt["source_prompt"] = "Use {backlog}."
    prompt["states"][1]["prompt"] = "Use {backlog}."
    prompt_result = validate_workflow(
        prompt,
        skills_dirs=(tmp_path,),
        status_options=("Backlog",),
    )
    assert (
        sum("must have type 'text'" in issue.message for issue in prompt_result.errors)
        == 2
    )


def test_validator_normalizes_generated_always_null_and_rejects_bad_condition_values(
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\ndescription: ticket\n---\n", encoding="utf-8")

    generated = definition()
    generated["transitions"][-1]["conditions"] = [{"kind": "always", "value": None}]
    valid = validate_workflow(
        generated, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    assert valid.valid
    assert valid.definition["transitions"][-1]["conditions"] == [{"kind": "always"}]

    invalid = definition()
    invalid["transitions"][0]["conditions"] = [{"kind": "item_type_is", "value": None}]
    invalid["transitions"][1]["conditions"] = [{"kind": "outcome_is", "value": ""}]
    result = validate_workflow(
        invalid, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    messages = [issue.message for issue in result.errors]
    assert any("non-empty string" in message for message in messages)

    wrong_literal = definition()
    wrong_literal["transitions"][0]["conditions"] = [
        {"kind": "item_type_is", "value": "draft_issue"}
    ]
    result = validate_workflow(
        wrong_literal, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    assert any("issue or pull_request" in issue.message for issue in result.errors)

    non_null_always = definition()
    non_null_always["transitions"][-1]["conditions"] = [
        {"kind": "always", "value": "unexpected"}
    ]
    result = validate_workflow(
        non_null_always, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    assert any("must be omitted for always" in issue.message for issue in result.errors)


def test_skill_resolution_confines_literal_names_to_configured_roots(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    outside = tmp_path / "outside"
    (skills / "next-ticket").mkdir(parents=True)
    outside.mkdir()
    (outside / "SKILL.md").write_text("skill", encoding="utf-8")
    escaped = definition(skill="../outside")

    result = validate_workflow(
        escaped, skills_dirs=(skills,), status_options=("Backlog",)
    )

    assert any("does not resolve" in issue.message for issue in result.errors)


def test_generation_preserves_operator_parameter_rows_and_includes_contract() -> None:
    operator_parameters = [
        {"name": "backlog", "type": "status", "const": True, "value": "Backlog"}
    ]
    completed = _complete_generated_definition(
        {
            "parameters": [
                {"name": "backlog", "type": "text", "const": False, "value": None},
                {"name": "generated", "type": "text", "const": False, "value": None},
            ]
        },
        name="Delivery",
        operator_prompt="Wait for {backlog}.",
        parameters=operator_parameters,
    )

    assert completed["parameters"][:1] == operator_parameters
    assert completed["parameters"][1]["name"] == "generated"
    prompt = build_generator_prompt(
        name="Delivery",
        operator_prompt="Wait for {backlog}.",
        parameters=operator_parameters,
        skills=(),
        statuses=("Backlog",),
        labels=(),
    )
    for phrase in (
        "item_repository_is",
        "always omits value",
        "Every state is reachable",
        "Literal skills must resolve",
        "const parameter",
    ):
        assert phrase in prompt


def test_codex_runner_has_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def timed_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("codex", 300)

    monkeypatch.setattr(subprocess, "run", timed_out)

    with pytest.raises(RuntimeError, match="timed out"):
        _run_codex(["codex"], "prompt", tmp_path / "result.json")


def test_layered_layout_only_fills_missing_positions() -> None:
    value = definition()
    value["states"][0]["layout"] = {"x": 99, "y": 12}
    laid_out = assign_layered_layout(value)
    assert laid_out["states"][0]["layout"] == {"x": 99.0, "y": 12.0}
    assert all("layout" in state for state in laid_out["states"])


def test_workflow_store_versions_revisions_and_assignment_deletion(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(tmp_path / "hive.db")
    store = WorkflowStore(database)
    created = store.create_workflow("Delivery", "prompt", definition())
    assert created["latest"]["revision"] == 1
    revised = store.add_revision(1, "second", definition())
    assert revised is not None
    assert revised["latest"]["revision"] == 2
    assert len(revised["revisions"]) == 2

    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflow_assignments(workflow_id, agent_id) "
            "VALUES (1, 'agent')"
        )
    )
    assert store.delete_workflow(1) == "assigned"
    database.transaction(
        lambda connection: connection.execute("DELETE FROM workflow_assignments")
    )
    assert store.delete_workflow(1) == "deleted"
    database.close()


def test_workflow_http_api_rejects_invalid_save_and_persists_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "hive.db"
    database = SnapshotDatabase(database_path)
    service = ProjectSnapshotService(
        EmptyProvider(), database, minimum_refresh_seconds=0
    )
    config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(tmp_path,),
    )
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\ndescription: ticket\n---\n", encoding="utf-8")
    app = create_app(config, database=database, service=service)

    with TestClient(app) as client:
        invalid = client.post("/api/workflows", json={"definition": {"name": "bad"}})
        validation = client.post(
            "/api/workflows/validate", json={"definition": definition()}
        )
        created = client.post(
            "/api/workflows",
            json={"source_prompt": "first", "definition": definition()},
        )
        revised_definition = definition()
        revised_definition["description"] = "Second revision"
        revision = client.post(
            "/api/workflows/1/revisions",
            json={"source_prompt": "second", "definition": revised_definition},
        )
        listed = client.get("/api/workflows")
        detail = client.get("/api/workflows/1")

    assert invalid.status_code == 422
    assert validation.status_code == 200
    assert validation.json()["valid"]
    assert created.status_code == 200
    assert revision.status_code == 200
    assert listed.json()["workflows"][0]["name"] == "Delivery"
    assert detail.json()["latest"]["revision"] == 2
    assert [item["source_prompt"] for item in detail.json()["revisions"]] == [
        "first",
        "second",
    ]
    assert (
        detail.json()["revisions"][0]["definition"]["description"]
        != (detail.json()["revisions"][1]["definition"]["description"])
    )
    database.close()

    reopened = SnapshotDatabase(database_path)
    reopened_service = ProjectSnapshotService(
        EmptyProvider(), reopened, minimum_refresh_seconds=0
    )
    reopened_app = create_app(config, database=reopened, service=reopened_service)
    with TestClient(reopened_app) as client:
        reopened_detail = client.get("/api/workflows/1")
        reopened.transaction(
            lambda connection: connection.execute(
                "INSERT INTO workflow_assignments(workflow_id, agent_id) "
                "VALUES (1, 'agent')"
            )
        )
        assigned_delete = client.delete("/api/workflows/1")
        reopened.transaction(
            lambda connection: connection.execute("DELETE FROM workflow_assignments")
        )
        deleted = client.delete("/api/workflows/1")

    assert reopened_detail.status_code == 200
    assert reopened_detail.json()["revisions"][0]["source_prompt"] == "first"
    assert reopened_detail.json()["revisions"][1]["source_prompt"] == "second"
    assert assigned_delete.status_code == 409
    assert deleted.status_code == 200
    reopened.close()


def test_generation_retries_once_and_returns_failed_draft(tmp_path: Path) -> None:
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        EmptyProvider(), database, minimum_refresh_seconds=0
    )
    config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(tmp_path,),
    )
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\ndescription: ticket\n---\n", encoding="utf-8")
    calls: list[str] = []

    def runner(_command: list[str], prompt: str, result_path: Path) -> str:
        calls.append(prompt)
        result_path.write_text(
            json.dumps({"bad": True} if len(calls) == 1 else definition()),
            encoding="utf-8",
        )
        return ""

    generator = WorkflowGenerationService(config, service, runner=runner)
    job = generator.start("Delivery", "Wait for a backlog item.", [])
    for _ in range(100):
        result = generator.get(job["id"])
        assert result is not None
        if result["status"] != "pending":
            break
        time.sleep(0.01)

    assert result["status"] == "succeeded"
    assert result["attempts"] == 2
    assert len(calls) == 2
    assert "Repair these validation errors" in calls[1]
    database.close()


def test_generation_http_success_exposes_one_repair_and_codex_flags(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        EmptyProvider(), database, minimum_refresh_seconds=0
    )
    config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(tmp_path,),
    )
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\ndescription: ticket\n---\n", encoding="utf-8")
    calls: list[tuple[list[str], str]] = []

    def runner(command: list[str], prompt: str, result_path: Path) -> str:
        calls.append((command, prompt))
        result_path.write_text(json.dumps(definition()), encoding="utf-8")
        return ""

    generator = WorkflowGenerationService(config, service, runner=runner)
    app = create_app(
        config,
        database=database,
        service=service,
        workflow_generator=generator,
    )
    with TestClient(app) as client:
        started = client.post(
            "/api/workflows/generate",
            json={"name": "Delivery", "prompt": "Run delivery.", "parameters": []},
        )
        assert started.status_code == 200
        for _ in range(100):
            result = client.get(
                f"/api/workflows/generate/{started.json()['id']}"
            ).json()
            if result["status"] != "pending":
                break
            time.sleep(0.01)

    assert result["status"] == "succeeded"
    assert result["attempts"] == 1
    assert result["draft"]["name"] == "Delivery"
    command = calls[0][0]
    assert "--sandbox" in command
    assert "read-only" in command
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert "--output-schema" in command
    assert "-o" in command
    assert command[-1] == "-"
    database.close()


def test_generation_http_failure_after_one_repair_keeps_draft_and_errors(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        EmptyProvider(), database, minimum_refresh_seconds=0
    )
    config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(tmp_path,),
    )
    calls: list[str] = []

    def runner(_command: list[str], prompt: str, result_path: Path) -> str:
        calls.append(prompt)
        result_path.write_text(json.dumps({"bad": True}), encoding="utf-8")
        return ""

    generator = WorkflowGenerationService(config, service, runner=runner)
    app = create_app(
        config,
        database=database,
        service=service,
        workflow_generator=generator,
    )
    with TestClient(app) as client:
        started = client.post(
            "/api/workflows/generate",
            json={"name": "Broken", "prompt": "Run delivery.", "parameters": []},
        )
        assert started.status_code == 200
        for _ in range(100):
            result = client.get(
                f"/api/workflows/generate/{started.json()['id']}"
            ).json()
            if result["status"] != "pending":
                break
            time.sleep(0.01)

    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["draft"] is not None
    assert result["validation_errors"]
    assert len(calls) == 2
    assert "Repair these validation errors exactly once" in calls[1]
    database.close()


def test_generator_helpers_cover_catalog_metadata_and_codex_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    duplicate = tmp_path / "duplicate"
    (skills / "alpha").mkdir(parents=True)
    (skills / "plain").mkdir()
    (duplicate / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text(
        "---\ndescription: Alpha skill\n---\n", encoding="utf-8"
    )
    (skills / "plain" / "SKILL.md").write_text("plain", encoding="utf-8")
    (duplicate / "alpha" / "SKILL.md").write_text(
        "---\ndescription: duplicate\n---\n", encoding="utf-8"
    )
    catalog = discover_skill_catalog((tmp_path / "missing", skills, duplicate))
    assert catalog == (
        {"name": "alpha", "description": "Alpha skill"},
        {"name": "plain", "description": ""},
    )
    assert _skill_description(tmp_path / "missing" / "SKILL.md") == ""
    assert _infer_parameter_type("status_name") == "status"
    assert _infer_parameter_type("label_name") == "label"
    assert _infer_parameter_type("skill_name") == "skill"
    assert _infer_parameter_type("repository_name") == "repository"
    assert _infer_parameter_type("item_type") == "item_type"
    assert _infer_parameter_type("is_ready") == "boolean"
    assert _infer_parameter_type("retry_count") == "number"
    assert _infer_parameter_type("description") == "text"
    assert _decode_definition('```json\n{"definition": {"id": 1}}\n```') == {"id": 1}
    with pytest.raises(RuntimeError, match="valid JSON"):
        _decode_definition("not-json")
    with pytest.raises(RuntimeError, match="workflow object"):
        _decode_definition("[]")

    class SnapshotProvider:
        def fetch_snapshot(self) -> ProjectSnapshot:
            return ProjectSnapshot(
                fetched_at="2026-09-29T10:00:00+00:00",
                rate_limited_until=None,
                columns=(
                    ProjectColumn(status="Todo", items=()),
                    ProjectColumn(status="No status", items=()),
                ),
            )

    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        SnapshotProvider(), database, minimum_refresh_seconds=0
    )
    config_value = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(skills,),
    )
    generator = WorkflowGenerationService(
        config_value,
        service,
        runner=lambda _command, _prompt, _result_path: "direct output",
    )
    assert generator.get("missing") is None
    assert generator._project_metadata() == (("Todo",), ())
    assert generator._invoke("prompt") == "direct output"

    empty_generator = WorkflowGenerationService(
        config_value,
        service,
        runner=lambda _command, _prompt, _result_path: "",
    )
    with pytest.raises(RuntimeError, match="structured output"):
        empty_generator._invoke("prompt")

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return generator_module.subprocess.CompletedProcess(
            [], 0, stdout="stdout result", stderr=""
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    result_path = tmp_path / "result.json"
    assert _run_codex(["codex"], "prompt", result_path) == "stdout result"
    result_path.write_text("file result", encoding="utf-8")
    assert _run_codex(["codex"], "prompt", result_path) == "file result"

    monkeypatch.setattr(
        generator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: generator_module.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="failed"
        ),
    )
    with pytest.raises(RuntimeError, match="failed"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")
    monkeypatch.setattr(
        generator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: generator_module.subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="structured output"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")

    def raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("codex", 300)

    monkeypatch.setattr(generator_module.subprocess, "run", raise_timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        _run_codex(["codex"], "prompt", tmp_path / "missing.json")

    failing = WorkflowGenerationService(
        config_value,
        service,
        runner=lambda *_args: (_ for _ in ()).throw(RuntimeError("runner broke")),
    )
    job = failing.start("Broken", "Prompt", [])
    for _ in range(100):
        result = failing.get(job["id"])
        assert result is not None
        if result["status"] != "pending":
            break
        time.sleep(0.01)
    assert result["status"] == "failed"
    assert result["error"] == "runner broke"
    database.close()


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


def test_workflow_shape_helpers_and_edge_contracts(tmp_path: Path) -> None:
    errors: list[tuple[str, str]] = []
    malformed = definition()
    malformed["parameters"] = [
        1,
        {"name": 1, "type": 1, "const": 1, "unexpected": True},
    ]
    malformed["states"] = [
        1,
        {
            "id": 1,
            "title": 1,
            "action": 1,
            "max_visits": True,
            "skill": 1,
            "prompt": 1,
            "outcomes": [1],
            "layout": 1,
        },
    ]
    malformed["transitions"] = [
        1,
        {"from": 1, "to": 1, "priority": True, "conditions": "bad"},
    ]
    assert not workflow_module._validate_definition_shape(
        malformed, lambda location, message: errors.append((location, message))
    )
    assert errors

    condition_shape = definition()
    condition_shape["transitions"] = [
        {
            "from": "wait",
            "to": "run",
            "priority": 1,
            "conditions": [
                1,
                {"kind": 1, "value": 4},
                {"kind": "item_status_is"},
            ],
        }
    ]
    assert not workflow_module._validate_definition_shape(
        condition_shape, lambda location, message: errors.append((location, message))
    )
    invalid_layout = definition()
    invalid_layout["states"][0]["layout"] = {"x": True, "y": "bad"}
    assert not workflow_module._validate_definition_shape(
        invalid_layout, lambda location, message: errors.append((location, message))
    )

    normalized = workflow_module.normalize_workflow(
        {
            "parameters": [{"name": "status"}, 1],
            "states": [1, {"id": "wait", "layout": {"x": 1, "y": 2}}],
            "transitions": [
                1,
                {"conditions": [1, {"kind": "always", "value": None}]},
            ],
        }
    )
    assert normalized["states"][1]["layout"] == {"x": 1, "y": 2}
    assert normalized["transitions"][1]["conditions"] == [
        1,
        {"kind": "always"},
    ]

    base = definition()
    skill_file = tmp_path / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("skill", encoding="utf-8")
    cases = []
    invalid = deepcopy(base)
    invalid["schema_version"] = 2
    invalid["name"] = ""
    invalid["source_prompt"] = "x" * 8193
    invalid["max_steps_per_pass"] = 101
    cases.append(invalid)
    invalid = deepcopy(base)
    invalid["parameters"] = [
        {"name": "bad-name", "type": "wrong", "const": True, "value": None},
        {"name": "backlog", "type": "status", "const": False, "value": "x"},
    ] * 17
    cases.append(invalid)
    invalid = deepcopy(base)
    invalid["states"][0]["id"] = ""
    invalid["states"][0]["max_visits"] = 0
    invalid["states"][1]["prompt"] = ""
    invalid["states"][1]["outcomes"] = ["done", "done"]
    invalid["states"][1]["skill"] = "missing"
    invalid["states"][2]["skill"] = "unexpected"
    cases.append(invalid)
    invalid = deepcopy(base)
    invalid["transitions"] = [
        {"from": "unknown", "to": "missing", "priority": "bad", "conditions": []},
        {
            "from": "wait",
            "to": "run",
            "priority": 1,
            "conditions": [{"kind": "always"}],
        },
        {
            "from": "run",
            "to": "wait",
            "priority": 1,
            "conditions": [{"kind": "outcome_is", "value": "missing"}],
        },
        {
            "from": "escalate",
            "to": "wait",
            "priority": 1,
            "conditions": [{"kind": "item_status_is", "value": "Todo"}],
        },
    ]
    cases.append(invalid)
    for value in cases:
        result = validate_workflow(
            value, skills_dirs=(tmp_path,), status_options=("Backlog",)
        )
        assert not result.valid

    direct_errors: list[tuple[str, str]] = []
    direct_warnings: list[tuple[str, str]] = []
    for condition_kind, expected_type in (
        ("item_status_is", "status"),
        ("item_type_is", "item_type"),
        ("item_has_label", "label"),
        ("item_lacks_label", "label"),
        ("item_repository_is", "repository"),
    ):
        assert (
            workflow_module._condition_parameter_type(condition_kind) == expected_type
        )
    for parameter_type, value in (
        ("status", 1),
        ("label", 1),
        ("skill", 1),
        ("repository", 1),
        ("item_type", "bad"),
        ("text", 1),
        ("number", True),
        ("boolean", 1),
    ):
        workflow_module._validate_parameter_value(
            value,
            parameter_type,
            "value",
            lambda location, message: direct_errors.append((location, message)),
        )
    workflow_module._validate_condition_value(
        "item_type_is",
        "bad",
        "condition",
        {},
        set(),
        lambda location, message: direct_errors.append((location, message)),
        lambda location, message: direct_warnings.append((location, message)),
    )
    workflow_module._validate_condition_value(
        "item_status_is",
        "Todo",
        "condition",
        {},
        set(),
        lambda location, message: direct_errors.append((location, message)),
        lambda location, message: direct_warnings.append((location, message)),
    )
    workflow_module._validate_condition_value(
        "outcome_is",
        "{missing}",
        "condition",
        {},
        set(),
        lambda location, message: direct_errors.append((location, message)),
        lambda location, message: direct_warnings.append((location, message)),
    )
    workflow_module._check_placeholder_type(
        "literal",
        "text",
        "condition",
        {},
        lambda location, message: direct_errors.append((location, message)),
    )
    assert direct_errors
    assert direct_warnings
    assert not workflow_module._skill_exists("../escape", (tmp_path,))
    assert workflow_module._is_placeholder("{status}")
    assert not workflow_module._is_placeholder("status")
    assert workflow_module._reachable("a", {"a": ("b",), "b": ("a",)}) == {
        "a",
        "b",
    }
    assert workflow_module._condition_kind("bad") == ""
    assert workflow_module._string_list(None) == []

    database = SnapshotDatabase(":memory:")
    store = WorkflowStore(database)
    assert store.list_workflows() == []
    assert store.get_workflow(999) is None
    assert store.add_revision(999, "missing", definition()) is None
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflows(id, name, created_at) VALUES (99, 'bad', 'now')"
        )
    )
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflow_revisions(workflow_id, revision, source_prompt, "
            "definition_json, created_at) "
            "VALUES (99, 1, 'prompt', 'not-json', 'now')"
        )
    )
    with pytest.raises(RuntimeError, match="Persisted workflow"):
        store.get_workflow(99)
    database.close()


def test_workflow_validation_covers_state_and_transition_edges(tmp_path: Path) -> None:
    skill = tmp_path / "next-ticket" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("skill", encoding="utf-8")

    missing_run_fields = definition()
    missing_run_fields["states"] = [
        {
            "id": "wait",
            "title": "Wait",
            "action": "wait_for_work",
            "max_visits": 1,
        },
        {
            "id": "run",
            "title": "Run",
            "action": "run_skill",
            "max_visits": 1,
        },
    ]
    assert not workflow_module._validate_definition_shape(
        missing_run_fields, lambda _location, _message: None
    )

    too_many_states = definition()
    too_many_states["states"] = too_many_states["states"] + [
        {
            "id": f"extra-{index}",
            "title": "Extra",
            "action": "escalate",
            "max_visits": 1,
        }
        for index in range(30)
    ]
    state_cases = [too_many_states]
    duplicate_state = definition()
    duplicate_state["states"].append(deepcopy(duplicate_state["states"][0]))
    state_cases.append(duplicate_state)
    invalid_state = definition()
    invalid_state["states"][1]["action"] = "unknown"
    invalid_state["states"][1]["max_visits"] = 0
    state_cases.append(invalid_state)
    long_prompt = definition()
    long_prompt["states"][1]["prompt"] = "x" * 8193
    long_prompt["states"][1]["outcomes"] = []
    long_prompt["states"][1]["skill"] = ""
    state_cases.append(long_prompt)
    for value in state_cases:
        assert not validate_workflow(
            value, skills_dirs=(tmp_path,), status_options=("Backlog",)
        ).valid

    too_many_transitions = definition()
    too_many_transitions["transitions"] = too_many_transitions["transitions"] * 33
    assert not validate_workflow(
        too_many_transitions, skills_dirs=(tmp_path,), status_options=("Backlog",)
    ).valid

    transition_cases = []
    malformed_conditions = definition()
    malformed_conditions["transitions"] = [
        {
            "from": "wait",
            "to": "run",
            "priority": 1,
            "conditions": [
                {"kind": "unknown", "value": "x"},
                {"kind": "always", "value": "unexpected"},
                {"kind": "item_status_is"},
            ],
        }
    ]
    transition_cases.append(malformed_conditions)
    initial_action = definition()
    initial_action["initial"] = "run"
    transition_cases.append(initial_action)
    orphan = definition()
    orphan["states"].append(
        {"id": "orphan", "title": "Orphan", "action": "escalate", "max_visits": 1}
    )
    transition_cases.append(orphan)
    wait_edges = definition()
    wait_edges["transitions"][0]["conditions"] = [
        {"kind": "outcome_is", "value": "done"}
    ]
    transition_cases.append(wait_edges)
    unmatched = definition()
    unmatched["states"][1]["outcomes"] = ["never"]
    transition_cases.append(unmatched)
    escalation = definition()
    escalation["transitions"] = escalation["transitions"][:-1]
    transition_cases.append(escalation)
    undeclared_parameter = definition()
    undeclared_parameter["source_prompt"] = "Use {missing}."
    transition_cases.append(undeclared_parameter)
    empty_conditions = definition()
    empty_conditions["transitions"][0]["conditions"] = []
    transition_cases.append(empty_conditions)
    wrong_status = definition()
    wrong_status["transitions"][0]["conditions"] = [
        {"kind": "item_status_is", "value": "Not a status"}
    ]
    transition_cases.append(wrong_status)
    for value in transition_cases:
        assert not validate_workflow(
            value, skills_dirs=(tmp_path,), status_options=("Backlog",)
        ).valid
