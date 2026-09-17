from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from beehaiive.agent import redact_worker_text, worker_secret_values
from beehaiive.contract_types.validation import is_credential_name
from beehaiive.graph import MAX_GRAPH_EDGES, MAX_GRAPH_NODES

MAX_DASHBOARD_TEXT_LENGTH = 4_000
MAX_DASHBOARD_ITEMS = 100
MAX_DASHBOARD_BYTES = 64_000
_SAFE_DASHBOARD_STRING_VALUES = frozenset(
    {
        "action",
        "branch",
        "commit_sha",
        "kind",
        "lease_id",
        "question_id",
        "repository",
        "repository_name",
        "run_id",
        "stage",
        "stage_label",
        "status",
        "target",
    }
)
_STRUCTURAL_DASHBOARD_KEYS = frozenset(
    {
        "action",
        "actions",
        "active",
        "active_workers",
        "allowed",
        "approved",
        "archived",
        "artifact_refs",
        "attempt",
        "activity_state",
        "autonomous_handoffs",
        "autonomous_current_step",
        "autonomous_process",
        "autonomous_status",
        "baseline",
        "baseline_fixtures",
        "branch",
        "candidate",
        "cases",
        "checks",
        "claimable",
        "commit_sha",
        "comparison",
        "completed",
        "counts",
        "created_at",
        "current_pbi",
        "definition_hash",
        "definitions",
        "delivery",
        "details",
        "edges",
        "empty",
        "error",
        "errors",
        "evidence",
        "evidence_hash",
        "event_limit",
        "events",
        "execution_id",
        "finished_at",
        "fixtures",
        "from_stage",
        "graph",
        "graph_trace",
        "id",
        "kind",
        "last_error",
        "last_output",
        "last_output_at",
        "last_poll_at",
        "last_result",
        "lease_expires_at",
        "lease_id",
        "message",
        "model",
        "metadata",
        "name",
        "nodes",
        "notification_status",
        "next_skill",
        "operator_questions",
        "outcome",
        "owner",
        "owner_id",
        "pbi_number",
        "project",
        "project_id",
        "process",
        "pid",
        "returncode",
        "recent_deliveries",
        "question",
        "reason",
        "repository",
        "repository_name",
        "result",
        "review",
        "revision",
        "run_id",
        "queues",
        "safety_evidence",
        "scheduler",
        "selected_edge",
        "stage",
        "status",
        "step",
        "step_started_at",
        "started_at",
        "supported_action_owners",
        "supported_action_readback",
        "supported_actions",
        "task",
        "task_contract",
        "task_result",
        "title",
        "timeout_seconds",
        "to_stage",
        "transition_evidence",
        "updated_at",
        "workflow_id",
        "workflow_ids",
        "workflow_queue",
        "workflow_skill_ids",
        "writer",
    }
)


def mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items = cast(Sequence[object], value)
    return [
        dict(cast(Mapping[str, object], item))
        for item in items
        if isinstance(item, Mapping)
    ]


def sequence(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(cast(Sequence[object], value))


def integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bounded_graph_trace_event(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    mapping_value = cast(Mapping[str, object], value)
    payload = json.dumps(mapping_value, ensure_ascii=False, separators=(",", ":"))
    if len(payload) <= MAX_DASHBOARD_TEXT_LENGTH:
        return mapping_value
    return {
        "execution_id": str(mapping_value.get("execution_id", ""))[:256],
        "task_id": str(mapping_value.get("task_id", ""))[:256],
        "workflow_id": str(mapping_value.get("workflow_id", ""))[:256],
        "revision": mapping_value.get("revision", 0),
        "node_id": str(mapping_value.get("node_id", ""))[:256],
        "step": mapping_value.get("step", 0),
        "attempt": mapping_value.get("attempt", 0),
        "outcome": str(mapping_value.get("outcome", ""))[:64],
        "status": str(mapping_value.get("status", ""))[:64],
        "selected_edge": None,
        "reason": str(mapping_value.get("reason", ""))[:512],
        "evidence": {"truncated": True},
        "created_at": str(mapping_value.get("created_at", ""))[:128],
        "question": None,
        "required_action": None,
        "replay_id": str(mapping_value.get("replay_id", ""))[:256],
    }


def safe_dashboard_value(
    value: object,
    secret_values: tuple[str, ...] = (),
    depth: int = 0,
    keep_last: bool = False,
    preserve_string: bool = False,
    preserve_items: bool = False,
    max_items: int = MAX_DASHBOARD_ITEMS,
    max_bytes: int | None = None,
) -> object:
    secrets = secret_values or worker_secret_values()
    if depth >= 12:
        return "[truncated]"
    if isinstance(value, str):
        if preserve_string:
            return redact_worker_text(
                value, secrets, max_length=MAX_DASHBOARD_TEXT_LENGTH
            )
        return redact_worker_text(value, secrets, max_length=MAX_DASHBOARD_TEXT_LENGTH)
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value).items()
        safe_mapping: dict[str, object] = {}
        for index, (key, item) in enumerate(items):
            if index >= MAX_DASHBOARD_ITEMS:
                break
            raw_key = str(key)
            output_key = raw_key[:200]
            if (
                is_credential_name(raw_key)
                or any(separator in raw_key for separator in ("=", ":"))
                or (
                    raw_key not in _STRUCTURAL_DASHBOARD_KEYS
                    and any(secret and secret in raw_key for secret in secrets)
                )
            ):
                output_key = redact_worker_text(raw_key, secrets, max_length=200)
            if is_credential_name(raw_key):
                safe_mapping[output_key] = "[redacted]"
            else:
                item_limit = (
                    MAX_GRAPH_NODES
                    if raw_key == "nodes"
                    else MAX_GRAPH_EDGES
                    if raw_key == "edges"
                    else max_items
                )
                safe_mapping[output_key] = safe_dashboard_value(
                    item,
                    secrets,
                    depth + 1,
                    raw_key
                    in {"events", "activity", "transition_evidence", "graph_trace"},
                    raw_key in _SAFE_DASHBOARD_STRING_VALUES,
                    raw_key
                    in {
                        "supported_actions",
                        "supported_action_owners",
                        "supported_action_readback",
                        "workflow_skill_ids",
                    },
                    max_items=item_limit,
                    max_bytes=MAX_DASHBOARD_BYTES if raw_key == "graph_trace" else None,
                )
        return safe_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = cast(Sequence[object], value)
        values = list(items)
        bounded_values = values[-max_items:] if keep_last else values[:max_items]
        sanitized_values = [
            safe_dashboard_value(
                item,
                secrets,
                depth + 1,
                preserve_string=preserve_items,
            )
            for item in bounded_values
        ]
        if max_bytes is None:
            return sanitized_values
        sanitized_values = [
            _bounded_graph_trace_event(item) for item in sanitized_values
        ]
        selected: list[object] = []
        total_bytes = 2
        for item in reversed(sanitized_values):
            item_bytes = len(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            separator_bytes = 1 if selected else 0
            if (
                item_bytes > max_bytes
                or total_bytes + separator_bytes + item_bytes > max_bytes
            ):
                if selected:
                    break
                continue
            selected.append(item)
            total_bytes += separator_bytes + item_bytes
        selected.reverse()
        return selected
    return value
