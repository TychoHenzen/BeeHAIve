from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import beehaiive.workflows as workflow_module
from tests.workflow_support import definition


def recorder(sink: list[tuple[str, str]]) -> Callable[[str, str], None]:
    def record(location: str, message: str) -> None:
        sink.append((location, message))

    return record


def test_shape_validation_rejects_malformed_collections() -> None:
    errors: list[tuple[str, str]] = []
    malformed = definition()
    parameter_row = {"name": 1, "type": 1, "const": 1, "unexpected": True}
    state_row = {
        "id": 1,
        "title": 1,
        "action": 1,
        "max_visits": True,
        "skill": 1,
        "prompt": 1,
        "outcomes": [1],
        "layout": 1,
    }
    transition_row = {"from": 1, "to": 1, "priority": True, "conditions": "bad"}
    malformed["parameters"] = [1, parameter_row]
    malformed["states"] = [1, state_row]
    malformed["transitions"] = [1, transition_row]
    assert not workflow_module._validate_definition_shape(
        malformed, lambda location, message: errors.append((location, message))
    )
    assert errors


def test_shape_validation_rejects_malformed_conditions_and_layout() -> None:
    errors: list[tuple[str, str]] = []
    condition_shape = definition()
    conditions = [1, {"kind": 1, "value": 4}, {"kind": "item_status_is"}]
    condition_shape["transitions"] = [
        {"from": "wait", "to": "run", "priority": 1, "conditions": conditions}
    ]
    assert not workflow_module._validate_definition_shape(
        condition_shape, lambda location, message: errors.append((location, message))
    )
    invalid_layout = definition()
    invalid_layout["states"][0]["layout"] = {"x": True, "y": "bad"}
    assert not workflow_module._validate_definition_shape(
        invalid_layout, lambda location, message: errors.append((location, message))
    )


def test_shape_validation_requires_run_skill_fields() -> None:
    missing_run_fields = definition()
    wait = {"id": "wait", "title": "Wait", "action": "wait_for_work", "max_visits": 1}
    run = {"id": "run", "title": "Run", "action": "run_skill", "max_visits": 1}
    missing_run_fields["states"] = [wait, run]
    assert not workflow_module._validate_definition_shape(
        missing_run_fields, lambda _location, _message: None
    )


def test_normalize_workflow_tolerates_malformed_rows() -> None:
    always = {"kind": "always", "value": None}
    wait_state = {"id": "wait", "layout": {"x": 1, "y": 2}}
    raw = {
        "parameters": [{"name": "status"}, 1],
        "states": [1, wait_state],
        "transitions": [1, {"conditions": [1, always]}],
    }
    normalized = workflow_module.normalize_workflow(raw)
    assert normalized["states"][1]["layout"] == {"x": 1, "y": 2}
    assert normalized["transitions"][1]["conditions"] == [
        1,
        {"kind": "always"},
    ]


def test_condition_kinds_map_to_parameter_types() -> None:
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


def test_parameter_values_are_checked_against_their_types() -> None:
    direct_errors: list[tuple[str, str]] = []
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
            value, parameter_type, "value", recorder(direct_errors)
        )
    assert direct_errors


def test_condition_values_report_errors_and_warnings() -> None:
    direct_errors: list[tuple[str, str]] = []
    direct_warnings: list[tuple[str, str]] = []
    for kind, value in (
        ("item_type_is", "bad"),
        ("item_status_is", "Todo"),
        ("outcome_is", "{missing}"),
    ):
        workflow_module._validate_condition_value(
            kind,
            value,
            "condition",
            {},
            set(),
            recorder(direct_errors),
            recorder(direct_warnings),
        )
    workflow_module._check_placeholder_type(
        "literal", "text", "condition", {}, recorder(direct_errors)
    )
    assert direct_errors
    assert direct_warnings


def test_pure_helpers_classify_paths_placeholders_and_conditions(
    tmp_path: Path,
) -> None:
    assert not workflow_module._skill_exists("../escape", (tmp_path,))
    assert workflow_module._is_placeholder("{status}")
    assert not workflow_module._is_placeholder("status")
    assert workflow_module._reachable("a", {"a": ("b",), "b": ("a",)}) == {
        "a",
        "b",
    }
    assert workflow_module._condition_kind("bad") == ""
    assert workflow_module._string_list(None) == []
