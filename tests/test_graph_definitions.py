from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from beehaiive import (
    ALLOWED_TOOL_CAPABILITIES,
    GraphDefinition,
    GraphDefinitionError,
    GraphEdge,
    GraphExecutionLimits,
    GraphModelPolicy,
    GraphNode,
    GraphNodeKind,
    GraphReference,
    ModelTier,
    RoutingConfig,
)
from beehaiive.storage import OrchestratorStore, StoreError


def _definition(revision: int = 1) -> GraphDefinition:
    return GraphDefinition(
        workflow_id="review-flow",
        revision=revision,
        nodes=(
            GraphNode(
                "start",
                GraphNodeKind.PROMPT,
                GraphReference("prompt/review"),
                {"purpose": "review"},
            ),
            GraphNode(
                "checks",
                GraphNodeKind.TOOL,
                GraphReference("run_checks"),
            ),
        ),
        edges=(GraphEdge("start", "checks", "pass"),),
        model_policy=GraphModelPolicy(ModelTier.LUNA, (ModelTier.LUNA, ModelTier.SOL)),
        limits=GraphExecutionLimits(
            max_retries=2, max_loops=4, timeout_seconds=120, max_maintenance_passes=1
        ),
        metadata={"owner": "quality", "tags": ["safe", "bounded"]},
    )


def test_graph_definition_round_trip_is_sorted_and_bounded() -> None:
    definition = _definition()
    restored = GraphDefinition.from_dict(definition.as_dict())
    assert restored == definition
    assert definition.definition_id == "review-flow:1"
    assert [node.node_id for node in definition.nodes] == ["checks", "start"]
    assert "command" not in json.dumps(definition.as_dict()).lower()
    assert ALLOWED_TOOL_CAPABILITIES
    with pytest.raises(TypeError):
        definition.metadata["owner"] = "other"  # type: ignore[index]


def test_graph_definition_rejects_structure_and_trust_violations() -> None:
    with pytest.raises(GraphDefinitionError, match="unique"):
        GraphDefinition(
            "flow",
            1,
            (
                GraphNode("node", GraphNodeKind.PROMPT, GraphReference("prompt/a")),
                GraphNode("node", GraphNodeKind.SKILL, GraphReference("skill/a")),
            ),
        )
    with pytest.raises(GraphDefinitionError, match="known node"):
        GraphDefinition(
            "flow",
            1,
            (GraphNode("node", GraphNodeKind.PROMPT, GraphReference("prompt/a")),),
            (GraphEdge("node", "missing"),),
        )
    with pytest.raises(GraphDefinitionError, match="allowlisted"):
        GraphNode("tool", GraphNodeKind.TOOL, GraphReference("shell"))
    with pytest.raises(GraphDefinitionError, match="credentials"):
        GraphNode(
            "node",
            GraphNodeKind.PROMPT,
            GraphReference("prompt/a"),
            {"api_key": "secret"},
        )
    with pytest.raises(GraphDefinitionError, match="executable"):
        GraphNode(
            "tool",
            GraphNodeKind.TOOL,
            GraphReference("run_checks"),
            {"command": "pytest"},
        )
    with pytest.raises(GraphDefinitionError, match="SHA-256"):
        GraphReference("prompt/a", "bad-hash")
    with pytest.raises(GraphDefinitionError, match="supported"):
        GraphModelPolicy.from_dict(
            {"default_model": "unknown", "allowed_models": ["unknown"]}
        )


def test_graph_definition_rejects_unknown_versions_and_invalid_limits() -> None:
    with pytest.raises(GraphDefinitionError, match="unknown graph schema"):
        GraphDefinition(
            "flow",
            1,
            (GraphNode("node", GraphNodeKind.PROMPT, GraphReference("prompt/a")),),
            schema_version=2,
        )
    with pytest.raises(GraphDefinitionError, match="max_retries"):
        GraphExecutionLimits(max_retries=9)
    with pytest.raises(GraphDefinitionError, match="timeout_seconds"):
        GraphExecutionLimits(timeout_seconds=901)
    with pytest.raises(GraphDefinitionError, match="timeout_seconds"):
        GraphExecutionLimits(timeout_seconds=math.nan)
    with pytest.raises(GraphDefinitionError, match="model_policy"):
        GraphDefinition.from_dict({**_definition().as_dict(), "model_policy": None})
    with pytest.raises(GraphDefinitionError, match="allowed_models"):
        GraphModelPolicy(ModelTier.LUNA, (ModelTier.LUNA, ModelTier.LUNA))
    with pytest.raises(GraphDefinitionError, match="metadata"):
        GraphDefinition(
            "flow",
            1,
            (GraphNode("node", GraphNodeKind.PROMPT, GraphReference("prompt/a")),),
            metadata={"nested": {"too": {"deep": {"for": {"this": "contract"}}}}},
        )


def test_graph_definition_persists_replays_and_keeps_revisions(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "graph.sqlite3")
    first = _definition()
    assert store.save_graph_definition(first) == first
    assert store.save_graph_definition(first) == first
    assert store.graph_definition_for("review-flow") == first
    assert store.graph_definition_for("review-flow", 1) == first
    second = _definition(2)
    store.save_graph_definition(second)
    assert store.graph_definitions_for("review-flow") == (first, second)
    with pytest.raises(StoreError, match="immutable"):
        store.save_graph_definition(
            GraphDefinition(
                "review-flow",
                1,
                (
                    GraphNode(
                        "other", GraphNodeKind.PROMPT, GraphReference("prompt/other")
                    ),
                ),
            )
        )
    store.close()
    reopened = OrchestratorStore(tmp_path / "graph.sqlite3")
    assert reopened.graph_definition_for("review-flow", 2) == second
    reopened.close()


def test_graph_model_policy_matches_existing_router_aliases() -> None:
    config = RoutingConfig()
    for model in _definition().model_policy.allowed_models:
        assert config.spec_for(model).tier is model


def test_graph_definition_storage_fails_closed_on_corrupt_json(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "graph-corrupt.sqlite3")
    store.save_graph_definition(_definition())
    store._connection.execute("UPDATE graph_definitions SET definition_json = '[]'")
    with pytest.raises(StoreError, match="graph definition is invalid"):
        store.graph_definition_for("review-flow")
    store.close()
