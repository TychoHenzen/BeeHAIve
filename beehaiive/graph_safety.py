from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol, cast

from .contract_types import (
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
    _bounded_value,
    _redact_text,
)
from .graph import ALLOWED_TOOL_CAPABILITIES, GraphDefinition, GraphEdge, GraphNodeKind
from .graph_execution import (
    GraphExecutionError,
    GraphTransitionStatus,
    evaluate_graph_transition,
)

MAX_GRAPH_SAFETY_TEXT = 512
MAX_GRAPH_SAFETY_CHANGES = 128
MAX_GRAPH_SIMULATION_CASES = 32
MAX_GRAPH_SIMULATION_STEPS = 256
MAX_GRAPH_SAFETY_EVIDENCE_BYTES = 64 * 1024
REQUIRED_GRAPH_SAFETY_CHECKS = (
    "reachability",
    "edge_targets",
    "handler_references",
    "terminal_paths",
    "bounded_execution",
)


class GraphSafetyError(ContractError):
    """Raised when graph safety evidence cannot be trusted."""


class GraphSafetyStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class GraphSimulationStatus(StrEnum):
    COMPLETE = "complete"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GraphSafetyCheck:
    name: str
    status: GraphSafetyStatus
    reason: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        _text(self.name, "check name")
        _text(self.reason, "check reason")
        if not isinstance(cast(object, self.status), GraphSafetyStatus):
            raise GraphSafetyError("check status must be a GraphSafetyStatus value")
        object.__setattr__(self, "evidence", _freeze_mapping(self.evidence))

    @property
    def passed(self) -> bool:
        return self.status is GraphSafetyStatus.PASS

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "evidence": _thaw(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class GraphSafetyReport:
    workflow_id: str
    revision: int
    definition_hash: str
    checks: tuple[GraphSafetyCheck, ...]

    def __post_init__(self) -> None:
        _text(self.workflow_id, "workflow id")
        if type(self.revision) is not int or self.revision <= 0:
            raise GraphSafetyError("revision must be a positive integer")
        _text(self.definition_hash, "definition hash")
        _validate_hash(self.definition_hash, "definition hash")
        if not self.checks:
            raise GraphSafetyError("safety checks are required")
        names = tuple(check.name for check in self.checks)
        if len(names) != len(set(names)):
            raise GraphSafetyError("safety check names must be unique")
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def passed(self) -> bool:
        statuses = {check.name: check.status for check in self.checks}
        return all(
            statuses.get(name) is GraphSafetyStatus.PASS
            for name in REQUIRED_GRAPH_SAFETY_CHECKS
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "definition_hash": self.definition_hash,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class GraphSimulationStep:
    case_id: str
    step: int
    node_id: str
    outcome: TaskOutcome
    status: GraphTransitionStatus
    selected_edge: GraphEdge | None
    evidence: Mapping[str, object]
    question: str | None = None
    required_action: str | None = None
    artifact_refs: tuple[Mapping[str, object], ...] = ()
    validation_reason: str | None = None
    answer: str | None = None

    def __post_init__(self) -> None:
        _text(self.case_id, "case id")
        _text(self.node_id, "node id")
        if type(self.step) is not int or self.step <= 0:
            raise GraphSafetyError("simulation step must be positive")
        if not isinstance(cast(object, self.outcome), TaskOutcome):
            raise GraphSafetyError("simulation outcome must be a TaskOutcome value")
        if not isinstance(cast(object, self.status), GraphTransitionStatus):
            raise GraphSafetyError("simulation status is invalid")
        for value, label in (
            (self.question, "simulation question"),
            (self.required_action, "simulation required action"),
            (self.validation_reason, "simulation validation reason"),
            (self.answer, "simulation answer"),
        ):
            if value is not None:
                _text(value, label)
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(_freeze_mapping(artifact) for artifact in self.artifact_refs),
        )
        object.__setattr__(self, "evidence", _freeze_mapping(self.evidence))

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "step": self.step,
            "node_id": self.node_id,
            "outcome": self.outcome.value,
            "status": self.status.value,
            "selected_edge": (
                self.selected_edge.as_dict() if self.selected_edge is not None else None
            ),
            "evidence": _thaw(self.evidence),
            "question": self.question,
            "required_action": self.required_action,
            "artifact_refs": [_thaw(value) for value in self.artifact_refs],
            "validation_reason": self.validation_reason,
            "answer": self.answer,
        }


@dataclass(frozen=True, slots=True)
class GraphSimulationCase:
    case_id: str
    status: GraphSimulationStatus
    steps: tuple[GraphSimulationStep, ...]
    reason: str

    def __post_init__(self) -> None:
        _text(self.case_id, "case id")
        _text(self.reason, "simulation reason")
        if not isinstance(cast(object, self.status), GraphSimulationStatus):
            raise GraphSafetyError("simulation case status is invalid")
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def passed(self) -> bool:
        return self.status is not GraphSimulationStatus.FAILED

    @property
    def signature(self) -> tuple[tuple[str, str, str, str | None], ...]:
        return tuple(
            (
                step.node_id,
                step.outcome.value,
                step.status.value,
                step.selected_edge.target if step.selected_edge is not None else None,
            )
            for step in self.steps
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "reason": self.reason,
            "passed": self.passed,
            "steps": [step.as_dict() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class GraphSimulationReport:
    workflow_id: str
    revision: int
    definition_hash: str
    safety: GraphSafetyReport
    cases: tuple[GraphSimulationCase, ...]
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.workflow_id, "workflow id")
        if type(self.revision) is not int or self.revision <= 0:
            raise GraphSafetyError("revision must be a positive integer")
        _text(self.definition_hash, "definition hash")
        _validate_hash(self.definition_hash, "definition hash")
        if (
            self.safety.workflow_id != self.workflow_id
            or self.safety.revision != self.revision
            or self.safety.definition_hash != self.definition_hash
        ):
            raise GraphSafetyError(
                "simulation safety evidence does not match candidate"
            )
        case_ids = tuple(case.case_id for case in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise GraphSafetyError("simulation case ids must be unique")
        if len(self.cases) > MAX_GRAPH_SIMULATION_CASES:
            raise GraphSafetyError("too many simulation cases")
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(
            self,
            "errors",
            tuple(_text(error, "simulation error") for error in self.errors),
        )

    @property
    def complete(self) -> bool:
        return (
            bool(self.cases)
            and self.safety.passed
            and not self.errors
            and all(case.passed for case in self.cases)
        )

    def case_signatures(
        self,
    ) -> dict[str, tuple[tuple[str, str, str, str | None], ...]]:
        return {case.case_id: case.signature for case in self.cases}

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "definition_hash": self.definition_hash,
            "complete": self.complete,
            "safety": self.safety.as_dict(),
            "cases": [case.as_dict() for case in self.cases],
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class GraphComparison:
    candidate_id: str
    candidate_hash: str
    baseline_id: str | None
    baseline_hash: str | None
    structural_changes: tuple[str, ...]
    outcome_changes: tuple[Mapping[str, object], ...]
    complete: bool
    reason: str
    truncated: bool = False

    def __post_init__(self) -> None:
        _text(self.candidate_id, "candidate id")
        _text(self.candidate_hash, "candidate hash")
        _validate_hash(self.candidate_hash, "candidate hash")
        if self.baseline_id is not None:
            _text(self.baseline_id, "baseline id")
        if self.baseline_hash is not None:
            _text(self.baseline_hash, "baseline hash")
            _validate_hash(self.baseline_hash, "baseline hash")
        _text(self.reason, "comparison reason")
        if type(self.truncated) is not bool:
            raise GraphSafetyError("comparison truncation flag is invalid")
        if len(self.structural_changes) > MAX_GRAPH_SAFETY_CHANGES:
            raise GraphSafetyError("too many structural changes")
        object.__setattr__(
            self,
            "structural_changes",
            tuple(
                _text(value, "structural change") for value in self.structural_changes
            ),
        )
        object.__setattr__(
            self,
            "outcome_changes",
            tuple(_freeze_mapping(value) for value in self.outcome_changes),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_hash": self.candidate_hash,
            "baseline_id": self.baseline_id,
            "baseline_hash": self.baseline_hash,
            "structural_changes": list(self.structural_changes),
            "outcome_changes": [_thaw(value) for value in self.outcome_changes],
            "complete": self.complete,
            "reason": self.reason,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class GraphSafetyEvaluation:
    candidate: GraphDefinition
    safety: GraphSafetyReport
    simulation: GraphSimulationReport
    comparison: GraphComparison
    baseline_simulation: GraphSimulationReport | None = None

    @property
    def evidence_hash(self) -> str:
        payload = json.dumps(
            _canonical_json_value(self.evidence_payload()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @property
    def activatable(self) -> bool:
        return (
            self.safety.passed and self.simulation.complete and self.comparison.complete
        )

    def as_dict(self) -> dict[str, object]:
        result = self.evidence_payload()
        result.update(
            {
                "evidence_hash": self.evidence_hash,
                "activatable": self.activatable,
            }
        )
        return result

    def evidence_payload(self) -> dict[str, object]:
        return {
            "candidate": _canonical_definition_dict(self.candidate),
            "safety": self.safety.as_dict(),
            "simulation": self.simulation.as_dict(),
            "comparison": self.comparison.as_dict(),
            "baseline_simulation": (
                self.baseline_simulation.as_dict()
                if self.baseline_simulation is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class GraphReview:
    workflow_id: str
    revision: int
    definition_hash: str
    evidence_hash: str
    actor: str
    reviewed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "definition_hash": self.definition_hash,
            "evidence_hash": self.evidence_hash,
            "actor": self.actor,
            "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True, slots=True)
class GraphActivation:
    workflow_id: str
    revision: int
    definition_hash: str
    evidence_hash: str
    actor: str
    activated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "definition_hash": self.definition_hash,
            "evidence_hash": self.evidence_hash,
            "actor": self.actor,
            "activated_at": self.activated_at,
        }


class GraphSafetyStore(Protocol):
    def save_graph_definition(self, definition: GraphDefinition) -> GraphDefinition: ...

    def graph_definition_for(
        self, workflow_id: str, revision: int | None = None
    ) -> GraphDefinition | None: ...

    def graph_definitions_for(
        self, workflow_id: str
    ) -> tuple[GraphDefinition, ...]: ...

    def record_graph_safety_evidence(
        self,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def graph_safety_evidence_for(
        self, workflow_id: str, revision: int
    ) -> Mapping[str, object] | None: ...

    def record_graph_safety_review(
        self,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence_hash: str,
        actor: str,
    ) -> Mapping[str, object]: ...

    def graph_safety_review_for(
        self, workflow_id: str, revision: int
    ) -> Mapping[str, object] | None: ...

    def activate_graph_version(
        self,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence_hash: str,
        actor: str,
        *,
        allow_rollback: bool = False,
    ) -> Mapping[str, object]: ...

    def active_graph_version(self, workflow_id: str) -> Mapping[str, object] | None: ...


class GraphSafetyService:
    """Run deterministic admission checks and gate immutable activation."""

    def __init__(self, store: GraphSafetyStore | None = None) -> None:
        self.store = store

    def validate(self, definition: GraphDefinition) -> GraphSafetyReport:
        return validate_graph(definition)

    def simulate(
        self,
        definition: GraphDefinition,
        fixtures: Mapping[str, object],
    ) -> GraphSimulationReport:
        return simulate_graph(definition, fixtures)

    def compare(
        self,
        candidate: GraphDefinition,
        baseline: GraphDefinition | None,
        candidate_simulation: GraphSimulationReport,
        baseline_simulation: GraphSimulationReport | None = None,
    ) -> GraphComparison:
        return compare_graphs(
            candidate,
            baseline,
            candidate_simulation,
            baseline_simulation,
        )

    def evaluate(
        self,
        candidate: GraphDefinition,
        fixtures: Mapping[str, object],
        *,
        baseline: GraphDefinition | None = None,
        baseline_fixtures: Mapping[str, object] | None = None,
    ) -> GraphSafetyEvaluation:
        if not isinstance(cast(object, candidate), GraphDefinition):
            raise GraphSafetyError("candidate must be a GraphDefinition value")
        selected_baseline = baseline or self._previous_definition(candidate)
        if selected_baseline is not None and (
            selected_baseline.workflow_id != candidate.workflow_id
            or selected_baseline.revision >= candidate.revision
        ):
            raise GraphSafetyError(
                "baseline must be an earlier revision of the candidate"
            )
        if self.store is not None and selected_baseline is not None:
            stored_baseline = self.store.graph_definition_for(
                selected_baseline.workflow_id, selected_baseline.revision
            )
            if stored_baseline is not None:
                if graph_definition_hash(stored_baseline) != graph_definition_hash(
                    selected_baseline
                ):
                    raise GraphSafetyError(
                        "baseline does not match the persisted graph revision"
                    )
                selected_baseline = stored_baseline
        safety = self.validate(candidate)
        simulation = self.simulate(candidate, fixtures)
        baseline_simulation = (
            self.simulate(selected_baseline, baseline_fixtures or {})
            if selected_baseline is not None
            else None
        )
        comparison = self.compare(
            candidate, selected_baseline, simulation, baseline_simulation
        )
        evaluation = GraphSafetyEvaluation(
            candidate, safety, simulation, comparison, baseline_simulation
        )
        if self.store is not None:
            stored_candidate = self.store.graph_definition_for(
                candidate.workflow_id, candidate.revision
            )
            if stored_candidate is None:
                self.store.save_graph_definition(candidate)
            elif graph_definition_hash(stored_candidate) != safety.definition_hash:
                raise GraphSafetyError(
                    "candidate does not match the persisted graph revision"
                )
            if selected_baseline is not None:
                stored_baseline = self.store.graph_definition_for(
                    selected_baseline.workflow_id, selected_baseline.revision
                )
                if stored_baseline is None:
                    self.store.save_graph_definition(selected_baseline)
            self.store.record_graph_safety_evidence(
                candidate.workflow_id,
                candidate.revision,
                safety.definition_hash,
                evaluation.evidence_payload(),
            )
        return evaluation

    def review(self, evaluation: GraphSafetyEvaluation, actor: str) -> GraphReview:
        store = self._require_store()
        _operator(actor, "review actor")
        evidence = self._stored_evaluation(evaluation)
        if evidence is None:
            raise GraphSafetyError("graph safety evidence is missing or stale")
        if not evaluation.activatable:
            raise GraphSafetyError("graph safety evidence is incomplete")
        row = store.record_graph_safety_review(
            evaluation.candidate.workflow_id,
            evaluation.candidate.revision,
            evaluation.safety.definition_hash,
            evaluation.evidence_hash,
            actor,
        )
        return _review_from_row(row)

    def activate(
        self, evaluation: GraphSafetyEvaluation, actor: str
    ) -> GraphActivation:
        store = self._require_store()
        _operator(actor, "activation actor")
        evidence = self._stored_evaluation(evaluation)
        if evidence is None:
            raise GraphSafetyError("graph safety evidence is missing or stale")
        if not evaluation.activatable:
            raise GraphSafetyError("candidate failed safety, simulation, or comparison")
        active = store.active_graph_version(evaluation.candidate.workflow_id)
        active_revision = active.get("revision") if active is not None else None
        if (
            type(active_revision) is int
            and evaluation.candidate.revision < active_revision
        ):
            raise GraphSafetyError("use rollback to select an earlier graph version")
        review = store.graph_safety_review_for(
            evaluation.candidate.workflow_id, evaluation.candidate.revision
        )
        if review is None or review.get("evidence_hash") != evaluation.evidence_hash:
            raise GraphSafetyError(
                "exact candidate review is required before activation"
            )
        row = store.activate_graph_version(
            evaluation.candidate.workflow_id,
            evaluation.candidate.revision,
            evaluation.safety.definition_hash,
            evaluation.evidence_hash,
            actor,
        )
        return _activation_from_row(row)

    def rollback(self, workflow_id: str, revision: int, actor: str) -> GraphActivation:
        store = self._require_store()
        _text(workflow_id, "workflow id")
        _operator(actor, "rollback actor")
        active = store.active_graph_version(workflow_id)
        if active is None:
            raise GraphSafetyError("rollback requires an active graph version")
        active_revision = active.get("revision")
        if type(active_revision) is int and revision >= active_revision:
            raise GraphSafetyError("rollback requires an earlier graph version")
        definition = store.graph_definition_for(workflow_id, revision)
        if definition is None:
            raise GraphSafetyError("reviewed graph version was not found")
        row = store.graph_safety_evidence_for(workflow_id, revision)
        review = store.graph_safety_review_for(workflow_id, revision)
        if row is None or review is None:
            raise GraphSafetyError("rollback requires reviewed safety evidence")
        definition_hash = _required_string(row, "definition_hash")
        evidence_hash = _required_string(review, "evidence_hash")
        if review.get("definition_hash") != definition_hash:
            raise GraphSafetyError("rollback evidence does not match the graph version")
        result = store.activate_graph_version(
            workflow_id,
            revision,
            definition_hash,
            evidence_hash,
            actor,
            allow_rollback=True,
        )
        return _activation_from_row(result)

    def active(self, workflow_id: str) -> GraphActivation | None:
        store = self._require_store()
        row = store.active_graph_version(workflow_id)
        return _activation_from_row(row) if row is not None else None

    def _previous_definition(
        self, candidate: GraphDefinition
    ) -> GraphDefinition | None:
        store = self.store
        if store is None:
            return None
        definitions = store.graph_definitions_for(candidate.workflow_id)
        prior = [item for item in definitions if item.revision < candidate.revision]
        return prior[-1] if prior else None

    def _stored_evaluation(
        self, evaluation: GraphSafetyEvaluation
    ) -> Mapping[str, object] | None:
        store = self.store
        if store is None:
            return None
        row = store.graph_safety_evidence_for(
            evaluation.candidate.workflow_id, evaluation.candidate.revision
        )
        if row is None:
            return None
        if (
            row.get("definition_hash") != evaluation.safety.definition_hash
            or row.get("evidence_hash") != evaluation.evidence_hash
        ):
            return None
        return row

    def _require_store(self) -> GraphSafetyStore:
        if self.store is None:
            raise GraphSafetyError("graph safety persistence is not configured")
        return self.store


def validate_graph(definition: GraphDefinition) -> GraphSafetyReport:
    if not isinstance(cast(object, definition), GraphDefinition):
        raise GraphSafetyError("definition must be a GraphDefinition value")
    node_ids = {node.node_id for node in definition.nodes}
    outgoing: dict[str, tuple[GraphEdge, ...]] = {
        node_id: tuple(edge for edge in definition.edges if edge.source == node_id)
        for node_id in node_ids
    }
    entry = _entry_node(definition)
    reachable = _reachable(entry, outgoing)
    terminals = {node_id for node_id, edges in outgoing.items() if not edges}
    can_reach_terminal = _reverse_reachable(terminals, outgoing)
    checks = (
        _check(
            "reachability",
            reachable == node_ids,
            "every graph node is reachable from the deterministic entry node"
            if reachable == node_ids
            else "graph contains unreachable nodes",
            {
                "entry_node": entry,
                "reachable": sorted(reachable),
                "unreachable": sorted(node_ids - reachable),
            },
        ),
        _check(
            "edge_targets",
            all(edge.target in node_ids for edge in definition.edges),
            "all edge targets are declared graph nodes",
            {"edge_count": len(definition.edges)},
        ),
        _check(
            "handler_references",
            all(
                node.kind is not GraphNodeKind.TOOL
                or node.reference.reference_id in ALLOWED_TOOL_CAPABILITIES
                for node in definition.nodes
            ),
            "node handlers and tool capabilities are allowlisted",
            {
                "references": [
                    {
                        "node_id": node.node_id,
                        "kind": node.kind.value,
                        "reference": node.reference.reference_id,
                    }
                    for node in definition.nodes
                ]
            },
        ),
        _check(
            "terminal_paths",
            reachable <= can_reach_terminal,
            "every reachable path can end at a node without outgoing edges"
            if reachable <= can_reach_terminal
            else "a reachable cycle has no terminal path",
            {
                "terminal_nodes": sorted(terminals),
                "non_terminal_paths": sorted(reachable - can_reach_terminal),
            },
        ),
        _check(
            "bounded_execution",
            0 < definition.limits.max_loops <= MAX_GRAPH_SIMULATION_STEPS
            and definition.limits.max_retries >= 0,
            "graph loop and retry limits are bounded",
            {
                "max_loops": definition.limits.max_loops,
                "max_retries": definition.limits.max_retries,
                "timeout_seconds": definition.limits.timeout_seconds,
            },
        ),
    )
    return GraphSafetyReport(
        definition.workflow_id,
        definition.revision,
        graph_definition_hash(definition),
        checks,
    )


def simulate_graph(
    definition: GraphDefinition,
    fixtures: Mapping[str, object],
) -> GraphSimulationReport:
    safety = validate_graph(definition)
    normalized_fixtures = _fixture_mapping(fixtures, definition)
    if not safety.passed:
        return GraphSimulationReport(
            definition.workflow_id,
            definition.revision,
            safety.definition_hash,
            safety,
            (),
            ("safety checks failed",),
        )
    cases: list[GraphSimulationCase] = []
    entry = _entry_node(definition)
    for case_id in sorted(normalized_fixtures):
        node_id = entry
        steps: list[GraphSimulationStep] = []
        reason = "terminal path reached"
        status = GraphSimulationStatus.COMPLETE
        case_fixtures = normalized_fixtures[case_id]
        for step_number in range(1, MAX_GRAPH_SIMULATION_STEPS + 1):
            if step_number > definition.limits.max_loops:
                status = GraphSimulationStatus.FAILED
                reason = "simulation loop bound reached"
                break
            raw_result = case_fixtures.get(node_id)
            if raw_result is None:
                status = GraphSimulationStatus.FAILED
                reason = f"fixture outcome is missing for node {node_id}"
                break
            try:
                result = _fixture_result(raw_result)
                transition = evaluate_graph_transition(
                    definition,
                    execution_id=f"simulation:{definition.definition_id}:{case_id}",
                    task_id=f"fixture:{case_id}:{step_number}",
                    node_id=node_id,
                    result=result,
                    step=step_number,
                    attempt=1,
                    created_at="simulation",
                )
            except (ContractError, GraphExecutionError, TypeError, ValueError) as error:
                status = GraphSimulationStatus.FAILED
                reason = f"fixture outcome is invalid: {error}"
                break
            steps.append(
                GraphSimulationStep(
                    case_id,
                    step_number,
                    node_id,
                    transition.outcome,
                    transition.status,
                    transition.selected_edge,
                    transition.evidence,
                    transition.question,
                    transition.required_action,
                    result.artifact_refs,
                    result.validation_reason,
                    result.answer,
                )
            )
            if transition.status is GraphTransitionStatus.TERMINAL:
                break
            if transition.status is GraphTransitionStatus.PAUSED:
                status = GraphSimulationStatus.PAUSED
                reason = "human outcome pauses the deterministic simulation"
                break
            if transition.selected_edge is None:
                status = GraphSimulationStatus.FAILED
                reason = "advanced simulation transition has no target"
                break
            node_id = transition.selected_edge.target
        else:
            status = GraphSimulationStatus.FAILED
            reason = "simulation step bound reached"
        cases.append(GraphSimulationCase(case_id, status, tuple(steps), reason))
    errors = () if cases else ("at least one fixture case is required",)
    return GraphSimulationReport(
        definition.workflow_id,
        definition.revision,
        safety.definition_hash,
        safety,
        tuple(cases),
        errors,
    )


def compare_graphs(
    candidate: GraphDefinition,
    baseline: GraphDefinition | None,
    candidate_simulation: GraphSimulationReport,
    baseline_simulation: GraphSimulationReport | None = None,
) -> GraphComparison:
    if not isinstance(cast(object, candidate), GraphDefinition):
        raise GraphSafetyError("candidate must be a GraphDefinition value")
    candidate_hash = graph_definition_hash(candidate)
    if candidate_simulation.definition_hash != candidate_hash:
        raise GraphSafetyError("candidate simulation does not match the graph version")
    if baseline is None:
        return GraphComparison(
            candidate.definition_id,
            candidate_hash,
            None,
            None,
            (),
            (),
            candidate_simulation.complete,
            "no earlier graph version is available",
        )
    if (
        baseline.workflow_id != candidate.workflow_id
        or baseline.revision >= candidate.revision
    ):
        raise GraphSafetyError("baseline must be an earlier revision of the candidate")
    baseline_hash = graph_definition_hash(baseline)
    if (
        baseline_simulation is None
        or baseline_simulation.definition_hash != baseline_hash
    ):
        structural_changes, structural_truncated = graph_structural_changes(
            candidate, baseline
        )
        return GraphComparison(
            candidate.definition_id,
            candidate_hash,
            baseline.definition_id,
            baseline_hash,
            structural_changes,
            (),
            False,
            "baseline simulation evidence is missing or mismatched",
            structural_truncated,
        )
    structural_changes, structural_truncated = graph_structural_changes(
        candidate, baseline
    )
    outcome_changes, outcome_truncated = _outcome_changes(
        candidate_simulation, baseline_simulation
    )
    truncated = structural_truncated or outcome_truncated
    complete = (
        candidate_simulation.complete and baseline_simulation.complete and not truncated
    )
    reason = (
        "candidate and baseline evidence are complete"
        if complete
        else "candidate or baseline comparison evidence is incomplete"
    )
    return GraphComparison(
        candidate.definition_id,
        candidate_hash,
        baseline.definition_id,
        baseline_hash,
        structural_changes,
        outcome_changes,
        complete,
        reason,
        truncated,
    )


def graph_definition_hash(definition: GraphDefinition) -> str:
    if not isinstance(cast(object, definition), GraphDefinition):
        raise GraphSafetyError("definition must be a GraphDefinition value")
    normalized = _canonical_definition_dict(definition)
    payload = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _canonical_definition_dict(definition: GraphDefinition) -> dict[str, object]:
    normalized = definition.as_dict()
    limits = cast(dict[str, object], normalized["limits"])
    limits["timeout_seconds"] = float(cast(float, limits["timeout_seconds"]))
    return normalized


def _canonical_json_value(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _canonical_json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in cast(Sequence[object], value)]
    return value


def graph_structural_changes(
    candidate: GraphDefinition, baseline: GraphDefinition
) -> tuple[tuple[str, ...], bool]:
    changes: list[str] = []
    candidate_nodes = {node.node_id: node.as_dict() for node in candidate.nodes}
    baseline_nodes = {node.node_id: node.as_dict() for node in baseline.nodes}
    for node_id in sorted(set(candidate_nodes) | set(baseline_nodes)):
        if node_id not in baseline_nodes:
            changes.append(f"node added: {node_id}")
        elif node_id not in candidate_nodes:
            changes.append(f"node removed: {node_id}")
        elif candidate_nodes[node_id] != baseline_nodes[node_id]:
            changes.append(f"node changed: {node_id}")
    candidate_edges = {
        json.dumps(edge.as_dict(), sort_keys=True) for edge in candidate.edges
    }
    baseline_edges = {
        json.dumps(edge.as_dict(), sort_keys=True) for edge in baseline.edges
    }
    for edge in sorted(candidate_edges - baseline_edges):
        changes.append(f"edge added: {edge}")
    for edge in sorted(baseline_edges - candidate_edges):
        changes.append(f"edge removed: {edge}")
    for name, left, right in (
        (
            "model policy",
            candidate.model_policy.as_dict(),
            baseline.model_policy.as_dict(),
        ),
        ("limits", candidate.limits.as_dict(), baseline.limits.as_dict()),
        ("metadata", candidate.metadata, baseline.metadata),
    ):
        if left != right:
            changes.append(f"{name} changed")
    return tuple(changes[:MAX_GRAPH_SAFETY_CHANGES]), len(
        changes
    ) > MAX_GRAPH_SAFETY_CHANGES


def _outcome_changes(
    candidate: GraphSimulationReport, baseline: GraphSimulationReport
) -> tuple[tuple[Mapping[str, object], ...], bool]:
    candidate_cases = candidate.case_signatures()
    baseline_cases = baseline.case_signatures()
    changes: list[Mapping[str, object]] = []
    for case_id in sorted(set(candidate_cases) | set(baseline_cases)):
        candidate_signature = candidate_cases.get(case_id)
        baseline_signature = baseline_cases.get(case_id)
        if candidate_signature != baseline_signature:
            changes.append(
                {
                    "case_id": case_id,
                    "candidate": [list(item) for item in candidate_signature or ()],
                    "baseline": [list(item) for item in baseline_signature or ()],
                }
            )
    return (
        tuple(changes[:MAX_GRAPH_SAFETY_CHANGES]),
        len(changes) > MAX_GRAPH_SAFETY_CHANGES,
    )


def _reachable(entry: str, outgoing: Mapping[str, Sequence[GraphEdge]]) -> set[str]:
    seen: set[str] = set()
    pending = [entry]
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        pending.extend(edge.target for edge in outgoing.get(node_id, ()))
    return seen


def _entry_node(definition: GraphDefinition) -> str:
    targets = {edge.target for edge in definition.edges}
    roots = sorted(
        node.node_id for node in definition.nodes if node.node_id not in targets
    )
    return roots[0] if roots else definition.nodes[0].node_id


def _reverse_reachable(
    terminals: set[str], outgoing: Mapping[str, Sequence[GraphEdge]]
) -> set[str]:
    reverse: dict[str, set[str]] = {}
    for source, edges in outgoing.items():
        for edge in edges:
            reverse.setdefault(edge.target, set()).add(source)
    seen = set(terminals)
    pending = list(terminals)
    while pending:
        node_id = pending.pop()
        for parent in reverse.get(node_id, ()):
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return seen


def _check(
    name: str, passed: bool, reason: str, evidence: Mapping[str, object]
) -> GraphSafetyCheck:
    return GraphSafetyCheck(
        name,
        GraphSafetyStatus.PASS if passed else GraphSafetyStatus.FAIL,
        reason,
        evidence,
    )


def _fixture_mapping(
    fixtures: Mapping[str, object], definition: GraphDefinition
) -> dict[str, Mapping[str, object]]:
    if not isinstance(cast(object, fixtures), Mapping):
        raise GraphSafetyError("simulation fixtures must be an object")
    if len(fixtures) > MAX_GRAPH_SIMULATION_CASES:
        raise GraphSafetyError("too many simulation cases")
    node_ids = {node.node_id for node in definition.nodes}
    result: dict[str, Mapping[str, object]] = {}
    for case_id, case in fixtures.items():
        if type(case_id) is not str:
            raise GraphSafetyError("case id must be a string")
        normalized_case_id = _text(
            _redact_text(case_id, MAX_GRAPH_SAFETY_TEXT), "case id"
        )
        if not isinstance(case, Mapping):
            raise GraphSafetyError(
                "simulation fixtures must map cases to node outcomes"
            )
        if normalized_case_id in result:
            raise GraphSafetyError("simulation case ids must be unique")
        case_mapping = cast(Mapping[str, object], case)
        unknown = set(case_mapping) - node_ids
        if unknown:
            raise GraphSafetyError("simulation fixture references an unknown node")
        result[normalized_case_id] = case_mapping
    return result


def _fixture_result(value: object) -> TaskResult:
    contract = TaskContract(
        "graph.safety.simulation",
        1,
        "fixture",
        {},
        (),
        (),
        tuple(TaskOutcome),
    )
    if isinstance(value, TaskResult):
        if not isinstance(cast(object, value.outcome), TaskOutcome):
            raise GraphSafetyError("fixture outcome is invalid")
        return value.validated(contract)
    if not isinstance(value, Mapping):
        raise GraphSafetyError("fixture outcome must be a complete task result object")
    mapping = cast(Mapping[str, object], value)
    allowed = {
        "outcome",
        "evidence",
        "artifact_refs",
        "question",
        "required_action",
        "validation_reason",
        "answer",
    }
    if set(mapping) != allowed:
        raise GraphSafetyError(
            "fixture outcome must contain the complete task result shape"
        )
    raw_outcome = mapping.get("outcome")
    if not isinstance(raw_outcome, str):
        raise GraphSafetyError("fixture outcome must name an outcome")
    try:
        outcome = TaskOutcome(raw_outcome)
    except ValueError as error:
        raise GraphSafetyError("fixture outcome is unknown") from error
    evidence = mapping.get("evidence", {})
    if not isinstance(evidence, Mapping):
        raise GraphSafetyError("fixture evidence must be an object")
    raw_artifacts = mapping.get("artifact_refs", ())
    if not isinstance(raw_artifacts, Sequence) or isinstance(
        raw_artifacts, (str, bytes, bytearray)
    ):
        raise GraphSafetyError("fixture artifact references must be a list")
    artifacts: list[Mapping[str, object]] = []
    for artifact in cast(Sequence[object], raw_artifacts):
        if not isinstance(artifact, Mapping):
            raise GraphSafetyError("fixture artifact references must be objects")
        artifacts.append(cast(Mapping[str, object], artifact))
    return TaskResult(
        outcome,
        cast(Mapping[str, object], _bounded_value(cast(object, evidence))),
        tuple(artifacts),
        question=_optional_text(mapping.get("question"), "question"),
        required_action=_optional_text(
            mapping.get("required_action"), "required_action"
        ),
        validation_reason=_optional_text(
            mapping.get("validation_reason"), "validation_reason"
        ),
        answer=_optional_text(mapping.get("answer"), "answer"),
    ).validated(contract)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(cast(object, value), Mapping):
        raise GraphSafetyError("evidence must be an object")
    try:
        bounded = _bounded_safety_value(value)
    except ContractError as error:
        raise GraphSafetyError(f"evidence is not bounded: {error}") from error
    if not isinstance(bounded, dict):
        raise GraphSafetyError("evidence must be an object")
    _reject_sensitive(cast(object, bounded))
    encoded = json.dumps(bounded, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_GRAPH_SAFETY_EVIDENCE_BYTES:
        raise GraphSafetyError("evidence is too large")
    return cast(Mapping[str, object], _freeze_value(cast(object, bounded)))


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in mapping.items()}
        )
    if isinstance(value, list):
        sequence = cast(Sequence[object], value)
        return tuple(_freeze_value(item) for item in sequence)
    return value


def _bounded_safety_value(value: object, depth: int = 0) -> object:
    if depth > 12:
        raise ContractError("Safety evidence is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ContractError("Safety evidence contains a non-finite number")
        return value
    if isinstance(value, str):
        return _redact_text(value, 1_000)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if len(mapping) > 128:
            raise ContractError("Safety evidence has too many keys")
        bounded: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ContractError("Safety evidence keys must be non-empty strings")
            bounded[key[:1_000]] = _bounded_safety_value(item, depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        sequence = cast(Sequence[object], value)
        if len(sequence) > 256:
            raise ContractError("Safety evidence has too many items")
        return [_bounded_safety_value(item, depth + 1) for item in sequence]
    raise ContractError("Safety evidence contains an unsupported value")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _thaw(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(Sequence[object], value)
        return [_thaw(item) for item in sequence]
    return value


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if any(
                part in normalized
                for part in (
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "apikey",
                    "privatekey",
                )
            ):
                raise GraphSafetyError("evidence must not contain credentials")
            _reject_sensitive(item)
    elif isinstance(value, (list, tuple)):
        for item in cast(Sequence[object], value):
            _reject_sensitive(item)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GraphSafetyError(f"{label} is required")
    if len(value) > MAX_GRAPH_SAFETY_TEXT:
        raise GraphSafetyError(f"{label} exceeds {MAX_GRAPH_SAFETY_TEXT} characters")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _operator(value: object, label: str) -> str:
    actor = _text(value, label)
    if actor != "operator":
        raise GraphSafetyError("operator approval is required")
    return actor


def _required_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GraphSafetyError(f"stored {key} is invalid")
    return value


def _validate_hash(value: str, label: str) -> None:
    if len(value) != 64:
        raise GraphSafetyError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as error:
        raise GraphSafetyError(f"{label} must be a SHA-256 hex digest") from error


def _review_from_row(row: Mapping[str, object]) -> GraphReview:
    return GraphReview(
        _required_string(row, "workflow_id"),
        _required_row_revision(row),
        _required_string(row, "definition_hash"),
        _required_string(row, "evidence_hash"),
        _required_string(row, "actor"),
        _required_string(row, "reviewed_at"),
    )


def _activation_from_row(row: Mapping[str, object]) -> GraphActivation:
    return GraphActivation(
        _required_string(row, "workflow_id"),
        _required_row_revision(row),
        _required_string(row, "definition_hash"),
        _required_string(row, "evidence_hash"),
        _required_string(row, "actor"),
        _required_string(row, "activated_at"),
    )


def _required_row_revision(row: Mapping[str, object]) -> int:
    value = row.get("revision")
    if type(value) is not int or value <= 0:
        raise GraphSafetyError("stored revision is invalid")
    return value


__all__ = [
    "GraphActivation",
    "GraphComparison",
    "GraphReview",
    "GraphSafetyCheck",
    "GraphSafetyError",
    "GraphSafetyEvaluation",
    "GraphSafetyReport",
    "GraphSafetyService",
    "GraphSafetyStatus",
    "GraphSimulationCase",
    "GraphSimulationReport",
    "GraphSimulationStatus",
    "GraphSimulationStep",
    "MAX_GRAPH_SAFETY_CHANGES",
    "MAX_GRAPH_SAFETY_EVIDENCE_BYTES",
    "MAX_GRAPH_SAFETY_TEXT",
    "MAX_GRAPH_SIMULATION_CASES",
    "MAX_GRAPH_SIMULATION_STEPS",
    "REQUIRED_GRAPH_SAFETY_CHECKS",
    "compare_graphs",
    "graph_definition_hash",
    "graph_structural_changes",
    "simulate_graph",
    "validate_graph",
]
