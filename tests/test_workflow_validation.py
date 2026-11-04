from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from beehaiive.workflows import validate_workflow
from tests.workflow_support import definition, write_skill


def assert_rejected(value: dict[str, Any], skills_root: Path) -> None:
    result = validate_workflow(
        value, skills_dirs=(skills_root,), status_options=("Backlog",)
    )
    assert not result.valid


def test_validator_checks_reachability_types_and_skill_resolution(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, "---\ndescription: implement one ticket\n---\n")

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


def test_validator_rejects_malformed_schema() -> None:
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


def test_validator_rejects_non_text_prompt_placeholders(tmp_path: Path) -> None:
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


def test_validator_normalizes_generated_always_null(tmp_path: Path) -> None:
    write_skill(tmp_path)

    generated = definition()
    generated["transitions"][-1]["conditions"] = [{"kind": "always", "value": None}]
    valid = validate_workflow(
        generated, skills_dirs=(tmp_path,), status_options=("Backlog",)
    )
    assert valid.valid
    assert valid.definition["transitions"][-1]["conditions"] == [{"kind": "always"}]


def test_validator_rejects_bad_condition_values(tmp_path: Path) -> None:
    write_skill(tmp_path)

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


def test_validator_rejects_invalid_top_level_fields(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")
    invalid = definition()
    invalid["schema_version"] = 2
    invalid["name"] = ""
    invalid["source_prompt"] = "x" * 8193
    invalid["max_steps_per_pass"] = 101
    assert_rejected(invalid, tmp_path)


def test_validator_rejects_invalid_parameter_rows(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")
    invalid = definition()
    invalid["parameters"] = [
        {"name": "bad-name", "type": "wrong", "const": True, "value": None},
        {"name": "backlog", "type": "status", "const": False, "value": "x"},
    ] * 17
    assert_rejected(invalid, tmp_path)


def test_validator_rejects_invalid_state_fields(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")
    invalid = definition()
    invalid["states"][0]["id"] = ""
    invalid["states"][0]["max_visits"] = 0
    invalid["states"][1]["prompt"] = ""
    invalid["states"][1]["outcomes"] = ["done", "done"]
    invalid["states"][1]["skill"] = "missing"
    invalid["states"][2]["skill"] = "unexpected"
    assert_rejected(invalid, tmp_path)


def test_validator_rejects_invalid_transition_rows(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")
    invalid = definition()
    unknown: dict[str, Any] = {
        "from": "unknown",
        "to": "missing",
        "priority": "bad",
        "conditions": [],
    }
    always = {"kind": "always"}
    missing = {"kind": "outcome_is", "value": "missing"}
    todo = {"kind": "item_status_is", "value": "Todo"}
    invalid["transitions"] = [
        unknown,
        {"from": "wait", "to": "run", "priority": 1, "conditions": [always]},
        {"from": "run", "to": "wait", "priority": 1, "conditions": [missing]},
        {"from": "escalate", "to": "wait", "priority": 1, "conditions": [todo]},
    ]
    assert_rejected(invalid, tmp_path)


def test_validator_rejects_invalid_state_collections(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")

    too_many_states = definition()
    extra = {"title": "Extra", "action": "escalate", "max_visits": 1}
    too_many_states["states"] = too_many_states["states"] + [
        {"id": f"extra-{index}"} | extra for index in range(30)
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


def test_validator_rejects_too_many_transitions(tmp_path: Path) -> None:
    write_skill(tmp_path, "skill")
    too_many_transitions = definition()
    too_many_transitions["transitions"] = too_many_transitions["transitions"] * 33
    assert not validate_workflow(
        too_many_transitions, skills_dirs=(tmp_path,), status_options=("Backlog",)
    ).valid


def test_validator_rejects_malformed_conditions_and_unreachable_states(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, "skill")
    transition_cases: list[dict[str, Any]] = []
    malformed_conditions = definition()
    conditions = [
        {"kind": "unknown", "value": "x"},
        {"kind": "always", "value": "unexpected"},
        {"kind": "item_status_is"},
    ]
    malformed_conditions["transitions"] = [
        {"from": "wait", "to": "run", "priority": 1, "conditions": conditions}
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
    for value in transition_cases:
        assert not validate_workflow(
            value, skills_dirs=(tmp_path,), status_options=("Backlog",)
        ).valid


def test_validator_rejects_unmatched_edges_and_undeclared_parameters(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, "skill")
    transition_cases: list[dict[str, Any]] = []
    wait_edges = definition()
    done = {"kind": "outcome_is", "value": "done"}
    wait_edges["transitions"][0]["conditions"] = [done]
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
    not_a_status = {"kind": "item_status_is", "value": "Not a status"}
    wrong_status["transitions"][0]["conditions"] = [not_a_status]
    transition_cases.append(wrong_status)
    for value in transition_cases:
        assert not validate_workflow(
            value, skills_dirs=(tmp_path,), status_options=("Backlog",)
        ).valid
