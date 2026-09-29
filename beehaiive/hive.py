from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, cast

from .database import SnapshotDatabase
from .models import ProjectCard, ProjectSnapshot

_ALERT_KINDS = frozenset({"blocked", "escalated", "interrupted", "stalled"})


class HiveAcknowledgementConflict(ValueError):
    pass


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _iso(value: Any) -> str | None:
    return str(value) if value is not None else None


def _duration(started_at: Any, finished_at: Any) -> float | None:
    started = _timestamp(started_at)
    finished = _timestamp(finished_at)
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def _parameter_value(value: Any, parameters: Mapping[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("{") and value.endswith("}") and len(value) > 2:
        return parameters.get(value[1:-1], value)
    result = value
    for name, parameter in parameters.items():
        result = result.replace("{" + name + "}", str(parameter))
    return result


def _condition_matches(
    condition: Mapping[str, Any],
    card: ProjectCard,
    status: str,
    parameters: Mapping[str, Any],
) -> bool:
    kind = str(condition.get("kind", ""))
    value = _parameter_value(condition.get("value"), parameters)
    if kind == "always":
        return True
    if kind == "item_status_is":
        return status == value
    if kind == "item_type_is":
        return card.type.casefold() == str(value).replace("_", "").casefold()
    if kind == "item_has_label":
        return str(value) in card.labels
    if kind == "item_lacks_label":
        return str(value) not in card.labels
    if kind == "item_repository_is":
        return card.repository == value
    return False


def _matches_wait_transition(
    definition: Mapping[str, Any],
    card: ProjectCard,
    status: str,
    parameters: Mapping[str, Any],
) -> bool:
    initial = str(definition.get("initial", ""))
    candidates: list[Mapping[str, Any]] = []
    for raw_transition in cast(list[Any], definition.get("transitions", [])):
        if not isinstance(raw_transition, Mapping):
            continue
        transition = cast(Mapping[str, Any], raw_transition)
        if str(transition.get("from")) == initial:
            candidates.append(transition)
    candidates.sort(key=lambda transition: int(transition.get("priority", 0)))
    for transition in candidates:
        raw_conditions = transition.get("conditions", [])
        conditions = (
            cast(list[Any], raw_conditions) if isinstance(raw_conditions, list) else []
        )
        if conditions and all(
            isinstance(condition, Mapping)
            and _condition_matches(
                cast(Mapping[str, Any], condition), card, status, parameters
            )
            for condition in conditions
        ):
            return True
    return False


def _color(agent_id: str) -> str:
    value = sum(
        (index + 1) * ord(character) for index, character in enumerate(agent_id)
    )
    hue = value % 360
    return f"hsl({hue} 75% 65%)"


def _station_map(definition: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for raw_state in cast(list[Any], definition.get("states", [])):
        if not isinstance(raw_state, Mapping):
            continue
        state = cast(dict[str, Any], raw_state)
        state_id = str(state.get("id", ""))
        if state_id:
            values[state_id] = state
    return values


def _transition_label(transition: Mapping[str, Any]) -> str:
    raw_conditions = transition.get("conditions", [])
    conditions = (
        cast(list[Any], raw_conditions) if isinstance(raw_conditions, list) else []
    )
    for raw_condition in conditions:
        if not isinstance(raw_condition, Mapping):
            continue
        condition = cast(Mapping[str, Any], raw_condition)
        if condition.get("kind") == "outcome_is":
            return str(condition.get("value", ""))
    return "always"


def _event_cursor(timestamp: str, kind: str, identity: str) -> str:
    return f"{timestamp}|{kind}|{identity}"


def _synthetic_alert_id(*values: str) -> int:
    total = sum(
        (index + 1) * ord(character) for index, character in enumerate("|".join(values))
    )
    return -(total or 1)


class HiveProjection:
    def __init__(
        self,
        database: SnapshotDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, Any]:
        now = self._clock().astimezone(UTC)
        return self.database.transaction(
            lambda connection: self._snapshot(connection, now)
        )

    def events(self, since: str | None = None) -> dict[str, Any]:
        now = self._clock().astimezone(UTC)
        return self.database.transaction(
            lambda connection: self._events(connection, now, since or "")
        )

    def acknowledge(
        self, pass_id: str, acknowledged_by: str = "operator"
    ) -> dict[str, str] | None:
        now = self._clock().astimezone(UTC).isoformat()
        actor = acknowledged_by.strip() or "operator"

        def update(connection: sqlite3.Connection) -> dict[str, str] | None:
            row = connection.execute(
                "SELECT acknowledged_at, acknowledged_by FROM agent_passes "
                "WHERE id = ?",
                (pass_id,),
            ).fetchone()
            if row is None:
                return None
            if row["acknowledged_at"] is None:
                active_alert = connection.execute(
                    "SELECT 1 FROM agent_alerts WHERE pass_id = ? "
                    "AND kind IN ('blocked', 'escalated', 'interrupted', 'stalled') "
                    "UNION ALL SELECT 1 FROM agent_passes WHERE id = ? "
                    "AND status IN ('interrupted', 'stalled') "
                    "UNION ALL SELECT 1 FROM agent_steps WHERE pass_id = ? "
                    "AND outcome = 'blocked' LIMIT 1",
                    (pass_id, pass_id, pass_id),
                ).fetchone()
                if active_alert is None:
                    raise HiveAcknowledgementConflict("pass has no active alert")
                connection.execute(
                    "UPDATE agent_passes SET acknowledged_at = ?, acknowledged_by = ? "
                    "WHERE id = ?",
                    (now, actor, pass_id),
                )
                return {
                    "pass_id": pass_id,
                    "acknowledged_at": now,
                    "acknowledged_by": actor,
                }
            return {
                "pass_id": pass_id,
                "acknowledged_at": str(row["acknowledged_at"]),
                "acknowledged_by": str(row["acknowledged_by"] or actor),
            }

        return self.database.transaction(update)

    def _snapshot(
        self, connection: sqlite3.Connection, now: datetime
    ) -> dict[str, Any]:
        agents = connection.execute(
            "SELECT * FROM agents ORDER BY created_at, id"
        ).fetchall()
        passes = connection.execute(
            "SELECT * FROM agent_passes ORDER BY started_at, id"
        ).fetchall()
        steps = connection.execute(
            "SELECT * FROM agent_steps ORDER BY started_at, pass_id, sequence"
        ).fetchall()
        alerts = connection.execute(
            "SELECT * FROM agent_alerts ORDER BY created_at, id"
        ).fetchall()
        definitions = self._definitions(connection, agents)
        steps_by_pass: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
        for step in steps:
            steps_by_pass[str(step["pass_id"])].append(step)
        passes_by_id = {str(value["id"]): value for value in passes}
        agents_by_id = {str(value["id"]): value for value in agents}
        timing = self._timing(passes, steps, now)
        units: list[dict[str, Any]] = []
        units_by_region_station: defaultdict[tuple[int, int, str], int] = defaultdict(
            int
        )
        for agent in agents:
            agent_id = str(agent["id"])
            workflow_id = int(agent["workflow_id"])
            revision = int(agent["workflow_revision"])
            definition = definitions.get((workflow_id, revision), {})
            states = _station_map(definition)
            initial = str(definition.get("initial", ""))
            pass_id = (
                str(agent["current_pass_id"]) if agent["current_pass_id"] else None
            )
            current_pass = passes_by_id.get(pass_id) if pass_id else None
            state_id = (
                str(agent["current_state"]) if agent["current_state"] else initial
            )
            if current_pass is not None and current_pass["current_state"]:
                state_id = str(current_pass["current_state"])
            if state_id not in states:
                state_id = initial
            if str(agent["status"]) == "stopped":
                state_id = initial
            current_step = next(
                (
                    value
                    for value in steps_by_pass.get(pass_id or "", [])
                    if str(value["status"]) == "running"
                ),
                None,
            )
            step_started_at = _iso(current_step["started_at"]) if current_step else None
            stats = timing.get((workflow_id, revision, state_id), (0, None))
            elapsed = _duration(step_started_at, now.isoformat())
            typical = stats[1] if stats[0] >= 3 else None
            units_by_region_station[(workflow_id, revision, state_id)] += 1
            item = _json_object(current_pass["item_json"]) if current_pass else None
            units.append(
                {
                    "agent_id": agent_id,
                    "name": str(agent["name"]),
                    "status": str(agent["status"]),
                    "region": workflow_id,
                    "workflow_id": workflow_id,
                    "workflow_revision": revision,
                    "station": state_id,
                    "pass_id": pass_id,
                    "item": item,
                    "step_started_at": step_started_at,
                    "typical_seconds": typical,
                    "elapsed_seconds": elapsed,
                    "overdue": typical is not None
                    and elapsed is not None
                    and elapsed > typical * 3,
                    "progress": (
                        min(1.0, elapsed / typical)
                        if typical and elapsed is not None
                        else None
                    ),
                    "timing": {
                        "sample_count": stats[0],
                        "typical_seconds": typical,
                        "label": "history" if typical is not None else "no history",
                    },
                    "current_step": self._step_dict(current_step)
                    if current_step
                    else None,
                    "steps": [
                        self._step_dict(value)
                        for value in steps_by_pass.get(pass_id or "", [])
                    ],
                    "color": _color(agent_id),
                    "parked": str(agent["status"]) == "stopped",
                    "stalled": str(agent["status"]) == "stalled",
                }
            )
        waiting = self._waiting_counts(connection, definitions, agents, now)
        stations_by_region = {
            key: self._stations(
                definition,
                key[0],
                key[1],
                units_by_region_station,
                steps,
                passes_by_id,
                timing,
                waiting.get(str(key[0]), 0),
                now,
            )
            for key, definition in definitions.items()
        }
        regions: list[dict[str, Any]] = []
        for (workflow_id, revision), definition in definitions.items():
            station_values = stations_by_region[(workflow_id, revision)]
            roads: list[dict[str, Any]] = []
            for raw_transition in cast(list[Any], definition.get("transitions", [])):
                if not isinstance(raw_transition, Mapping):
                    continue
                transition = cast(Mapping[str, Any], raw_transition)
                roads.append(
                    {
                        "from": str(transition.get("from", "")),
                        "to": str(transition.get("to", "")),
                        "outcome": _transition_label(transition),
                        "priority": int(transition.get("priority", 0)),
                    }
                )
            layout = {
                str(station["id"]): dict(cast(dict[str, Any], station["layout"]))
                for station in station_values
                if isinstance(station.get("layout"), Mapping)
            }
            regions.append(
                {
                    "workflow_id": workflow_id,
                    "revision": revision,
                    "name": str(definition.get("name", f"Workflow {workflow_id}")),
                    "initial": str(definition.get("initial", "")),
                    "stations": station_values,
                    "roads": roads,
                    "layout": layout,
                }
            )
        return {
            "generated_at": now.isoformat(),
            "regions": sorted(
                regions, key=lambda value: (value["workflow_id"], value["revision"])
            ),
            "units": units,
            "alerts": self._alerts(
                alerts, passes_by_id, steps_by_pass, agents_by_id, definitions
            ),
            "waiting": waiting,
        }

    def _definitions(
        self, connection: sqlite3.Connection, agents: list[sqlite3.Row]
    ) -> dict[tuple[int, int], dict[str, Any]]:
        keys = {
            (int(agent["workflow_id"]), int(agent["workflow_revision"]))
            for agent in agents
        }
        definitions: dict[tuple[int, int], dict[str, Any]] = {}
        for workflow_id, revision in sorted(keys):
            row = connection.execute(
                "SELECT definition_json FROM workflow_revisions WHERE workflow_id = ? "
                "AND revision = ?",
                (workflow_id, revision),
            ).fetchone()
            if row is not None:
                definition = _json_object(row["definition_json"])
                if definition:
                    definitions[(workflow_id, revision)] = definition
        return definitions

    def _timing(
        self,
        passes: list[sqlite3.Row],
        steps: list[sqlite3.Row],
        now: datetime,
    ) -> dict[tuple[int, int, str], tuple[int, float | None]]:
        pass_keys: set[str] = set()
        by_workflow: defaultdict[tuple[int, int], list[sqlite3.Row]] = defaultdict(list)
        for value in passes:
            if value["finished_at"] is not None:
                by_workflow[
                    (int(value["workflow_id"]), int(value["workflow_revision"]))
                ].append(value)
        for values in by_workflow.values():
            values.sort(key=lambda value: str(value["finished_at"] or ""), reverse=True)
            pass_keys.update(str(value["id"]) for value in values[:30])
        durations: defaultdict[tuple[int, int, str], list[float]] = defaultdict(list)
        pass_by_id = {str(value["id"]): value for value in passes}
        for step in steps:
            pass_id = str(step["pass_id"])
            if pass_id not in pass_keys or str(step["status"]) != "completed":
                continue
            value = _duration(step["started_at"], step["finished_at"])
            if value is not None:
                current_pass = pass_by_id.get(pass_id)
                if current_pass is not None:
                    durations[
                        (
                            int(current_pass["workflow_id"]),
                            int(current_pass["workflow_revision"]),
                            str(step["state_id"]),
                        )
                    ].append(value)
        del now
        return {
            key: (len(values), float(median(values)))
            for key, values in durations.items()
        }

    def _stations(
        self,
        definition: Mapping[str, Any],
        workflow_id: int,
        revision: int,
        units_by_region_station: Mapping[tuple[int, int, str], int],
        steps: list[sqlite3.Row],
        passes_by_id: Mapping[str, sqlite3.Row],
        timing: Mapping[tuple[int, int, str], tuple[int, float | None]],
        waiting_count: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        transitions = [
            cast(Mapping[str, Any], value)
            for value in cast(list[Any], definition.get("transitions", []))
            if isinstance(value, Mapping)
        ]
        outgoing: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for transition in transitions:
            outgoing[str(transition.get("from", ""))].append(transition)
        finished_recent: defaultdict[str, int] = defaultdict(int)
        recent_steps: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        recent_cutoff = now - timedelta(hours=24)
        for step in steps:
            current_pass = passes_by_id.get(str(step["pass_id"]))
            if current_pass is None:
                continue
            if (
                int(current_pass["workflow_id"]) != workflow_id
                or int(current_pass["workflow_revision"]) != revision
            ):
                continue
            finished = _timestamp(step["finished_at"])
            if (
                str(step["status"]) == "completed"
                and finished is not None
                and finished >= recent_cutoff
            ):
                state_id = str(step["state_id"])
                finished_recent[state_id] += 1
                recent_steps[state_id].append(
                    {
                        "sequence": int(step["sequence"]),
                        "outcome": step["outcome"],
                        "summary": str(step["summary"]),
                        "finished_at": step["finished_at"],
                    }
                )
        result: list[dict[str, Any]] = []
        for raw_state in cast(list[Any], definition.get("states", [])):
            if not isinstance(raw_state, Mapping):
                continue
            state = cast(dict[str, Any], raw_state)
            state_id = str(state.get("id", ""))
            sample_count, sample_median = timing.get(
                (workflow_id, revision, state_id), (0, None)
            )
            typical = sample_median if sample_count >= 3 else None
            layout = state.get("layout")
            layout_value = (
                dict(cast(Mapping[str, Any], layout))
                if isinstance(layout, Mapping)
                else {"x": 0, "y": 0}
            )
            result.append(
                {
                    "id": state_id,
                    "title": str(state.get("title", state_id)),
                    "action": str(state.get("action", "")),
                    "prompt": str(state.get("prompt", "")),
                    "skill": state.get("skill"),
                    "outcomes": list(state.get("outcomes", []))
                    if isinstance(state.get("outcomes"), list)
                    else [],
                    "layout": layout_value,
                    "depot": state_id == str(definition.get("initial", "")),
                    "terminal": str(state.get("action")) == "escalate"
                    or not outgoing[state_id],
                    "agents_present": units_by_region_station.get(
                        (workflow_id, revision, state_id), 0
                    ),
                    "steps_completed_24h": finished_recent.get(state_id, 0),
                    "recent_steps": recent_steps.get(state_id, [])[-10:],
                    "duration": {
                        "sample_count": sample_count,
                        "typical_seconds": typical,
                        "label": "history" if typical is not None else "no history",
                    },
                    "waiting_items": waiting_count
                    if state_id == str(definition.get("initial", ""))
                    else 0,
                    "roads": [
                        {
                            "to": str(transition.get("to", "")),
                            "outcome": _transition_label(transition),
                        }
                        for transition in outgoing[state_id]
                    ],
                    "workflow_revision": revision,
                }
            )
        return result

    def _waiting_counts(
        self,
        connection: sqlite3.Connection,
        definitions: Mapping[tuple[int, int], Mapping[str, Any]],
        agents: list[sqlite3.Row],
        now: datetime,
    ) -> dict[str, int]:
        del now
        row = connection.execute(
            "SELECT payload FROM project_snapshot WHERE id = 1"
        ).fetchone()
        snapshot = (
            ProjectSnapshot.from_dict(_json_object(row["payload"])) if row else None
        )
        claims = {
            str(value["item_key"])
            for value in connection.execute(
                "SELECT item_key FROM agent_claims"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        if snapshot is None:
            return {str(workflow_id): 0 for workflow_id, _ in definitions}
        for workflow_id, revision in definitions:
            definition = definitions[(workflow_id, revision)]
            candidates = [
                agent
                for agent in agents
                if int(agent["workflow_id"]) == workflow_id
                and int(agent["workflow_revision"]) == revision
            ]
            count = 0
            for column in snapshot.columns:
                for card in column.items:
                    if card.item_key in claims:
                        continue
                    if any(
                        (
                            not str(agent["repository"])
                            or card.repository == str(agent["repository"])
                        )
                        and _matches_wait_transition(
                            definition,
                            card,
                            column.status,
                            _json_object(agent["parameters_json"]),
                        )
                        for agent in candidates
                    ):
                        count += 1
            key = str(workflow_id)
            counts[key] = counts.get(key, 0) + count
        return counts

    def _alerts(
        self,
        alerts: list[sqlite3.Row],
        passes_by_id: Mapping[str, sqlite3.Row],
        steps_by_pass: Mapping[str, list[sqlite3.Row]],
        agents_by_id: Mapping[str, sqlite3.Row],
        definitions: Mapping[tuple[int, int], Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        values: dict[tuple[str, int | None, str], dict[str, Any]] = {}
        for alert in alerts:
            kind = str(alert["kind"])
            if kind not in _ALERT_KINDS:
                continue
            pass_id = str(alert["pass_id"]) if alert["pass_id"] else None
            current_pass = passes_by_id.get(pass_id or "")
            if current_pass is not None and current_pass["acknowledged_at"] is not None:
                continue
            agent_id = str(alert["agent_id"])
            step = self._step_for_alert(
                steps_by_pass.get(pass_id or "", []), alert["sequence"]
            )
            state_id = (
                str(step["state_id"])
                if step
                else str(current_pass["current_state"])
                if current_pass
                else ""
            )
            key = (agent_id, int(alert["id"]), kind)
            values[key] = self._alert_dict(
                int(alert["id"]),
                agent_id,
                pass_id,
                alert["sequence"],
                kind,
                str(alert["message"]),
                str(alert["created_at"]),
                state_id,
                current_pass,
                agents_by_id,
                definitions,
            )
        for pass_id, current_pass in passes_by_id.items():
            kind = str(current_pass["status"])
            if (
                kind not in {"stalled", "interrupted"}
                or current_pass["acknowledged_at"] is not None
            ):
                continue
            agent_id = str(current_pass["agent_id"])
            if not any(
                value["pass_id"] == pass_id and value["kind"] == kind
                for value in values.values()
            ):
                alert_id = _synthetic_alert_id(agent_id, pass_id, kind)
                values[(agent_id, alert_id, kind)] = self._alert_dict(
                    alert_id,
                    agent_id,
                    pass_id,
                    None,
                    kind,
                    str(current_pass["stalled_reason"] or f"pass {kind}"),
                    str(current_pass["finished_at"] or current_pass["started_at"]),
                    str(current_pass["current_state"]),
                    current_pass,
                    agents_by_id,
                    definitions,
                )
        for pass_id, pass_steps in steps_by_pass.items():
            current_pass = passes_by_id.get(pass_id)
            if current_pass is None or current_pass["acknowledged_at"] is not None:
                continue
            for step in pass_steps:
                if step["outcome"] != "blocked":
                    continue
                agent_id = str(current_pass["agent_id"])
                alert_id = _synthetic_alert_id(
                    agent_id, pass_id, str(step["sequence"]), "blocked"
                )
                values[(agent_id, alert_id, "blocked")] = self._alert_dict(
                    alert_id,
                    agent_id,
                    pass_id,
                    step["sequence"],
                    "blocked",
                    str(step["summary"] or "step returned blocked"),
                    str(step["finished_at"] or step["started_at"]),
                    str(step["state_id"]),
                    current_pass,
                    agents_by_id,
                    definitions,
                )
        return sorted(
            values.values(),
            key=lambda value: (str(value["created_at"]), int(value["id"])),
        )

    @staticmethod
    def _step_for_alert(steps: list[sqlite3.Row], sequence: Any) -> sqlite3.Row | None:
        if sequence is None:
            return None
        return next(
            (step for step in steps if int(step["sequence"]) == int(sequence)), None
        )

    def _alert_dict(
        self,
        alert_id: int,
        agent_id: str,
        pass_id: str | None,
        sequence: Any,
        kind: str,
        message: str,
        created_at: str,
        state_id: str,
        current_pass: sqlite3.Row | None,
        agents_by_id: Mapping[str, sqlite3.Row],
        definitions: Mapping[tuple[int, int], Mapping[str, Any]],
    ) -> dict[str, Any]:
        agent = agents_by_id.get(agent_id)
        workflow_id = int(agent["workflow_id"]) if agent is not None else None
        revision = int(agent["workflow_revision"]) if agent is not None else None
        definition = definitions.get((workflow_id or -1, revision or -1), {})
        state = _station_map(definition).get(state_id, {})
        return {
            "id": alert_id,
            "agent_id": agent_id,
            "agent_name": str(agent["name"]) if agent is not None else agent_id,
            "pass_id": pass_id,
            "sequence": int(sequence) if sequence is not None else None,
            "kind": kind,
            "message": message,
            "created_at": created_at,
            "workflow_id": workflow_id,
            "workflow_revision": revision,
            "station": state_id,
            "station_title": str(state.get("title", state_id)),
            "item": _json_object(current_pass["item_json"]) if current_pass else None,
        }

    def _events(
        self, connection: sqlite3.Connection, now: datetime, since: str
    ) -> dict[str, Any]:
        passes = connection.execute(
            "SELECT * FROM agent_passes ORDER BY started_at, id"
        ).fetchall()
        steps = connection.execute(
            "SELECT * FROM agent_steps ORDER BY started_at, pass_id, sequence"
        ).fetchall()
        events: list[dict[str, Any]] = []
        for value in passes:
            pass_id = str(value["id"])
            agent_id = str(value["agent_id"])
            started = str(value["started_at"])
            events.append(
                self._event(
                    started,
                    "pass",
                    f"{agent_id}:{pass_id}:started",
                    "pass_started",
                    agent_id,
                    pass_id,
                    None,
                    str(value["current_state"]),
                    None,
                    str(value["status"]),
                )
            )
            if value["finished_at"] is not None:
                finished = str(value["finished_at"])
                events.append(
                    self._event(
                        finished,
                        "pass",
                        f"{agent_id}:{pass_id}:finished",
                        "pass_finished",
                        agent_id,
                        pass_id,
                        None,
                        str(value["current_state"]),
                        None,
                        str(value["status"]),
                    )
                )
        for value in steps:
            pass_id = str(value["pass_id"])
            pass_row = next(
                (item for item in passes if str(item["id"]) == pass_id), None
            )
            agent_id = str(pass_row["agent_id"]) if pass_row is not None else ""
            sequence = int(value["sequence"])
            state_id = str(value["state_id"])
            started = str(value["started_at"])
            events.append(
                self._event(
                    started,
                    "step",
                    f"{agent_id}:{pass_id}:{sequence}:started",
                    "step_started",
                    agent_id,
                    pass_id,
                    sequence,
                    state_id,
                    None,
                    str(value["status"]),
                )
            )
            if value["finished_at"] is not None:
                finished = str(value["finished_at"])
                events.append(
                    self._event(
                        finished,
                        "step",
                        f"{agent_id}:{pass_id}:{sequence}:finished",
                        "step_finished",
                        agent_id,
                        pass_id,
                        sequence,
                        state_id,
                        value["outcome"],
                        str(value["status"]),
                    )
                )
        events.sort(key=lambda value: (str(value["timestamp"]), str(value["cursor"])))
        cursor = events[-1]["cursor"] if events else since
        selected = [
            value
            for value in events
            if not since or since in {"0", "-"} or str(value["cursor"]) > since
        ]
        return {
            "generated_at": now.isoformat(),
            "cursor": cursor,
            "next_cursor": cursor,
            "events": selected,
        }

    @staticmethod
    def _event(
        timestamp: str,
        kind: str,
        identity: str,
        event_type: str,
        agent_id: str,
        pass_id: str,
        sequence: int | None,
        state_id: str,
        outcome: Any,
        status: str,
    ) -> dict[str, Any]:
        return {
            "cursor": _event_cursor(timestamp, kind, identity),
            "timestamp": timestamp,
            "event": event_type,
            "type": event_type,
            "kind": kind,
            "agent_id": agent_id,
            "pass_id": pass_id,
            "sequence": sequence,
            "state_id": state_id,
            "outcome": outcome,
            "status": status,
        }

    @staticmethod
    def _step_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "state_id": str(row["state_id"]),
            "status": str(row["status"]),
            "outcome": row["outcome"],
            "summary": str(row["summary"]),
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
        }
