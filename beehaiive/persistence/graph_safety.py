from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, cast

from beehaiive.contract_types import (
    MAX_CONTRACT_TEXT,
    MAX_RESULT_TEXT,
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
    _redact_text,
)
from beehaiive.graph import MAX_GRAPH_REVISION, GraphDefinition
from beehaiive.graph_execution import GraphTransitionStatus, evaluate_graph_transition
from beehaiive.graph_safety import (
    MAX_GRAPH_SAFETY_CHANGES,
    MAX_GRAPH_SAFETY_EVIDENCE_BYTES,
    MAX_GRAPH_SAFETY_TEXT,
    MAX_GRAPH_SIMULATION_CASES,
    REQUIRED_GRAPH_SAFETY_CHECKS,
    graph_definition_hash,
    graph_structural_changes,
    validate_graph,
)

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class GraphSafetyMixin:
    def record_graph_safety_evidence(
        self: Any,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        _required_text(workflow_id, "workflow id")
        _required_revision(revision)
        _required_hash(definition_hash, "definition hash")
        payload = _payload(evidence)
        evidence_hash = sha256(payload.encode("utf-8")).hexdigest()
        created_at = _now()
        with self._transaction() as connection:
            expected_hash = _definition_hash_for(connection, workflow_id, revision)
            if expected_hash != definition_hash:
                raise StoreError("Definition hash does not match the persisted graph")
            row = connection.execute(
                """
                SELECT definition_hash, evidence_hash, evidence_json, created_at
                FROM graph_safety_evidence
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if row is not None:
                if (
                    row["definition_hash"] != definition_hash
                    or row["evidence_hash"] != evidence_hash
                    or row["evidence_json"] != payload
                ):
                    if _evidence_is_activatable(
                        connection,
                        workflow_id,
                        revision,
                        row["evidence_json"],
                        str(row["definition_hash"]),
                    ):
                        raise StoreError("Graph safety evidence is immutable")
                    connection.execute(
                        """
                        UPDATE graph_safety_evidence
                        SET definition_hash = ?, evidence_hash = ?,
                            evidence_json = ?, created_at = ?
                        WHERE workflow_id = ? AND revision = ?
                        """,
                        (
                            definition_hash,
                            evidence_hash,
                            payload,
                            created_at,
                            workflow_id,
                            revision,
                        ),
                    )
                    return {
                        "workflow_id": workflow_id,
                        "revision": revision,
                        "definition_hash": definition_hash,
                        "evidence_hash": evidence_hash,
                        "evidence": json.loads(payload),
                        "created_at": created_at,
                    }
                return _evidence_row(row, workflow_id, revision)
            connection.execute(
                """
                INSERT INTO graph_safety_evidence(
                    workflow_id, revision, definition_hash, evidence_hash,
                    evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    revision,
                    definition_hash,
                    evidence_hash,
                    payload,
                    created_at,
                ),
            )
        return {
            "workflow_id": workflow_id,
            "revision": revision,
            "definition_hash": definition_hash,
            "evidence_hash": evidence_hash,
            "evidence": json.loads(payload),
            "created_at": created_at,
        }

    def graph_safety_evidence_for(
        self: Any, workflow_id: str, revision: int
    ) -> Mapping[str, object] | None:
        _required_text(workflow_id, "workflow id")
        _required_revision(revision)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT definition_hash, evidence_hash, evidence_json, created_at
                FROM graph_safety_evidence
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if (
                row is not None
                and _definition_hash_for(self._connection, workflow_id, revision)
                != row["definition_hash"]
            ):
                raise StoreError("Stored graph safety evidence is stale")
        return _evidence_row(row, workflow_id, revision) if row is not None else None

    def record_graph_safety_review(
        self: Any,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence_hash: str,
        actor: str,
    ) -> Mapping[str, object]:
        _required_text(workflow_id, "workflow id")
        _required_revision(revision)
        _required_hash(definition_hash, "definition hash")
        _required_hash(evidence_hash, "evidence hash")
        _required_operator(actor, "review actor")
        reviewed_at = _now()
        with self._transaction() as connection:
            if (
                _definition_hash_for(connection, workflow_id, revision)
                != definition_hash
            ):
                raise StoreError("Definition hash does not match the persisted graph")
            evidence = connection.execute(
                """
                SELECT definition_hash, evidence_hash, evidence_json
                FROM graph_safety_evidence
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if evidence is None or evidence["definition_hash"] != definition_hash:
                raise StoreError("Graph safety evidence is missing or stale")
            if evidence["evidence_hash"] != evidence_hash:
                raise StoreError("Graph safety evidence hash does not match")
            payload_row = connection.execute(
                """
                SELECT evidence_json
                FROM graph_safety_evidence
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if payload_row is None or not _evidence_is_activatable(
                connection,
                workflow_id,
                revision,
                payload_row["evidence_json"],
                definition_hash,
            ):
                raise StoreError("Graph safety evidence is incomplete")
            row = connection.execute(
                """
                SELECT workflow_id, revision, definition_hash, evidence_hash,
                       actor, reviewed_at
                FROM graph_safety_reviews
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if row is not None:
                if (
                    row["definition_hash"] != definition_hash
                    or row["evidence_hash"] != evidence_hash
                    or row["actor"] != actor
                ):
                    raise StoreError("Graph safety review is immutable")
                return dict(row)
            connection.execute(
                """
                INSERT INTO graph_safety_reviews(
                    workflow_id, revision, definition_hash, evidence_hash,
                    actor, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    revision,
                    definition_hash,
                    evidence_hash,
                    actor,
                    reviewed_at,
                ),
            )
        return {
            "workflow_id": workflow_id,
            "revision": revision,
            "definition_hash": definition_hash,
            "evidence_hash": evidence_hash,
            "actor": actor,
            "reviewed_at": reviewed_at,
        }

    def graph_safety_review_for(
        self: Any, workflow_id: str, revision: int
    ) -> Mapping[str, object] | None:
        _required_text(workflow_id, "workflow id")
        _required_revision(revision)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT workflow_id, revision, definition_hash, evidence_hash,
                       actor, reviewed_at
                FROM graph_safety_reviews
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
        return dict(row) if row is not None else None

    def activate_graph_version(
        self: Any,
        workflow_id: str,
        revision: int,
        definition_hash: str,
        evidence_hash: str,
        actor: str,
        *,
        allow_rollback: bool = False,
    ) -> Mapping[str, object]:
        _required_text(workflow_id, "workflow id")
        _required_revision(revision)
        _required_hash(definition_hash, "definition hash")
        _required_hash(evidence_hash, "evidence hash")
        _required_operator(actor, "activation actor")
        activated_at = _now()
        with self._transaction() as connection:
            expected_hash = _definition_hash_for(connection, workflow_id, revision)
            if expected_hash != definition_hash:
                raise StoreError("Definition hash does not match the persisted graph")
            evidence = connection.execute(
                """
                SELECT definition_hash, evidence_hash, evidence_json
                FROM graph_safety_evidence
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            review = connection.execute(
                """
                SELECT definition_hash, evidence_hash
                FROM graph_safety_reviews
                WHERE workflow_id = ? AND revision = ?
                LIMIT 1
                """,
                (workflow_id, revision),
            ).fetchone()
            if (
                evidence is None
                or review is None
                or evidence["definition_hash"] != definition_hash
                or evidence["evidence_hash"] != evidence_hash
                or review["definition_hash"] != definition_hash
                or review["evidence_hash"] != evidence_hash
                or not _evidence_is_activatable(
                    connection,
                    workflow_id,
                    revision,
                    evidence["evidence_json"],
                    definition_hash,
                )
            ):
                raise StoreError("Exact reviewed graph evidence is required")
            existing = connection.execute(
                """
                SELECT workflow_id, revision, definition_hash, evidence_hash,
                       actor, activated_at
                FROM graph_active_versions
                WHERE workflow_id = ?
                LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
            if allow_rollback and existing is None:
                raise StoreError("rollback requires an active graph version")
            if (
                allow_rollback
                and existing is not None
                and type(existing["revision"]) is int
                and revision >= existing["revision"]
            ):
                raise StoreError("rollback requires an earlier graph version")
            if existing is not None and (
                existing["revision"] == revision
                and existing["definition_hash"] == definition_hash
                and existing["evidence_hash"] == evidence_hash
            ):
                return dict(existing)
            if (
                existing is not None
                and type(existing["revision"]) is int
                and existing["revision"] > revision
                and not allow_rollback
            ):
                raise StoreError(
                    "An older graph version cannot replace the active version"
                )
            connection.execute(
                """
                INSERT INTO graph_active_versions(
                    workflow_id, revision, definition_hash, evidence_hash,
                    actor, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    revision = excluded.revision,
                    definition_hash = excluded.definition_hash,
                    evidence_hash = excluded.evidence_hash,
                    actor = excluded.actor,
                    activated_at = excluded.activated_at
                """,
                (
                    workflow_id,
                    revision,
                    definition_hash,
                    evidence_hash,
                    actor,
                    activated_at,
                ),
            )
        return {
            "workflow_id": workflow_id,
            "revision": revision,
            "definition_hash": definition_hash,
            "evidence_hash": evidence_hash,
            "actor": actor,
            "activated_at": activated_at,
        }

    def active_graph_version(
        self: Any, workflow_id: str
    ) -> Mapping[str, object] | None:
        _required_text(workflow_id, "workflow id")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT workflow_id, revision, definition_hash, evidence_hash,
                       actor, activated_at
                FROM graph_active_versions
                WHERE workflow_id = ?
                LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        return dict(row) if row is not None else None


def _payload(value: Mapping[str, object]) -> str:
    if not isinstance(cast(object, value), Mapping):
        raise StoreError("Graph safety evidence must be an object")
    try:
        bounded = _bounded_safety_value(value)
    except ContractError as error:
        raise StoreError(f"Graph safety evidence is not bounded: {error}") from error
    if not isinstance(bounded, dict):
        raise StoreError("Graph safety evidence must be an object")
    _reject_sensitive(cast(object, bounded))
    payload = json.dumps(
        bounded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if len(payload.encode("utf-8")) > MAX_GRAPH_SAFETY_EVIDENCE_BYTES:
        raise StoreError("Graph safety evidence is too large")
    return payload


def _bounded_safety_value(value: object, depth: int = 0) -> object:
    if depth > 12:
        raise ContractError("Graph safety evidence is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("Graph safety evidence contains a non-finite number")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        if len(value) > MAX_RESULT_TEXT:
            raise ContractError("Graph safety evidence text is too long")
        return _redact_text(value, MAX_RESULT_TEXT)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if len(mapping) > 128:
            raise ContractError("Graph safety evidence has too many keys")
        bounded: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ContractError(
                    "Graph safety evidence keys must be non-empty strings"
                )
            bounded[key[:MAX_CONTRACT_TEXT]] = _bounded_safety_value(item, depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        sequence = cast(Sequence[object], value)
        if len(sequence) > 256:
            raise ContractError("Graph safety evidence has too many items")
        return [_bounded_safety_value(item, depth + 1) for item in sequence]
    raise ContractError("Graph safety evidence contains an unsupported value")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if any(
                marker in normalized
                for marker in (
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "apikey",
                    "privatekey",
                )
            ):
                raise StoreError("Graph safety evidence must not contain credentials")
            _reject_sensitive(item)
    elif isinstance(value, (list, tuple)):
        for item in cast(Sequence[object], value):
            _reject_sensitive(item)


def _evidence_row(row: Any, workflow_id: str, revision: int) -> Mapping[str, object]:
    try:
        evidence = json.loads(str(row["evidence_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StoreError("Stored graph safety evidence is invalid") from error
    if not isinstance(evidence, Mapping):
        raise StoreError("Stored graph safety evidence is invalid")
    evidence_mapping = cast(Mapping[str, object], evidence)
    payload = _payload(evidence_mapping)
    definition_hash = str(row["definition_hash"])
    _required_hash(definition_hash, "definition hash")
    evidence_hash = str(row["evidence_hash"])
    _required_hash(evidence_hash, "evidence hash")
    if sha256(payload.encode("utf-8")).hexdigest() != evidence_hash:
        raise StoreError("Stored graph safety evidence is invalid")
    return {
        "workflow_id": workflow_id,
        "revision": revision,
        "definition_hash": definition_hash,
        "evidence_hash": evidence_hash,
        "evidence": dict(evidence_mapping),
        "created_at": str(row["created_at"]),
    }


def _definition_hash_for(connection: Any, workflow_id: str, revision: int) -> str:
    row = connection.execute(
        """
        SELECT definition_json
        FROM graph_definitions
        WHERE workflow_id = ? AND revision = ?
        LIMIT 1
        """,
        (workflow_id, revision),
    ).fetchone()
    if row is None:
        raise StoreError("Graph definition is not persisted")
    try:
        definition = GraphDefinition.from_dict(json.loads(str(row["definition_json"])))
        return graph_definition_hash(definition)
    except (ContractError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise StoreError("Persisted graph definition is invalid") from error


def _evidence_is_activatable(
    connection: Any,
    workflow_id: str,
    revision: int,
    value: object,
    definition_hash: str,
) -> bool:
    try:
        if not isinstance(value, (str, bytes, bytearray)):
            return False
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            return False
        decoded_mapping = cast(Mapping[str, object], decoded)
        if not _only_keys(
            decoded_mapping,
            {"candidate", "safety", "simulation", "comparison", "baseline_simulation"},
            {"candidate", "safety", "simulation", "comparison", "baseline_simulation"},
        ):
            return False
        candidate = decoded_mapping.get("candidate")
        if not isinstance(candidate, Mapping):
            return False
        if not _definition_shape_valid(cast(Mapping[str, object], candidate)):
            return False
        definition = GraphDefinition.from_dict(cast(Mapping[str, object], candidate))
        if graph_definition_hash(definition) != definition_hash:
            return False
        safety = decoded_mapping.get("safety")
        simulation = decoded_mapping.get("simulation")
        comparison = decoded_mapping.get("comparison")
        safety_mapping = (
            cast(Mapping[str, object], safety) if isinstance(safety, Mapping) else None
        )
        simulation_mapping = (
            cast(Mapping[str, object], simulation)
            if isinstance(simulation, Mapping)
            else None
        )
        comparison_mapping = (
            cast(Mapping[str, object], comparison)
            if isinstance(comparison, Mapping)
            else None
        )
        if simulation_mapping is None:
            return False
        if comparison_mapping is None:
            return False
        if not _simulation_shape_valid(simulation_mapping):
            return False
        if not _comparison_shape_valid(comparison_mapping):
            return False
        if safety_mapping is None or not _safety_shape_valid(safety_mapping):
            return False
        checks = safety_mapping.get("checks")
        check_values = (
            cast(Sequence[object], checks)
            if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes))
            else ()
        )
        check_statuses = {
            cast(Mapping[str, object], check).get("name"): cast(
                Mapping[str, object], check
            ).get("status")
            for check in check_values
            if isinstance(check, Mapping)
        }
        candidate_mapping = cast(Mapping[str, object], candidate)
        candidate_id = candidate_mapping.get("workflow_id")
        candidate_revision = candidate_mapping.get("revision")
        expected_safety = validate_graph(definition).as_dict()
        if safety_mapping != expected_safety:
            return False
        baseline_ok, baseline = _baseline_definition(
            connection, workflow_id, revision, comparison_mapping
        )
        if not baseline_ok:
            return False
        baseline_simulation_value = decoded_mapping.get("baseline_simulation")
        if baseline is None:
            if baseline_simulation_value is not None:
                return False
            expected_structural: tuple[str, ...] = ()
            expected_structural_truncated = False
            expected_outcomes: tuple[Mapping[str, object], ...] = ()
            expected_outcome_truncated = False
            baseline_complete = True
        else:
            if not isinstance(baseline_simulation_value, Mapping):
                return False
            baseline_simulation_mapping = cast(
                Mapping[str, object], baseline_simulation_value
            )
            if not _simulation_shape_valid(baseline_simulation_mapping):
                return False
            if (
                baseline_simulation_mapping.get("safety")
                != validate_graph(baseline).as_dict()
                or baseline_simulation_mapping.get("errors") != []
                or baseline_simulation_mapping.get("complete") is not True
            ):
                return False
            if not _simulation_is_valid(baseline, baseline_simulation_mapping):
                return False
            expected_structural, expected_structural_truncated = (
                graph_structural_changes(definition, baseline)
            )
            expected_outcomes, expected_outcome_truncated = _simulation_outcome_changes(
                simulation_mapping, baseline_simulation_mapping
            )
            baseline_complete = baseline_simulation_mapping.get("complete") is True
        structural_changes = comparison_mapping.get("structural_changes")
        outcome_changes = comparison_mapping.get("outcome_changes")
        if not isinstance(structural_changes, Sequence) or isinstance(
            structural_changes, (str, bytes, bytearray)
        ):
            return False
        if not isinstance(outcome_changes, Sequence) or isinstance(
            outcome_changes, (str, bytes, bytearray)
        ):
            return False
        structural_values = cast(Sequence[object], structural_changes)
        outcome_values = cast(Sequence[object], outcome_changes)
        if not all(isinstance(item, str) for item in structural_values):
            return False
        if not all(isinstance(item, Mapping) for item in outcome_values):
            return False
        expected_truncated = expected_structural_truncated or expected_outcome_truncated
        expected_complete = (
            simulation_mapping.get("complete") is True
            and baseline_complete
            and not expected_truncated
        )
        expected_comparison_reason = (
            "no earlier graph version is available"
            if baseline is None
            else (
                "candidate and baseline evidence are complete"
                if expected_complete
                else "candidate or baseline comparison evidence is incomplete"
            )
        )
        return (
            safety_mapping.get("passed") is True
            and len(check_statuses) >= len(REQUIRED_GRAPH_SAFETY_CHECKS)
            and all(
                check_statuses.get(name) == "pass"
                for name in REQUIRED_GRAPH_SAFETY_CHECKS
            )
            and simulation_mapping.get("complete") is True
            and simulation_mapping.get("errors") == []
            and simulation_mapping.get("safety") == safety_mapping
            and _simulation_is_valid(definition, simulation_mapping)
            and comparison_mapping.get("complete") is expected_complete
            and comparison_mapping.get("candidate_hash") == definition_hash
            and comparison_mapping.get("candidate_id")
            == f"{candidate_id}:{candidate_revision}"
            and comparison_mapping.get("truncated") is expected_truncated
            and comparison_mapping.get("reason") == expected_comparison_reason
            and list(structural_values) == list(expected_structural)
            and [dict(cast(Mapping[str, object], item)) for item in outcome_values]
            == [dict(item) for item in expected_outcomes]
        )
    except (ContractError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _baseline_definition(
    connection: Any,
    workflow_id: str,
    revision: int,
    comparison: Mapping[str, object],
) -> tuple[bool, GraphDefinition | None]:
    baseline_id = comparison.get("baseline_id")
    baseline_hash = comparison.get("baseline_hash")
    rows = connection.execute(
        """
        SELECT revision, definition_json
        FROM graph_definitions
        WHERE workflow_id = ? AND revision < ?
        ORDER BY revision
        """,
        (workflow_id, revision),
    ).fetchall()
    if not rows:
        return (baseline_id is None and baseline_hash is None), None
    if not isinstance(baseline_id, str) or not isinstance(baseline_hash, str):
        return False, None
    prefix, separator, raw_revision = baseline_id.rpartition(":")
    if separator != ":" or prefix != workflow_id:
        return False, None
    try:
        baseline_revision = int(raw_revision)
    except ValueError:
        return False, None
    row = next((item for item in rows if item["revision"] == baseline_revision), None)
    if row is None:
        return False, None
    try:
        definition = GraphDefinition.from_dict(json.loads(str(row["definition_json"])))
    except (ContractError, TypeError, ValueError, json.JSONDecodeError):
        return False, None
    valid = (
        definition.definition_id == baseline_id
        and graph_definition_hash(definition) == baseline_hash
    )
    return valid, definition if valid else None


def _only_keys(
    value: Mapping[str, object], allowed: set[str], required: set[str] | None = None
) -> bool:
    keys = set(value)
    return keys <= allowed and (required is None or required <= keys)


def _definition_shape_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    definition = cast(Mapping[str, object], value)
    if not _only_keys(
        definition,
        {
            "workflow_id",
            "revision",
            "schema_version",
            "nodes",
            "edges",
            "model_policy",
            "limits",
            "metadata",
        },
        {
            "workflow_id",
            "revision",
            "schema_version",
            "nodes",
            "edges",
            "model_policy",
            "limits",
            "metadata",
        },
    ):
        return False
    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
        return False
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes, bytearray)):
        return False
    for node in cast(Sequence[object], nodes):
        if not isinstance(node, Mapping):
            return False
        node_mapping = cast(Mapping[str, object], node)
        if not _only_keys(
            node_mapping,
            {"node_id", "kind", "reference", "metadata"},
            {"node_id", "kind", "reference", "metadata"},
        ):
            return False
        reference = node_mapping.get("reference")
        if not isinstance(reference, Mapping) or not _only_keys(
            cast(Mapping[str, object], reference),
            {"reference_id", "content_hash"},
            {"reference_id", "content_hash"},
        ):
            return False
    for edge in cast(Sequence[object], edges):
        if not isinstance(edge, Mapping) or not _only_keys(
            cast(Mapping[str, object], edge),
            {"source", "target", "condition"},
            {"source", "target", "condition"},
        ):
            return False
    model_policy = definition.get("model_policy")
    if not isinstance(model_policy, Mapping) or not _only_keys(
        cast(Mapping[str, object], model_policy),
        {"default_model", "allowed_models"},
        {"default_model", "allowed_models"},
    ):
        return False
    limits = definition.get("limits")
    return isinstance(limits, Mapping) and _only_keys(
        cast(Mapping[str, object], limits),
        {"max_retries", "max_loops", "timeout_seconds", "max_maintenance_passes"},
        {"max_retries", "max_loops", "timeout_seconds", "max_maintenance_passes"},
    )


def _safety_shape_valid(value: Mapping[str, object]) -> bool:
    checks = value.get("checks")
    if not _only_keys(
        value,
        {"workflow_id", "revision", "definition_hash", "passed", "checks"},
        {"workflow_id", "revision", "definition_hash", "passed", "checks"},
    ) or not isinstance(checks, Sequence):
        return False
    return all(
        isinstance(check, Mapping)
        and _only_keys(
            cast(Mapping[str, object], check),
            {"name", "status", "reason", "evidence"},
            {"name", "status", "reason", "evidence"},
        )
        for check in cast(Sequence[object], checks)
    )


def _simulation_shape_valid(value: Mapping[str, object]) -> bool:
    if not _only_keys(
        value,
        {
            "workflow_id",
            "revision",
            "definition_hash",
            "complete",
            "safety",
            "cases",
            "errors",
        },
        {
            "workflow_id",
            "revision",
            "definition_hash",
            "complete",
            "safety",
            "cases",
            "errors",
        },
    ):
        return False
    cases = value.get("cases")
    return isinstance(cases, Sequence) and not isinstance(
        cases, (str, bytes, bytearray)
    )


def _comparison_shape_valid(value: Mapping[str, object]) -> bool:
    return _only_keys(
        value,
        {
            "candidate_id",
            "candidate_hash",
            "baseline_id",
            "baseline_hash",
            "structural_changes",
            "outcome_changes",
            "complete",
            "reason",
            "truncated",
        },
        {
            "candidate_id",
            "candidate_hash",
            "baseline_id",
            "baseline_hash",
            "structural_changes",
            "outcome_changes",
            "complete",
            "reason",
            "truncated",
        },
    )


def _simulation_outcome_changes(
    candidate: Mapping[str, object], baseline: Mapping[str, object]
) -> tuple[tuple[Mapping[str, object], ...], bool]:
    candidate_cases = _simulation_signatures(candidate)
    baseline_cases = _simulation_signatures(baseline)
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


def _simulation_signatures(
    simulation: Mapping[str, object],
) -> dict[str, tuple[tuple[str, str, str, str | None], ...]]:
    raw_cases = simulation.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(
        raw_cases, (str, bytes, bytearray)
    ):
        return {}
    result: dict[str, tuple[tuple[str, str, str, str | None], ...]] = {}
    for raw_case in cast(Sequence[object], raw_cases):
        if not isinstance(raw_case, Mapping):
            continue
        case = cast(Mapping[str, object], raw_case)
        case_id = case.get("case_id")
        raw_steps = case.get("steps")
        if not isinstance(case_id, str) or not isinstance(raw_steps, Sequence):
            continue
        signature: list[tuple[str, str, str, str | None]] = []
        for raw_step in cast(Sequence[object], raw_steps):
            if not isinstance(raw_step, Mapping):
                continue
            step = cast(Mapping[str, object], raw_step)
            node_id = step.get("node_id")
            outcome = step.get("outcome")
            status = step.get("status")
            selected_edge = step.get("selected_edge")
            target = (
                cast(Mapping[str, object], selected_edge).get("target")
                if isinstance(selected_edge, Mapping)
                else None
            )
            if not all(isinstance(item, str) for item in (node_id, outcome, status)):
                continue
            node_id_text = cast(str, node_id)
            outcome_text = cast(str, outcome)
            status_text = cast(str, status)
            signature.append(
                (
                    node_id_text,
                    outcome_text,
                    status_text,
                    target if isinstance(target, str) else None,
                )
            )
        result[case_id] = tuple(signature)
    return result


def _simulation_is_valid(
    definition: GraphDefinition, simulation: Mapping[str, object]
) -> bool:
    if (
        simulation.get("workflow_id") != definition.workflow_id
        or simulation.get("revision") != definition.revision
        or simulation.get("definition_hash") != graph_definition_hash(definition)
    ):
        return False
    cases = simulation.get("cases")
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes, bytearray)):
        return False
    case_values = cast(Sequence[object], cases)
    if not case_values or len(case_values) > MAX_GRAPH_SIMULATION_CASES:
        return False
    case_ids: set[str] = set()
    node_ids = {node.node_id for node in definition.nodes}
    targets = {edge.target for edge in definition.edges}
    roots = sorted(node_id for node_id in node_ids if node_id not in targets)
    entry = roots[0] if roots else definition.nodes[0].node_id
    for case in case_values:
        if not isinstance(case, Mapping):
            return False
        case_mapping = cast(Mapping[str, object], case)
        case_id = case_mapping.get("case_id")
        status = case_mapping.get("status")
        steps = case_mapping.get("steps")
        if not _only_keys(
            case_mapping,
            {"case_id", "status", "reason", "passed", "steps"},
            {"case_id", "status", "reason", "passed", "steps"},
        ):
            return False
        if (
            not isinstance(case_id, str)
            or len(case_id) > MAX_GRAPH_SAFETY_TEXT
            or status not in {"complete", "paused"}
            or not isinstance(case_mapping.get("reason"), str)
            or len(cast(str, case_mapping["reason"])) > MAX_GRAPH_SAFETY_TEXT
            or case_mapping.get("passed") is not True
            or not isinstance(steps, Sequence)
            or isinstance(steps, (str, bytes, bytearray))
        ):
            return False
        if not case_id.strip() or case_id in case_ids:
            return False
        case_ids.add(case_id)
        step_values = cast(Sequence[object], steps)
        if not step_values or len(step_values) > definition.limits.max_loops:
            return False
        node_id = entry
        final_status: str | None = None
        index = 0
        for index, step in enumerate(step_values, 1):
            if not isinstance(step, Mapping):
                return False
            step_mapping = cast(Mapping[str, object], step)
            if set(step_mapping) - {
                "case_id",
                "step",
                "node_id",
                "outcome",
                "status",
                "selected_edge",
                "evidence",
                "question",
                "required_action",
                "validation_reason",
                "answer",
                "artifact_refs",
            }:
                return False
            if not {
                "case_id",
                "step",
                "node_id",
                "outcome",
                "status",
                "selected_edge",
                "evidence",
                "question",
                "required_action",
                "validation_reason",
                "answer",
                "artifact_refs",
            } <= set(step_mapping):
                return False
            if (
                type(step_mapping.get("step")) is not int
                or step_mapping.get("step") != index
                or step_mapping.get("node_id") != node_id
                or step_mapping.get("case_id") != case_id
            ):
                return False
            raw_outcome = step_mapping.get("outcome")
            evidence = step_mapping.get("evidence", {})
            if not isinstance(raw_outcome, str) or not isinstance(evidence, Mapping):
                return False
            question = step_mapping.get("question")
            required_action = step_mapping.get("required_action")
            validation_reason = step_mapping.get("validation_reason")
            answer = step_mapping.get("answer")
            artifact_refs = step_mapping.get("artifact_refs", [])
            if any(
                value is not None and not isinstance(value, str)
                for value in (question, required_action, validation_reason, answer)
            ):
                return False
            if any(
                isinstance(value, str) and len(value) > MAX_GRAPH_SAFETY_TEXT
                for value in (validation_reason, answer)
            ):
                return False
            if not isinstance(artifact_refs, Sequence) or isinstance(
                artifact_refs, (str, bytes, bytearray)
            ):
                return False
            if cast(Sequence[object], artifact_refs):
                return False
            if raw_outcome == TaskOutcome.QUESTION.value and (
                not isinstance(question, str)
                or not question.strip()
                or len(question) > MAX_GRAPH_SAFETY_TEXT
            ):
                return False
            if raw_outcome == TaskOutcome.BLOCKED.value and (
                not isinstance(required_action, str)
                or not required_action.strip()
                or len(required_action) > MAX_GRAPH_SAFETY_TEXT
            ):
                return False
            try:
                outcome = TaskOutcome(raw_outcome)
                result = TaskResult(
                    outcome,
                    cast(Mapping[str, object], evidence),
                    question=question if isinstance(question, str) else None,
                    required_action=(
                        required_action if isinstance(required_action, str) else None
                    ),
                    validation_reason=(
                        validation_reason
                        if isinstance(validation_reason, str)
                        else None
                    ),
                    answer=answer if isinstance(answer, str) else None,
                    artifact_refs=(),
                ).validated(_simulation_contract())
                transition = evaluate_graph_transition(
                    definition,
                    execution_id=f"verification:{definition.definition_id}:{case_id}",
                    task_id=f"fixture:{case_id}:{index}",
                    node_id=node_id,
                    result=result,
                    step=index,
                    attempt=1,
                    created_at="simulation",
                )
            except (ContractError, TypeError, ValueError):
                return False
            selected_edge = step_mapping.get("selected_edge")
            expected_edge = (
                transition.selected_edge.as_dict()
                if transition.selected_edge is not None
                else None
            )
            if (
                step_mapping.get("status") != transition.status.value
                or selected_edge != expected_edge
            ):
                return False
            if transition.status is GraphTransitionStatus.TERMINAL:
                final_status = "complete"
                break
            if transition.status is GraphTransitionStatus.PAUSED:
                final_status = "paused"
                break
            if transition.selected_edge is None or index == len(step_values):
                return False
            node_id = transition.selected_edge.target
        expected_reason = (
            "terminal path reached"
            if final_status == "complete"
            else "human outcome pauses the deterministic simulation"
        )
        if (
            final_status != status
            or len(step_values) != index
            or case_mapping.get("reason") != expected_reason
        ):
            return False
    return True


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise StoreError(f"{label} is invalid")
    return value


def _required_operator(value: object, label: str) -> str:
    actor = _required_text(value, label)
    if actor != "operator":
        raise StoreError("operator approval is required")
    return actor


def _simulation_contract() -> TaskContract:
    return TaskContract(
        "graph.safety.simulation",
        1,
        "fixture",
        {},
        (),
        (),
        tuple(TaskOutcome),
    )


def _required_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_GRAPH_REVISION:
        raise StoreError("revision must be a positive integer")
    return value


def _required_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise StoreError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as error:
        raise StoreError(f"{label} must be a SHA-256 hex digest") from error
    return value


__all__ = ["GraphSafetyMixin"]
