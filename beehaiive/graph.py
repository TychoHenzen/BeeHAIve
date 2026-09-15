from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import cast

from .contract_types import ContractError, _bounded_value
from .routing import ModelTier

GRAPH_DEFINITION_SCHEMA_VERSION = 1
MAX_GRAPH_TEXT = 128
MAX_GRAPH_NODES = 64
MAX_GRAPH_EDGES = 128
MAX_GRAPH_METADATA_KEYS = 40
MAX_GRAPH_METADATA_DEPTH = 4
MAX_GRAPH_RETRIES = 8
MAX_GRAPH_LOOPS = 32
MAX_GRAPH_MAINTENANCE_PASSES = 8
MAX_GRAPH_TIMEOUT_SECONDS = 900.0

ALLOWED_TOOL_CAPABILITIES = frozenset(
    {
        "read_project",
        "read_repository",
        "read_pull_request",
        "run_checks",
        "request_operator",
    }
)

_LOGICAL_REFERENCE = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_METADATA_KEY = re.compile(
    r"(?:token|secret|password|credential|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
_EXECUTABLE_METADATA_KEY = re.compile(
    r"(?:command|argv|shell|executable|script|exec)", re.IGNORECASE
)


class GraphDefinitionError(ContractError):
    """Raised when a graph definition is unsafe or internally inconsistent."""


class GraphNodeKind(StrEnum):
    PROMPT = "prompt"
    SKILL = "skill"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class GraphReference:
    reference_id: str
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _validate_logical_reference(self.reference_id, "reference_id")
        if self.content_hash is not None:
            _validate_text(self.content_hash, "content_hash")
            if _CONTENT_HASH.fullmatch(self.content_hash) is None:
                raise GraphDefinitionError("content_hash must be a SHA-256 hex digest")

    @classmethod
    def from_dict(cls, value: object) -> GraphReference:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("reference must be an object")
        mapping = cast(Mapping[str, object], value)
        return cls(
            _required_text(mapping.get("reference_id"), "reference_id"),
            _optional_text(mapping.get("content_hash"), "content_hash"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    kind: GraphNodeKind
    reference: GraphReference
    metadata: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        _validate_logical_reference(self.node_id, "node_id")
        if not isinstance(cast(object, self.kind), GraphNodeKind):
            raise GraphDefinitionError("kind must be a GraphNodeKind value")
        if not isinstance(cast(object, self.reference), GraphReference):
            raise GraphDefinitionError("reference must be a GraphReference value")
        if self.kind is GraphNodeKind.TOOL and (
            self.reference.reference_id not in ALLOWED_TOOL_CAPABILITIES
        ):
            raise GraphDefinitionError("tool reference is not allowlisted")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    @classmethod
    def from_dict(cls, value: object) -> GraphNode:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("node must be an object")
        mapping = cast(Mapping[str, object], value)
        raw_kind = _required_text(mapping.get("kind"), "kind")
        try:
            kind = GraphNodeKind(raw_kind)
        except ValueError as error:
            raise GraphDefinitionError(
                f"unknown graph node kind: {raw_kind}"
            ) from error
        metadata = mapping.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise GraphDefinitionError("node metadata must be an object")
        return cls(
            _required_text(mapping.get("node_id"), "node_id"),
            kind,
            GraphReference.from_dict(mapping.get("reference")),
            cast(Mapping[str, object], metadata),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "reference": self.reference.as_dict(),
            "metadata": _thaw_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    condition: str = "always"

    def __post_init__(self) -> None:
        _validate_logical_reference(self.source, "edge source")
        _validate_logical_reference(self.target, "edge target")
        _validate_logical_reference(self.condition, "edge condition")

    @classmethod
    def from_dict(cls, value: object) -> GraphEdge:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("edge must be an object")
        mapping = cast(Mapping[str, object], value)
        return cls(
            _required_text(mapping.get("source"), "edge source"),
            _required_text(mapping.get("target"), "edge target"),
            _required_text(mapping.get("condition", "always"), "edge condition"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "condition": self.condition,
        }


@dataclass(frozen=True, slots=True)
class GraphModelPolicy:
    default_model: ModelTier = ModelTier.LUNA
    allowed_models: tuple[ModelTier, ...] = (ModelTier.LUNA,)

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.default_model), ModelTier):
            raise GraphDefinitionError("default_model must be a ModelTier value")
        if self.default_model is ModelTier.HUMAN:
            raise GraphDefinitionError("human is not a graph model")
        if not self.allowed_models:
            raise GraphDefinitionError("allowed_models must not be empty")
        if any(
            not isinstance(cast(object, model), ModelTier) or model is ModelTier.HUMAN
            for model in self.allowed_models
        ):
            raise GraphDefinitionError("allowed_models must contain supported models")
        if len(set(self.allowed_models)) != len(self.allowed_models):
            raise GraphDefinitionError("allowed_models must be unique")
        if self.default_model not in self.allowed_models:
            raise GraphDefinitionError("default_model must be allowed")
        object.__setattr__(self, "allowed_models", tuple(self.allowed_models))

    @classmethod
    def from_dict(cls, value: object) -> GraphModelPolicy:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("model_policy must be an object")
        mapping = cast(Mapping[str, object], value)
        default_model = _model_tier(mapping.get("default_model"), "default_model")
        raw_allowed = mapping.get("allowed_models")
        if not isinstance(raw_allowed, list) or not raw_allowed:
            raise GraphDefinitionError("allowed_models must be a non-empty list")
        return cls(
            default_model,
            tuple(
                _model_tier(item, "allowed_models")
                for item in cast(list[object], raw_allowed)
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "default_model": self.default_model.value,
            "allowed_models": [model.value for model in self.allowed_models],
        }


@dataclass(frozen=True, slots=True)
class GraphExecutionLimits:
    max_retries: int = 3
    max_loops: int = 8
    timeout_seconds: float = 900.0
    max_maintenance_passes: int = 1

    def __post_init__(self) -> None:
        for value, label, maximum in (
            (self.max_retries, "max_retries", MAX_GRAPH_RETRIES),
            (self.max_loops, "max_loops", MAX_GRAPH_LOOPS),
            (
                self.max_maintenance_passes,
                "max_maintenance_passes",
                MAX_GRAPH_MAINTENANCE_PASSES,
            ),
        ):
            if type(value) is not int or value < 0 or value > maximum:
                raise GraphDefinitionError(
                    f"{label} must be an integer between 0 and {maximum}"
                )
        if (
            type(self.timeout_seconds) not in {int, float}
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > MAX_GRAPH_TIMEOUT_SECONDS
        ):
            raise GraphDefinitionError(
                f"timeout_seconds must be between 0 and {MAX_GRAPH_TIMEOUT_SECONDS:g}"
            )

    @classmethod
    def from_dict(cls, value: object) -> GraphExecutionLimits:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("limits must be an object")
        mapping = cast(Mapping[str, object], value)
        return cls(
            _optional_int(mapping.get("max_retries", 3), "max_retries"),
            _optional_int(mapping.get("max_loops", 8), "max_loops"),
            _optional_float(mapping.get("timeout_seconds", 900.0), "timeout_seconds"),
            _optional_int(
                mapping.get("max_maintenance_passes", 1), "max_maintenance_passes"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "max_retries": self.max_retries,
            "max_loops": self.max_loops,
            "timeout_seconds": self.timeout_seconds,
            "max_maintenance_passes": self.max_maintenance_passes,
        }


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    workflow_id: str
    revision: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...] = ()
    model_policy: GraphModelPolicy = GraphModelPolicy()
    limits: GraphExecutionLimits = GraphExecutionLimits()
    metadata: Mapping[str, object] = MappingProxyType({})
    schema_version: int = GRAPH_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_logical_reference(self.workflow_id, "workflow_id")
        if type(self.revision) is not int or self.revision <= 0:
            raise GraphDefinitionError("revision must be a positive integer")
        if (
            type(self.schema_version) is not int
            or self.schema_version != GRAPH_DEFINITION_SCHEMA_VERSION
        ):
            raise GraphDefinitionError(
                f"unknown graph schema version: {self.schema_version}"
            )
        if not self.nodes or len(self.nodes) > MAX_GRAPH_NODES:
            raise GraphDefinitionError(
                f"nodes must contain between 1 and {MAX_GRAPH_NODES} entries"
            )
        if len(self.edges) > MAX_GRAPH_EDGES:
            raise GraphDefinitionError(
                f"too many graph edges, maximum {MAX_GRAPH_EDGES}"
            )
        if any(not isinstance(cast(object, node), GraphNode) for node in self.nodes):
            raise GraphDefinitionError("nodes must contain GraphNode values")
        if any(not isinstance(cast(object, edge), GraphEdge) for edge in self.edges):
            raise GraphDefinitionError("edges must contain GraphEdge values")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise GraphDefinitionError("node identifiers must be unique")
        node_id_set = set(node_ids)
        if any(
            edge.source not in node_id_set or edge.target not in node_id_set
            for edge in self.edges
        ):
            raise GraphDefinitionError("edges must reference known node identifiers")
        edge_ids = [(edge.source, edge.target, edge.condition) for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise GraphDefinitionError("edges must be unique")
        if not isinstance(cast(object, self.model_policy), GraphModelPolicy):
            raise GraphDefinitionError("model_policy must be a GraphModelPolicy value")
        if not isinstance(cast(object, self.limits), GraphExecutionLimits):
            raise GraphDefinitionError("limits must be a GraphExecutionLimits value")
        object.__setattr__(
            self, "nodes", tuple(sorted(self.nodes, key=lambda n: n.node_id))
        )
        object.__setattr__(
            self,
            "edges",
            tuple(sorted(self.edges, key=lambda e: (e.source, e.target, e.condition))),
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    @property
    def definition_id(self) -> str:
        return f"{self.workflow_id}:{self.revision}"

    @classmethod
    def from_dict(cls, value: object) -> GraphDefinition:
        if not isinstance(value, Mapping):
            raise GraphDefinitionError("graph definition must be an object")
        mapping = cast(Mapping[str, object], value)
        raw_nodes = mapping.get("nodes")
        if not isinstance(raw_nodes, list):
            raise GraphDefinitionError("nodes must be a list")
        raw_edges = mapping.get("edges", [])
        if not isinstance(raw_edges, list):
            raise GraphDefinitionError("edges must be a list")
        metadata = mapping.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise GraphDefinitionError("metadata must be an object")
        raw_schema_version = mapping.get(
            "schema_version", GRAPH_DEFINITION_SCHEMA_VERSION
        )
        if type(raw_schema_version) is not int:
            raise GraphDefinitionError("schema_version must be an integer")
        missing_policy = object()
        raw_policy = mapping.get("model_policy", missing_policy)
        model_policy = (
            GraphModelPolicy()
            if raw_policy is missing_policy
            else GraphModelPolicy.from_dict(raw_policy)
        )
        return cls(
            _required_text(mapping.get("workflow_id"), "workflow_id"),
            _optional_int(mapping.get("revision"), "revision"),
            tuple(GraphNode.from_dict(item) for item in cast(list[object], raw_nodes)),
            tuple(GraphEdge.from_dict(item) for item in cast(list[object], raw_edges)),
            model_policy,
            GraphExecutionLimits.from_dict(mapping.get("limits", {})),
            cast(Mapping[str, object], metadata),
            raw_schema_version,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "schema_version": self.schema_version,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "model_policy": self.model_policy.as_dict(),
            "limits": self.limits.as_dict(),
            "metadata": _thaw_metadata(self.metadata),
        }


def _validate_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise GraphDefinitionError(f"{label} is required")
    if len(value) > MAX_GRAPH_TEXT:
        raise GraphDefinitionError(f"{label} exceeds {MAX_GRAPH_TEXT} characters")


def _required_text(value: object, label: str) -> str:
    _validate_text(value, label)
    return cast(str, value)


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    _validate_text(value, label)
    return cast(str, value)


def _validate_logical_reference(value: object, label: str) -> None:
    _validate_text(value, label)
    if _LOGICAL_REFERENCE.fullmatch(cast(str, value)) is None:
        raise GraphDefinitionError(f"{label} must be an allowlisted logical reference")


def _model_tier(value: object, label: str) -> ModelTier:
    if not isinstance(value, str):
        raise GraphDefinitionError(f"{label} must be a model alias")
    try:
        return ModelTier(value)
    except ValueError as error:
        raise GraphDefinitionError(f"{label} is not a supported model alias") from error


def _optional_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise GraphDefinitionError(f"{label} must be an integer")
    return value


def _optional_float(value: object, label: str) -> float:
    if type(value) not in {int, float}:
        raise GraphDefinitionError(f"{label} must be a finite number")
    if not isinstance(value, (int, float)):
        raise GraphDefinitionError(f"{label} must be a finite number")
    if not isfinite(value):
        raise GraphDefinitionError(f"{label} must be a finite number")
    return float(value)


def _freeze_metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    try:
        bounded = _bounded_value(value)
    except ContractError as error:
        raise GraphDefinitionError(f"metadata is not bounded: {error}") from error
    if (
        not isinstance(bounded, dict)
        or len(cast(dict[str, object], bounded)) > MAX_GRAPH_METADATA_KEYS
    ):
        raise GraphDefinitionError("metadata must be a bounded object")
    bounded_mapping = cast(dict[str, object], bounded)
    _reject_sensitive_metadata(bounded_mapping)
    return cast(Mapping[str, object], _freeze_value(bounded_mapping))


def _reject_sensitive_metadata(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            if _SENSITIVE_METADATA_KEY.search(str(key)) is not None:
                raise GraphDefinitionError("metadata must not contain credentials")
            if _EXECUTABLE_METADATA_KEY.search(str(key)) is not None:
                raise GraphDefinitionError(
                    "metadata must not contain executable commands"
                )
            _reject_sensitive_metadata(item)
    elif isinstance(value, list):
        items = cast(list[object], value)
        for item in items:
            _reject_sensitive_metadata(item)


def _freeze_value(value: object, depth: int = 0) -> object:
    if depth > MAX_GRAPH_METADATA_DEPTH:
        raise GraphDefinitionError("metadata is nested too deeply")
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType(
            {str(key): _freeze_value(item, depth + 1) for key, item in mapping.items()}
        )
    if isinstance(value, list):
        items = cast(list[object], value)
        return tuple(_freeze_value(item, depth + 1) for item in items)
    return value


def _thaw_metadata(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _thaw_value(item)
        for key, item in sorted(value.items(), key=lambda pair: pair[0])
    }


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): _thaw_value(item)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        items = cast(tuple[object, ...], value)
        return [_thaw_value(item) for item in items]
    return value


__all__ = [
    "ALLOWED_TOOL_CAPABILITIES",
    "GRAPH_DEFINITION_SCHEMA_VERSION",
    "GraphDefinition",
    "GraphDefinitionError",
    "GraphEdge",
    "GraphExecutionLimits",
    "GraphModelPolicy",
    "GraphNode",
    "GraphNodeKind",
    "GraphReference",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_LOOPS",
    "MAX_GRAPH_MAINTENANCE_PASSES",
    "MAX_GRAPH_NODES",
    "MAX_GRAPH_RETRIES",
    "MAX_GRAPH_TEXT",
    "MAX_GRAPH_TIMEOUT_SECONDS",
]
