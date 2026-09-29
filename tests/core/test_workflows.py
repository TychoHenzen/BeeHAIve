from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beehaiive.core.app import create_app
from beehaiive.core.config import CoreConfig
from beehaiive.core.database import SnapshotDatabase
from beehaiive.core.project import ProjectDataError
from beehaiive.core.snapshot import ProjectSnapshotService
from beehaiive.core.workflow_generator import (
    WorkflowGenerationService,
    _complete_generated_definition,
    build_generator_prompt,
)
from beehaiive.core.workflows import (
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
        "source_prompt": "Wait for {backlog}, then run the skill.",
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
    app = create_app(config, database=database, service=service)

    with TestClient(app) as client:
        invalid = client.post("/api/workflows", json={"definition": {"name": "bad"}})
        created = client.post(
            "/api/workflows",
            json={"source_prompt": "prompt", "definition": definition()},
        )
        revision = client.post(
            "/api/workflows/1/revisions",
            json={"source_prompt": "second", "definition": definition()},
        )
        listed = client.get("/api/workflows")
        detail = client.get("/api/workflows/1")

    assert invalid.status_code == 422
    assert created.status_code == 200
    assert revision.status_code == 200
    assert listed.json()["workflows"][0]["name"] == "Delivery"
    assert detail.json()["latest"]["revision"] == 2
    database.close()


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
    job = generator.start("Delivery", "Wait for {backlog}.", [])
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
