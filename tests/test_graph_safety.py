from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive import (
    GraphDefinition,
    GraphEdge,
    GraphExecutionLimits,
    GraphModelPolicy,
    GraphNode,
    GraphNodeKind,
    GraphReference,
    GraphSafetyError,
    GraphSafetyService,
    GraphSafetyStatus,
    GraphSimulationStatus,
    GraphTransitionStatus,
    ModelTier,
    Orchestrator,
    OrchestratorStore,
    compare_graphs,
    graph_definition_hash,
    simulate_graph,
    validate_graph,
)
from beehaiive.contract_types import _redact_text
from beehaiive.storage import StoreError
from main import create_app
from tests.support.api_provider import ApiProvider


def _definition(revision: int = 1, *, unreachable: bool = False) -> GraphDefinition:
    nodes = [
        GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),
        GraphNode("done", GraphNodeKind.SKILL, GraphReference("skill/done")),
    ]
    if unreachable:
        nodes.append(
            GraphNode("unused", GraphNodeKind.SKILL, GraphReference("skill/unused"))
        )
    return GraphDefinition(
        "safety-flow",
        revision,
        tuple(nodes),
        (GraphEdge("start", "done", "pass"),),
        model_policy=GraphModelPolicy(ModelTier.LUNA, (ModelTier.LUNA,)),
        limits=GraphExecutionLimits(max_loops=4, max_retries=1, timeout_seconds=30),
        metadata={"revision_note": str(revision)},
    )


def _fixture(outcome: str, **fields: object) -> dict[str, object]:
    return {
        "outcome": outcome,
        "evidence": fields.pop("evidence", {}),
        "artifact_refs": fields.pop("artifact_refs", []),
        "question": fields.pop("question", None),
        "required_action": fields.pop("required_action", None),
        "validation_reason": fields.pop("validation_reason", None),
        "answer": fields.pop("answer", None),
        **fields,
    }


def _fixtures() -> dict[str, dict[str, object]]:
    return {"happy": {"start": _fixture("pass"), "done": _fixture("pass")}}


def test_validation_reports_unreachable_nodes_and_bounded_paths() -> None:
    report = validate_graph(_definition(unreachable=True))
    assert not report.passed
    assert report.checks[0].name == "reachability"
    assert report.checks[0].status.value == "fail"
    assert report.checks[-1].status.value == "pass"


def test_validation_rejects_unbounded_cycle_without_terminal() -> None:
    looping = GraphDefinition(
        "loop-flow",
        1,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
        (GraphEdge("start", "start", "always"),),
        limits=GraphExecutionLimits(max_loops=0, max_retries=0, timeout_seconds=1),
    )
    report = validate_graph(looping)
    assert not report.passed
    assert report.checks[3].status is GraphSafetyStatus.FAIL
    assert report.checks[4].status is GraphSafetyStatus.FAIL


def test_simulation_is_deterministic_and_side_effect_free() -> None:
    definition = _definition()
    first = simulate_graph(definition, _fixtures())
    second = simulate_graph(definition, _fixtures())
    assert first == second
    assert first.complete
    assert first.cases[0].status is GraphSimulationStatus.COMPLETE
    assert [step.node_id for step in first.cases[0].steps] == ["start", "done"]
    assert first.cases[0].steps[0].status is GraphTransitionStatus.ADVANCED


def test_simulation_fails_closed_for_missing_fixtures_and_unsafe_graph() -> None:
    missing = simulate_graph(_definition(), {"missing": {"start": _fixture("pass")}})
    assert not missing.complete
    assert missing.cases[0].status is GraphSimulationStatus.FAILED
    unsafe = simulate_graph(_definition(unreachable=True), _fixtures())
    assert not unsafe.complete
    assert unsafe.errors == ("safety checks failed",)
    with pytest.raises(GraphSafetyError, match="unknown node"):
        simulate_graph(_definition(), {"bad": {"missing": _fixture("pass")}})
    with pytest.raises(GraphSafetyError, match="map cases"):
        simulate_graph(_definition(), {"bad": None})  # type: ignore[dict-item]
    with pytest.raises(GraphSafetyError, match="map cases"):
        simulate_graph(_definition(), {"bad": []})  # type: ignore[dict-item]
    with pytest.raises(GraphSafetyError, match="case id"):
        simulate_graph(_definition(), {1: _fixtures()["happy"]})  # type: ignore[dict-item]


def test_simulation_records_human_pause_and_rejects_invalid_fixture() -> None:
    paused_definition = GraphDefinition(
        "pause-flow",
        1,
        (
            GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),
            GraphNode("done", GraphNodeKind.SKILL, GraphReference("skill/done")),
        ),
        (GraphEdge("start", "done", "question"),),
    )
    paused = simulate_graph(
        paused_definition,
        {"operator": {"start": _fixture("question", question="Need input")}},
    )
    assert paused.complete
    assert paused.cases[0].status is GraphSimulationStatus.PAUSED
    invalid = simulate_graph(
        _definition(),
        {"bad": {"start": _fixture("unknown")}},
    )
    assert invalid.cases[0].status is GraphSimulationStatus.FAILED
    incomplete = simulate_graph(
        _definition(),
        {"bad": {"start": _fixture("question")}},
    )
    assert not incomplete.complete
    assert incomplete.cases[0].status is GraphSimulationStatus.FAILED
    blocked = simulate_graph(
        _definition(),
        {"bad": {"start": _fixture("blocked")}},
    )
    assert not blocked.complete
    assert blocked.cases[0].status is GraphSimulationStatus.FAILED


def test_simulation_accepts_the_full_typed_result_shape() -> None:
    result = simulate_graph(
        _definition(),
        {
            "full": {
                "start": _fixture("pass"),
                "done": _fixture("pass"),
            }
        },
    )
    assert result.complete
    short = simulate_graph(_definition(), {"short": {"start": {"outcome": "pass"}}})
    assert not short.complete
    assert short.cases[0].status is GraphSimulationStatus.FAILED


def test_comparison_reports_structure_and_representative_outcome_changes() -> None:
    baseline = _definition(1)
    candidate = _definition(2)
    baseline_simulation = simulate_graph(baseline, _fixtures())
    candidate_simulation = simulate_graph(
        candidate, {"happy": {"start": _fixture("fail"), "done": _fixture("pass")}}
    )
    comparison = compare_graphs(
        candidate, baseline, candidate_simulation, baseline_simulation
    )
    assert comparison.complete
    assert "metadata changed" in comparison.structural_changes
    assert comparison.outcome_changes[0]["case_id"] == "happy"


def test_comparison_marks_truncated_structural_evidence_incomplete() -> None:
    nodes = (
        GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),
        GraphNode("done", GraphNodeKind.SKILL, GraphReference("skill/done")),
    )
    baseline = GraphDefinition(
        "wide-flow",
        1,
        nodes,
        tuple(GraphEdge("start", "done", f"base{index}") for index in range(128)),
    )
    candidate = GraphDefinition(
        "wide-flow",
        2,
        nodes,
        tuple(GraphEdge("start", "done", f"cand{index}") for index in range(128)),
    )
    baseline_simulation = simulate_graph(
        baseline, {"case": {"start": _fixture("pass")}}
    )
    candidate_simulation = simulate_graph(
        candidate, {"case": {"start": _fixture("pass")}}
    )
    comparison = compare_graphs(
        candidate, baseline, candidate_simulation, baseline_simulation
    )
    assert comparison.truncated
    assert not comparison.complete


def test_store_requires_exact_review_before_activation_and_supports_rollback(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "safety.sqlite3")
    service = GraphSafetyService(store)
    baseline = _definition(1)
    candidate = _definition(2)
    baseline_evaluation = service.evaluate(baseline, _fixtures())
    service.review(baseline_evaluation, "operator")
    service.activate(baseline_evaluation, "operator")
    evaluation = service.evaluate(
        candidate,
        _fixtures(),
        baseline=baseline,
        baseline_fixtures=_fixtures(),
    )
    with pytest.raises(GraphSafetyError, match="review"):
        service.activate(evaluation, "operator")
    review = service.review(evaluation, "operator")
    assert review.revision == 2
    active = service.activate(evaluation, "operator")
    assert active.revision == 2
    assert store.graph_safety_evidence_for("safety-flow", 2) is not None
    store.close()
    reopened = OrchestratorStore(tmp_path / "safety.sqlite3")
    reopened_service = GraphSafetyService(reopened)
    rolled_back = reopened_service.rollback("safety-flow", 1, "operator")
    assert rolled_back.revision == 1
    assert reopened_service.active("safety-flow") == rolled_back
    with pytest.raises(GraphSafetyError, match="earlier"):
        reopened_service.rollback("safety-flow", 1, "operator")
    reopened.close()


def test_unicode_long_metadata_and_reloaded_baseline_keep_evidence_identity(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "unicode.sqlite3")
    service = GraphSafetyService(store)
    baseline = GraphDefinition(
        "unicode-flow",
        1,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
        metadata={"k" * 600: "café"},
    )
    service.evaluate(baseline, {"case": {"start": _fixture("pass")}})
    store.close()
    reopened = OrchestratorStore(tmp_path / "unicode.sqlite3")
    reopened_service = GraphSafetyService(reopened)
    candidate = GraphDefinition(
        "unicode-flow",
        2,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
        metadata={"k" * 600: "café v2"},
    )
    evaluation = reopened_service.evaluate(
        candidate,
        {"case": {"start": _fixture("pass")}},
        baseline_fixtures={"case": {"start": _fixture("pass")}},
    )
    reopened_service.review(evaluation, "operator")
    assert reopened_service.activate(evaluation, "operator").revision == 2
    reopened.close()


def test_incomplete_evidence_can_be_replaced_by_a_complete_retry(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "retry.sqlite3")
    service = GraphSafetyService(store)
    definition = _definition()
    incomplete = service.evaluate(definition, {"case": {"start": _fixture("pass")}})
    assert not incomplete.activatable
    complete = service.evaluate(definition, _fixtures())
    service.review(complete, "operator")
    assert service.activate(complete, "operator").revision == 1
    store.close()


def test_large_valid_graph_evidence_and_failed_evaluations_fail_closed(
    tmp_path: Path,
) -> None:
    nodes = tuple(
        GraphNode(
            f"node-{index:02d}", GraphNodeKind.SKILL, GraphReference("skill/node")
        )
        for index in range(64)
    )
    definition = GraphDefinition("large-flow", 1, nodes)
    report = validate_graph(definition)
    assert report.checks[0].status is GraphSafetyStatus.FAIL
    store = OrchestratorStore(tmp_path / "failed.sqlite3")
    service = GraphSafetyService(store)
    evaluation = service.evaluate(definition, {"case": {"node-00": "pass"}})
    assert store.graph_safety_evidence_for("large-flow", 1) is not None
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            "large-flow",
            1,
            graph_definition_hash(definition),
            evaluation.evidence_hash,
            "operator",
        )
    store.close()


def test_direct_store_rejects_forged_hash_and_redacts_secret_values(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "forged.sqlite3")
    definition = _definition()
    store.save_graph_definition(definition)
    with pytest.raises(StoreError, match="does not match"):
        store.record_graph_safety_evidence(
            definition.workflow_id,
            definition.revision,
            "0" * 64,
            {"detail": "Bearer TOPSECRET"},
        )
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        graph_definition_hash(definition),
        {"detail": "Bearer TOPSECRET"},
    )
    assert "TOPSECRET" not in str(evidence)
    store.close()


def test_direct_store_rejects_empty_simulation_evidence(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "empty-evidence.sqlite3")
    definition = _definition()
    store.save_graph_definition(definition)
    definition_hash = graph_definition_hash(definition)
    forged = {
        "candidate": definition.as_dict(),
        "safety": {
            "passed": True,
            "checks": [
                {"name": name, "status": "pass"}
                for name in (
                    "reachability",
                    "edge_targets",
                    "handler_references",
                    "terminal_paths",
                    "bounded_execution",
                )
            ],
        },
        "simulation": {
            "complete": True,
            "definition_hash": definition_hash,
            "cases": [{"case_id": "empty", "status": "complete", "steps": []}],
        },
        "comparison": {
            "complete": True,
            "candidate_id": definition.definition_id,
            "candidate_hash": definition_hash,
            "truncated": False,
        },
    }
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_all_pass_forgery_for_unsafe_graph(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "unsafe-forgery.sqlite3")
    definition = _definition(unreachable=True)
    store.save_graph_definition(definition)
    definition_hash = graph_definition_hash(definition)
    forged = {
        "candidate": definition.as_dict(),
        "safety": {
            "passed": True,
            "checks": [
                {"name": name, "status": "pass", "reason": "forged", "evidence": {}}
                for name in (
                    "reachability",
                    "edge_targets",
                    "handler_references",
                    "terminal_paths",
                    "bounded_execution",
                )
            ],
        },
        "simulation": {
            "workflow_id": definition.workflow_id,
            "revision": definition.revision,
            "definition_hash": definition_hash,
            "complete": True,
            "errors": [],
            "safety": {},
            "cases": [],
        },
        "comparison": {
            "complete": True,
            "candidate_id": definition.definition_id,
            "candidate_hash": definition_hash,
            "truncated": False,
        },
    }
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_mismatched_baseline_comparison(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "baseline-forgery.sqlite3")
    baseline = _definition(1)
    candidate = _definition(2)
    standalone = GraphSafetyService()
    evaluation = standalone.evaluate(
        candidate,
        _fixtures(),
        baseline=baseline,
        baseline_fixtures=_fixtures(),
    )
    store.save_graph_definition(baseline)
    store.save_graph_definition(candidate)
    forged = evaluation.evidence_payload()
    comparison = dict(forged["comparison"])  # type: ignore[arg-type]
    comparison["baseline_id"] = None
    comparison["baseline_hash"] = None
    forged["comparison"] = comparison
    evidence = store.record_graph_safety_evidence(
        candidate.workflow_id,
        candidate.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            candidate.workflow_id,
            candidate.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_non_string_human_action_fields(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "typed-fields.sqlite3")
    definition = _definition()
    evaluation = GraphSafetyService().evaluate(definition, _fixtures())
    store.save_graph_definition(definition)
    forged = evaluation.evidence_payload()
    simulation = dict(forged["simulation"])  # type: ignore[arg-type]
    cases = list(simulation["cases"])  # type: ignore[index]
    first_case = dict(cases[0])  # type: ignore[arg-type]
    steps = list(first_case["steps"])  # type: ignore[index]
    first_step = dict(steps[0])  # type: ignore[arg-type]
    first_step.update({"outcome": "question", "question": 1})
    steps[0] = first_step
    first_case["steps"] = steps
    simulation["cases"] = [first_case]
    forged["simulation"] = simulation
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_oversized_question_and_boolean_step(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "typed-bounds.sqlite3")
    definition = _definition()
    evaluation = GraphSafetyService().evaluate(definition, _fixtures())
    store.save_graph_definition(definition)
    forged = evaluation.evidence_payload()
    simulation = dict(forged["simulation"])  # type: ignore[arg-type]
    cases = list(simulation["cases"])  # type: ignore[index]
    first_case = dict(cases[0])  # type: ignore[arg-type]
    steps = list(first_case["steps"])  # type: ignore[index]
    first_step = dict(steps[0])  # type: ignore[arg-type]
    first_step.update({"outcome": "question", "question": "q" * 513, "step": True})
    steps[0] = first_step
    first_case["steps"] = steps
    simulation["cases"] = [first_case]
    forged["simulation"] = simulation
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_forged_structural_comparison(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "comparison-forgery.sqlite3")
    baseline = _definition(1)
    candidate = _definition(2)
    evaluation = GraphSafetyService().evaluate(
        candidate,
        _fixtures(),
        baseline=baseline,
        baseline_fixtures=_fixtures(),
    )
    store.save_graph_definition(baseline)
    store.save_graph_definition(candidate)
    forged = evaluation.evidence_payload()
    comparison = dict(forged["comparison"])  # type: ignore[arg-type]
    comparison.update({"structural_changes": [], "complete": True, "truncated": False})
    forged["comparison"] = comparison
    evidence = store.record_graph_safety_evidence(
        candidate.workflow_id,
        candidate.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            candidate.workflow_id,
            candidate.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_forged_comparison_reason(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "comparison-reason-forgery.sqlite3")
    definition = _definition()
    evaluation = GraphSafetyService().evaluate(definition, _fixtures())
    store.save_graph_definition(definition)
    forged = evaluation.evidence_payload()
    comparison = dict(forged["comparison"])  # type: ignore[arg-type]
    comparison["reason"] = "forged"
    forged["comparison"] = comparison
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_forged_baseline_safety(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "baseline-safety-forgery.sqlite3")
    baseline = _definition(1)
    candidate = _definition(2)
    evaluation = GraphSafetyService().evaluate(
        candidate,
        _fixtures(),
        baseline=baseline,
        baseline_fixtures=_fixtures(),
    )
    store.save_graph_definition(baseline)
    store.save_graph_definition(candidate)
    forged = evaluation.evidence_payload()
    baseline_simulation = dict(forged["baseline_simulation"])  # type: ignore[arg-type]
    baseline_simulation["safety"] = {"passed": True}
    forged["baseline_simulation"] = baseline_simulation
    evidence = store.record_graph_safety_evidence(
        candidate.workflow_id,
        candidate.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            candidate.workflow_id,
            candidate.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_unknown_evidence_envelope_fields(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "unknown-envelope.sqlite3")
    definition = _definition()
    evaluation = GraphSafetyService().evaluate(definition, _fixtures())
    store.save_graph_definition(definition)
    forged = evaluation.evidence_payload()
    forged["evil"] = "unexpected"
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_direct_store_rejects_duplicate_or_oversized_case_evidence(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "case-bounds.sqlite3")
    definition = _definition()
    evaluation = GraphSafetyService().evaluate(definition, _fixtures())
    store.save_graph_definition(definition)
    forged = evaluation.evidence_payload()
    simulation = dict(forged["simulation"])  # type: ignore[arg-type]
    simulation["cases"] = list(simulation["cases"]) * 33  # type: ignore[index]
    forged["simulation"] = simulation
    evidence = store.record_graph_safety_evidence(
        definition.workflow_id,
        definition.revision,
        evaluation.safety.definition_hash,
        forged,
    )
    with pytest.raises(StoreError, match="incomplete"):
        store.record_graph_safety_review(
            definition.workflow_id,
            definition.revision,
            evaluation.safety.definition_hash,
            str(evidence["evidence_hash"]),
            "operator",
        )
    store.close()


def test_long_graph_metadata_values_survive_evidence_persistence(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "long-metadata.sqlite3")
    definition = GraphDefinition(
        "long-flow",
        1,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
        metadata={"detail": "é" * 1001},
    )
    service = GraphSafetyService(store)
    evaluation = service.evaluate(definition, {"case": {"start": _fixture("pass")}})
    service.review(evaluation, "operator")
    assert service.activate(evaluation, "operator").revision == 1
    store.close()


def test_explicit_baseline_must_match_the_persisted_revision(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "baseline-mismatch.sqlite3")
    service = GraphSafetyService(store)
    service.evaluate(_definition(1), _fixtures())
    mismatched = GraphDefinition(
        "safety-flow",
        1,
        _definition(1).nodes,
        _definition(1).edges,
        metadata={"revision_note": "different"},
    )
    with pytest.raises(GraphSafetyError, match="baseline"):
        service.evaluate(
            _definition(2),
            _fixtures(),
            baseline=mismatched,
            baseline_fixtures=_fixtures(),
        )
    store.close()


def test_assignment_redaction_covers_access_and_client_secret_variants() -> None:
    definition = GraphDefinition(
        "redaction-flow",
        1,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
        metadata={
            "detail": (
                "access_token=TOP client_secret=HIDDEN private_key=KEY "
                '{"private_key":"JSONLEAK","credential":"JSONLEAK2"}'
            )
        },
    )
    rendered = str(definition.as_dict())
    assert "TOP" not in rendered
    assert "HIDDEN" not in rendered
    assert "KEY" not in rendered
    assert "JSONLEAK" not in rendered
    assert "JSONLEAK2" not in rendered


def test_redaction_consumes_quoted_array_and_multiword_secret_values() -> None:
    values = (
        r"{\"private_key\":\"TOPSECRET\"}",
        'credential="TOP SECRET"',
        '{"credential":["LEAK","TAIL"]}',
        '{"private_key":["A]SECRET","B"]}',
        "private_key=[A,B]",
    )
    for value in values:
        redacted = _redact_text(value)
        leaked = [
            secret
            for secret in ("TOPSECRET", "SECRET", "LEAK", "TAIL", "A]SECRET")
            if secret in redacted
        ]
        assert not leaked, f"{value!r} -> {redacted!r} leaked {leaked!r}"


def test_redacted_case_ids_keep_evidence_hash_consistent(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "case-id.sqlite3")
    service = GraphSafetyService(store)
    case_id = (
        "Bearer TOP https://user:pass@example.invalid token=SECRET "
        '{"private_key":"CASELEAK","credential":"CASELEAK2"}'
    )
    evaluation = service.evaluate(
        _definition(),
        {case_id: {"start": _fixture("pass"), "done": _fixture("pass")}},
    )
    assert evaluation.simulation.cases[0].case_id != case_id
    assert "TOP" not in evaluation.simulation.cases[0].case_id
    assert "SECRET" not in evaluation.simulation.cases[0].case_id
    assert "CASELEAK" not in evaluation.simulation.cases[0].case_id
    service.review(evaluation, "operator")
    assert service.activate(evaluation, "operator").revision == 1
    store.close()


def test_connected_64_node_graph_produces_bounded_safety_evidence() -> None:
    nodes = tuple(
        GraphNode(
            f"node-{index:02d}",
            GraphNodeKind.SKILL,
            GraphReference("skill/node"),
        )
        for index in range(64)
    )
    edges = tuple(
        GraphEdge(f"node-{index:02d}", f"node-{index + 1:02d}", "pass")
        for index in range(63)
    )
    report = validate_graph(GraphDefinition("chain-flow", 1, nodes, edges))
    assert report.passed


def test_store_rollback_mode_requires_an_active_pointer(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "direct-rollback.sqlite3")
    service = GraphSafetyService(store)
    evaluation = service.evaluate(_definition(), _fixtures())
    service.review(evaluation, "operator")
    with pytest.raises(StoreError, match="active"):
        store.activate_graph_version(
            "safety-flow",
            1,
            evaluation.safety.definition_hash,
            evaluation.evidence_hash,
            "operator",
            allow_rollback=True,
        )
    store.close()


def test_reloaded_candidate_can_be_evaluated_again(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "reload-candidate.sqlite3")
    service = GraphSafetyService(store)
    service.evaluate(_definition(), _fixtures())
    store.close()
    reopened = OrchestratorStore(tmp_path / "reload-candidate.sqlite3")
    candidate = reopened.graph_definition_for("safety-flow", 1)
    assert candidate is not None
    evaluation = GraphSafetyService(reopened).evaluate(candidate, _fixtures())
    assert evaluation.activatable
    reopened.close()


def test_activation_store_cannot_regress_without_explicit_rollback(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "monotonic.sqlite3")
    service = GraphSafetyService(store)
    first = service.evaluate(_definition(1), _fixtures())
    service.review(first, "operator")
    service.activate(first, "operator")
    second = service.evaluate(
        _definition(2),
        _fixtures(),
        baseline=_definition(1),
        baseline_fixtures=_fixtures(),
    )
    service.review(second, "operator")
    service.activate(second, "operator")
    with pytest.raises(StoreError, match="older"):
        store.activate_graph_version(
            "safety-flow",
            1,
            first.safety.definition_hash,
            first.evidence_hash,
            "operator",
        )
    store.close()


def test_persistence_rejects_tampered_or_credentialed_safety_evidence() -> None:
    store = OrchestratorStore()
    with pytest.raises(StoreError, match="credentials"):
        store.record_graph_safety_evidence(
            "flow",
            1,
            "0" * 64,
            {"candidate": {"api-key": "secret"}},
        )
    with pytest.raises(StoreError, match="not persisted"):
        store.activate_graph_version("flow", 1, "0" * 64, "1" * 64, "operator")
    store.close()


def test_service_without_store_cannot_review_or_activate() -> None:
    evaluation = GraphSafetyService().evaluate(_definition(), _fixtures())
    with pytest.raises(GraphSafetyError, match="persistence"):
        GraphSafetyService().review(evaluation, "operator")


def test_service_review_requires_the_operator_actor(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "actor.sqlite3")
    service = GraphSafetyService(store)
    evaluation = service.evaluate(_definition(), _fixtures())
    with pytest.raises(GraphSafetyError, match="operator"):
        service.review(evaluation, "writer")
    store.close()


def test_rollback_requires_an_existing_active_pointer(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "rollback.sqlite3")
    service = GraphSafetyService(store)
    evaluation = service.evaluate(_definition(), _fixtures())
    service.review(evaluation, "operator")
    with pytest.raises(GraphSafetyError, match="active"):
        service.rollback("safety-flow", 1, "operator")
    store.close()


def test_api_evaluate_review_activate_and_authentication(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "api.sqlite3")
    app = create_app(
        orchestrator=Orchestrator(store, ApiProvider()),
        api_key="test-key",
        workflow_actor="operator",
    )
    client = TestClient(app)
    payload = {
        "candidate": _definition().as_dict(),
        "fixtures": _fixtures(),
    }
    assert (
        client.post("/workflow/graphs/safety/evaluate", json=payload).status_code == 401
    )
    evaluated = client.post(
        "/workflow/graphs/safety/evaluate",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )
    assert evaluated.status_code == 200
    reviewed = client.post(
        "/workflow/graphs/safety/review",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )
    assert reviewed.status_code == 200
    activated = client.post(
        "/workflow/graphs/safety/activate",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )
    assert activated.status_code == 200
    assert activated.json()["activation"]["revision"] == 1
    active = client.get(
        "/workflow/graphs/safety-flow/active",
        headers={"X-API-Key": "test-key"},
    )
    assert active.status_code == 200
    assert active.json()["revision"] == 1
    store.close()


def test_api_active_readback_supports_slash_workflow_ids(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "slash-api.sqlite3")
    definition = GraphDefinition(
        "owner/flow",
        1,
        (GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),),
    )
    payload = {
        "candidate": definition.as_dict(),
        "fixtures": {"case": {"start": _fixture("pass")}},
    }
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    headers = {"X-API-Key": "test-key"}
    assert (
        client.post(
            "/workflow/graphs/safety/review", json=payload, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/workflow/graphs/safety/activate", json=payload, headers=headers
        ).status_code
        == 200
    )
    active = client.get("/workflow/graphs/owner/flow/active", headers=headers)
    assert active.status_code == 200
    assert active.json()["workflow_id"] == "owner/flow"
    store.close()


def test_api_rejects_revision_beyond_sqlite_range(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "revision-api.sqlite3")
    client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            api_key="test-key",
        )
    )
    candidate = _definition().as_dict()
    candidate["revision"] = 2_147_483_648
    response = client.post(
        "/workflow/graphs/safety/evaluate",
        json={"candidate": candidate, "fixtures": _fixtures()},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 409
    assert store.graph_definition_for("safety-flow", 1) is None
    store.close()


def test_graph_definition_hash_is_stable() -> None:
    assert graph_definition_hash(_definition()) == graph_definition_hash(
        GraphDefinition.from_dict(_definition().as_dict())
    )
