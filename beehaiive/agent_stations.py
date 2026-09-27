from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import cast

from beehaiive.agent import redact_worker_text, worker_secret_values
from beehaiive.persistence import OrchestratorStore, StoreError
from beehaiive.persistence.helpers.lease_helpers import _now

MAX_STATION_UPDATES = 10
MAX_STATION_AGENTS = 100
MAX_STATION_EXPLANATION = 4_000
DEFAULT_PHASE_AVERAGES = {
    "mining": 300,
    "smelting": 900,
    "crafting": 600,
}
_EFFORT_LABEL = re.compile(r"\beffort\s+(?P<points>\d+)\b", re.IGNORECASE)
_STATION_BY_STAGE = {
    "refine": ("mining", "Mining"),
    "implement": ("smelting", "Smelting"),
    "pull_request": ("crafting", "Crafting"),
    "backlog": ("idle", "Idle"),
}


class AgentStationServiceError(StoreError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentStationService:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def view(self, project_id: str) -> dict[str, object]:
        try:
            state = self.store.project_state(project_id, event_limit=50)
        except StoreError as exc:
            raise AgentStationServiceError("not_found", str(exc)) from exc
        phase_averages = self._phase_averages()
        agents: list[dict[str, object]] = []
        for repository in _mappings(state.get("repositories")):
            repository_name = _text(repository.get("name"), "unknown")
            for pbi in _mappings(repository.get("pbis")):
                agent = self._agent_for_pbi(
                    project_id,
                    repository_name,
                    pbi,
                    phase_averages,
                )
                if agent is not None:
                    agents.append(agent)
                self._persist_issue_if_needed(project_id, repository_name, pbi)
        agents = agents[:MAX_STATION_AGENTS]
        try:
            issues = self.store.station_issues_for_project(project_id)
        except StoreError as exc:
            raise AgentStationServiceError("persistence", str(exc)) from exc
        stations = [
            {
                "id": station_id,
                "label": label,
                "description": description,
                "active_agents": sum(
                    1 for agent in agents if agent["station_id"] == station_id
                ),
            }
            for station_id, label, description in (
                ("mining", "Mining", "Refinement and discovery work"),
                ("smelting", "Smelting", "Implementation and execution work"),
                ("crafting", "Crafting", "Pull request, review, and delivery work"),
                ("idle", "Idle", "Known work without an active phase"),
            )
        ]
        return {
            "project_id": project_id,
            "project_name": _text(state.get("name"), project_id),
            "generated_at": _now(),
            "stations": stations,
            "agents": agents,
            "issues": issues,
            "counts": {
                "agents": len(agents),
                "visible_issues": sum(1 for issue in issues if issue.get("visible")),
                "issues": len(issues),
            },
        }

    def act_on_issue(
        self,
        project_id: str,
        issue_id: str,
        action: str,
        actor: str,
        note: str = "",
    ) -> dict[str, object]:
        if action not in {"dismiss", "address", "resolve"}:
            raise AgentStationServiceError(
                "invalid_action", "Unsupported station issue action"
            )
        try:
            issue = self.store.station_issue_action(
                project_id, issue_id, action, actor, note
            )
        except StoreError as exc:
            code = (
                "not_found" if "not found" in str(exc).casefold() else "invalid_action"
            )
            raise AgentStationServiceError(code, str(exc)) from exc
        return {"issue": issue}

    def _agent_for_pbi(
        self,
        project_id: str,
        repository_name: str,
        pbi: Mapping[str, object],
        phase_averages: Mapping[str, int],
    ) -> dict[str, object] | None:
        run_id = pbi.get("run_id")
        session = _mapping(pbi.get("agent_session"))
        status = _text(pbi.get("status"))
        session_state = _text(session.get("state")).casefold()
        is_active = (
            status
            in {
                "active",
                "awaiting_operator",
            }
            or session_state == "active"
        )
        if not is_active:
            return None
        stage_id, station_label = _station_for_pbi(pbi)
        worker_id = _text(session.get("worker_id"), "unassigned")
        updates = _recent_updates(pbi, session)
        movement = _movement(pbi, stage_id)
        estimate = _estimate(pbi, stage_id, phase_averages)
        return {
            "id": f"{repository_name}#{pbi.get('number')}",
            "worker_id": worker_id,
            "repository": repository_name,
            "pbi_number": pbi.get("number"),
            "title": _bounded_text(pbi.get("title"), 300),
            "run_id": run_id,
            "state": status or _text(session.get("state"), "unknown"),
            "station_id": stage_id,
            "station_label": station_label,
            "movement": movement,
            "estimate": estimate,
            "updates": updates,
            "active": is_active,
            "project_id": project_id,
        }

    def _persist_issue_if_needed(
        self, project_id: str, repository_name: str, pbi: Mapping[str, object]
    ) -> None:
        error = _bounded_text(pbi.get("last_error"), MAX_STATION_EXPLANATION)
        if not error:
            questions = _mappings(pbi.get("operator_questions"))
            pending = next(
                (
                    _bounded_text(question.get("question"), MAX_STATION_EXPLANATION)
                    for question in questions
                    if _text(question.get("status")).casefold() == "pending"
                ),
                "",
            )
            error = pending
        if not error:
            return
        station_id, station_label = _station_for_pbi(pbi)
        run_id = _optional_text(pbi.get("run_id"))
        identity = "|".join(
            (
                project_id,
                repository_name,
                str(pbi.get("number", "")),
                run_id or "",
                station_id,
                error,
            )
        )
        issue_id = (
            "station-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        )
        explanation = f"{station_label}: {error}"
        try:
            self.store.ensure_station_issue(
                issue_id,
                project_id,
                repository_name,
                _positive_int(pbi.get("number")),
                run_id,
                station_id,
                explanation,
            )
        except StoreError as exc:
            raise AgentStationServiceError("persistence", str(exc)) from exc

    def _phase_averages(self) -> dict[str, int]:
        values = dict(DEFAULT_PHASE_AVERAGES)
        settings = self.store.get_runtime_settings().get("station_phase_averages")
        settings_mapping: Mapping[str, object] = (
            cast(Mapping[str, object], settings)
            if isinstance(settings, Mapping)
            else cast(Mapping[str, object], {})
        )
        if settings_mapping:
            for station_id in values:
                candidate = settings_mapping.get(station_id)
                if (
                    isinstance(candidate, (int, float))
                    and not isinstance(candidate, bool)
                    and 1 <= candidate <= 86_400
                ):
                    values[station_id] = int(candidate)
        return values


def _station_for_pbi(pbi: Mapping[str, object]) -> tuple[str, str]:
    stage = _text(pbi.get("stage")).casefold()
    if stage in _STATION_BY_STAGE:
        return _STATION_BY_STAGE[stage]
    planning_status = _text(pbi.get("planning_status")).casefold()
    if planning_status in {"in progress", "implementing"}:
        return "smelting", "Smelting"
    if planning_status in {"todo", "backlog", "refine", "refining"}:
        return "mining", "Mining"
    return "idle", "Idle"


def _estimate(
    pbi: Mapping[str, object], station_id: str, phase_averages: Mapping[str, int]
) -> dict[str, object]:
    points = _effort_points(_mapping(pbi.get("metadata")))
    average = phase_averages.get(station_id)
    if points is None or average is None:
        return {
            "status": "unavailable",
            "reason": "Effort label or phase average is unavailable",
        }
    return {
        "status": "available",
        "effort_points": points,
        "phase_average_seconds": average,
        "duration_seconds": points * average,
        "basis": f"Effort {points} × {average} seconds per {station_id} phase point",
        "precision": "bounded estimate; not a completion promise",
    }


def _effort_points(metadata: Mapping[str, object]) -> int | None:
    candidates: list[str] = []
    for key in ("effort_label", "effort", "labels"):
        value = metadata.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            candidates.extend(
                item for item in cast(Sequence[object], value) if isinstance(item, str)
            )
    for candidate in candidates:
        match = _EFFORT_LABEL.search(candidate)
        if match is None and candidate.strip().isdigit():
            points = int(candidate.strip())
        elif match is not None:
            points = int(match.group("points"))
        else:
            continue
        if 1 <= points <= 100:
            return points
    return None


def _movement(pbi: Mapping[str, object], station_id: str) -> dict[str, object]:
    events = _mappings(pbi.get("events"))
    latest = events[-1] if events else {}
    from_stage = _text(latest.get("from_stage"))
    to_stage = _text(latest.get("to_stage"))
    if from_stage and to_stage and from_stage != to_stage:
        from_station, _ = _station_for_pbi({"stage": from_stage})
        to_station, _ = _station_for_pbi({"stage": to_stage})
        return {
            "state": "moving",
            "from_station": from_station,
            "to_station": to_station,
            "station_id": station_id,
            "updated_at": _text(latest.get("created_at") or latest.get("time")),
        }
    return {"state": "stationed", "station_id": station_id}


def _recent_updates(
    pbi: Mapping[str, object], session: Mapping[str, object]
) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for event in _mappings(pbi.get("events")):
        updates.append(
            {
                "kind": "workflow",
                "text": _bounded_text(
                    event.get("event_type")
                    or event.get("details")
                    or "Workflow state updated",
                    500,
                ),
                "at": _text(event.get("created_at") or event.get("time")),
            }
        )
    for event in _mappings(session.get("events")):
        updates.append(
            {
                "kind": _text(event.get("kind"), "session"),
                "text": _bounded_text(event.get("text"), 500),
                "at": _text(event.get("timestamp")),
            }
        )
    return updates[-MAX_STATION_UPDATES:]


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    values = cast(Sequence[object], value)
    return [
        dict(cast(Mapping[str, object], item))
        for item in values
        if isinstance(item, Mapping)
    ]


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _text(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _optional_text(value: object) -> str | None:
    value = _text(value)
    return value or None


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        if isinstance(value, Mapping):
            value_mapping = cast(Mapping[object, object], value)
            value = ", ".join(f"{key}: {item}" for key, item in value_mapping.items())
        else:
            value = str(value) if value is not None else ""
    return redact_worker_text(value, worker_secret_values(), max_length=limit).strip()


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


__all__ = ["AgentStationService", "AgentStationServiceError"]
