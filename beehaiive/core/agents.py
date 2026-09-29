from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .config import CoreConfig
from .database import SnapshotDatabase
from .models import ProjectCard, ProjectSnapshot
from .snapshot import ProjectSnapshotService
from .workflows import WorkflowStore, validate_workflow

AGENT_STATUSES = frozenset({"stopped", "waiting", "working", "stalled"})
PASS_TERMINAL_STATUSES = frozenset(
    {"completed", "stalled", "reset", "stopped", "interrupted"}
)
_LOG_LOCK = threading.Lock()


class AgentError(ValueError):
    pass


class AgentConflict(AgentError):
    pass


class AgentExecutionError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _now(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def item_key(card: ProjectCard) -> str:
    if card.item_key:
        return card.item_key
    return ":".join(
        value
        for value in (
            card.type,
            card.repository or "",
            str(card.number) if card.number is not None else "",
            card.url or "",
        )
        if value
    )


class AgentStore:
    def __init__(
        self,
        database: SnapshotDatabase,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def recover_abandoned(self) -> None:
        now = _now(self._clock)

        def recover(connection: Any) -> None:
            running = connection.execute(
                "SELECT id, agent_id FROM agent_passes WHERE status = 'running'"
            ).fetchall()
            for row in running:
                connection.execute(
                    "UPDATE agent_passes SET status = 'interrupted', finished_at = ?, "
                    "stalled_reason = ? WHERE id = ?",
                    (now, "server restarted while pass was running", row["id"]),
                )
                connection.execute(
                    "UPDATE agent_steps SET status = 'interrupted', "
                    "finished_at = ?, summary = ? WHERE pass_id = ? "
                    "AND status = 'running'",
                    (now, "server restarted while step was running", row["id"]),
                )
                connection.execute(
                    "DELETE FROM agent_claims WHERE pass_id = ?", (row["id"],)
                )
                connection.execute(
                    "INSERT INTO agent_alerts(agent_id, pass_id, kind, message, "
                    "created_at) "
                    "VALUES (?, ?, 'interrupted', ?, ?)",
                    (
                        row["agent_id"],
                        row["id"],
                        "running pass interrupted on startup",
                        now,
                    ),
                )
            connection.execute(
                "UPDATE agents SET status = 'stopped', current_state = NULL, "
                "current_pass_id = NULL, updated_at = ? "
                "WHERE status IN ('working', 'waiting')",
                (now,),
            )

        self.database.transaction(recover)

    def create(
        self,
        *,
        name: str,
        workflow_id: int,
        workflow_revision: int,
        parameters: Mapping[str, Any],
        repository: str,
        checkout_path: str,
        model: str | None,
    ) -> dict[str, Any]:
        agent_id = str(uuid.uuid4())
        now = _now(self._clock)

        def insert(connection: Any) -> None:
            connection.execute(
                "INSERT INTO agents(id, name, workflow_id, workflow_revision, "
                "parameters_json, repository, checkout_path, model, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "'stopped', ?, ?)",
                (
                    agent_id,
                    name,
                    workflow_id,
                    workflow_revision,
                    _json(dict(parameters)),
                    repository,
                    checkout_path,
                    model,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_assignments(workflow_id, agent_id) VALUES (?, ?)",
                (workflow_id, agent_id),
            )

        self.database.transaction(insert)
        result = self.get(agent_id)
        if result is None:
            raise RuntimeError("created agent could not be read back")
        return result

    def list(self) -> list[dict[str, Any]]:
        return self.database.transaction(
            lambda connection: [
                self._agent_dict(row)
                for row in connection.execute(
                    "SELECT * FROM agents ORDER BY created_at, id"
                ).fetchall()
            ]
        )

    def get(self, agent_id: str) -> dict[str, Any] | None:
        def read(connection: Any) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return None
            result = self._agent_dict(row)
            current_pass_id = row["current_pass_id"]
            result["current_pass"] = (
                self._pass_dict(connection, str(current_pass_id))
                if current_pass_id
                else None
            )
            result["history"] = [
                self._pass_dict(connection, str(pass_row["id"]))
                for pass_row in connection.execute(
                    "SELECT id FROM agent_passes WHERE agent_id = ? "
                    "ORDER BY started_at DESC LIMIT 20",
                    (agent_id,),
                ).fetchall()
            ]
            result["alerts"] = [
                {
                    "id": int(alert["id"]),
                    "pass_id": alert["pass_id"],
                    "sequence": alert["sequence"],
                    "kind": str(alert["kind"]),
                    "message": str(alert["message"]),
                    "created_at": str(alert["created_at"]),
                }
                for alert in connection.execute(
                    "SELECT id, pass_id, sequence, kind, message, created_at "
                    "FROM agent_alerts WHERE agent_id = ? "
                    "ORDER BY id DESC LIMIT 50",
                    (agent_id,),
                ).fetchall()
            ]
            return result

        return self.database.transaction(read)

    def update(
        self,
        agent_id: str,
        values: Mapping[str, Any],
        *,
        workflow_id: int | None = None,
        workflow_revision: int | None = None,
    ) -> dict[str, Any] | None:
        allowed = {
            "name",
            "parameters_json",
            "repository",
            "checkout_path",
            "model",
            "workflow_id",
            "workflow_revision",
        }
        assignments: list[str] = []
        parameters: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            parameters.append(value)
        if not assignments:
            return self.get(agent_id)
        now = _now(self._clock)
        parameters.extend((now, agent_id))

        def change(connection: Any) -> bool:
            row = connection.execute(
                "SELECT status, workflow_id FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) != "stopped":
                raise AgentConflict("agent must be stopped before it can be updated")
            connection.execute(
                f"UPDATE agents SET {', '.join(assignments)}, updated_at = ? "
                "WHERE id = ?",
                (*parameters,),
            )
            if workflow_id is not None and workflow_id != int(row["workflow_id"]):
                connection.execute(
                    "DELETE FROM workflow_assignments WHERE agent_id = ?",
                    (agent_id,),
                )
                connection.execute(
                    "INSERT INTO workflow_assignments(workflow_id, agent_id) "
                    "VALUES (?, ?)",
                    (workflow_id, agent_id),
                )
            return True

        if not self.database.transaction(change):
            return None
        return self.get(agent_id)

    def delete(self, agent_id: str) -> str:
        def remove(connection: Any) -> str:
            row = connection.execute(
                "SELECT status FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return "missing"
            if str(row["status"]) != "stopped":
                return "running"
            connection.execute(
                "DELETE FROM agent_claims WHERE agent_id = ?", (agent_id,)
            )
            connection.execute(
                "DELETE FROM agent_steps WHERE pass_id IN "
                "(SELECT id FROM agent_passes WHERE agent_id = ?)",
                (agent_id,),
            )
            connection.execute(
                "DELETE FROM agent_alerts WHERE agent_id = ?", (agent_id,)
            )
            connection.execute(
                "DELETE FROM agent_passes WHERE agent_id = ?", (agent_id,)
            )
            connection.execute(
                "DELETE FROM workflow_assignments WHERE agent_id = ?", (agent_id,)
            )
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            return "deleted"

        return self.database.transaction(remove)

    def set_status(
        self,
        agent_id: str,
        status: str,
        *,
        state: str | None = None,
        pass_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        if status not in AGENT_STATUSES:
            raise ValueError(f"unsupported agent status: {status}")
        self.database.transaction(
            lambda connection: connection.execute(
                "UPDATE agents SET status = ?, current_state = ?, current_pass_id = ?, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (status, state, pass_id, last_error, _now(self._clock), agent_id),
            )
        )

    def claim(
        self,
        agent_id: str,
        card: ProjectCard,
        *,
        status: str,
        workflow_id: int,
        workflow_revision: int,
        initial_state: str,
    ) -> str | None:
        pass_id = str(uuid.uuid4())
        now = _now(self._clock)
        key = item_key(card)

        def reserve(connection: Any) -> str | None:
            agent = connection.execute(
                "SELECT status, current_pass_id FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if agent is None or str(agent["status"]) not in {"waiting", "working"}:
                return None
            if agent["current_pass_id"] is not None:
                return None
            existing = connection.execute(
                "SELECT 1 FROM agent_claims WHERE item_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return None
            connection.execute(
                "INSERT INTO agent_passes(id, agent_id, item_key, item_json, "
                "workflow_id, "
                "workflow_revision, current_state, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    pass_id,
                    agent_id,
                    key,
                    _json({"status": status, **card.as_dict()}),
                    workflow_id,
                    workflow_revision,
                    initial_state,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO agent_claims(item_key, agent_id, pass_id, claimed_at) "
                "VALUES (?, ?, ?, ?)",
                (key, agent_id, pass_id, now),
            )
            connection.execute(
                "UPDATE agents SET status = 'working', current_state = ?, "
                "current_pass_id = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (initial_state, pass_id, now, agent_id),
            )
            return pass_id

        return self.database.transaction(reserve)

    def begin_step(
        self,
        pass_id: str,
        state_id: str,
        action: str,
        command: Sequence[str],
        log_path: str,
    ) -> int:
        now = _now(self._clock)

        def insert(connection: Any) -> int:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM agent_steps WHERE pass_id = ?",
                (pass_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            connection.execute(
                "INSERT INTO agent_steps(pass_id, sequence, state_id, action, status, "
                "command_json, started_at, log_path) VALUES (?, ?, ?, ?, "
                "'running', ?, ?, ?)",
                (
                    pass_id,
                    sequence,
                    state_id,
                    action,
                    _json(list(command)),
                    now,
                    log_path,
                ),
            )
            connection.execute(
                "UPDATE agent_passes SET current_state = ?, "
                "step_count = step_count + 1 "
                "WHERE id = ?",
                (state_id, pass_id),
            )
            connection.execute(
                "UPDATE agents SET current_state = ?, updated_at = ? "
                "WHERE current_pass_id = ?",
                (state_id, now, pass_id),
            )
            return sequence

        return self.database.transaction(insert)

    def finish_step(
        self,
        pass_id: str,
        sequence: int,
        *,
        status: str,
        outcome: str | None = None,
        summary: str = "",
        handover: Mapping[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        self.database.transaction(
            lambda connection: connection.execute(
                "UPDATE agent_steps SET status = ?, outcome = ?, summary = ?, "
                "handover_json = ?, finished_at = ?, exit_code = ? "
                "WHERE pass_id = ? AND sequence = ?",
                (
                    status,
                    outcome,
                    summary,
                    _json(dict(handover or {})),
                    _now(self._clock),
                    exit_code,
                    pass_id,
                    sequence,
                ),
            )
        )

    def record_alert(
        self,
        agent_id: str,
        pass_id: str,
        sequence: int | None,
        kind: str,
        message: str,
    ) -> None:
        self.database.transaction(
            lambda connection: connection.execute(
                "INSERT INTO agent_alerts(agent_id, pass_id, sequence, kind, "
                "message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, pass_id, sequence, kind, message, _now(self._clock)),
            )
        )

    def complete_pass(self, agent_id: str, pass_id: str) -> None:
        self._finish_pass(agent_id, pass_id, "completed", None, release=True)

    def stall_pass(
        self,
        agent_id: str,
        pass_id: str,
        reason: str,
        *,
        auto_reset: bool,
        sequence: int | None = None,
    ) -> None:
        now = _now(self._clock)

        def stall(connection: Any) -> None:
            connection.execute(
                "UPDATE agent_passes SET status = 'stalled', finished_at = ?, "
                "stalled_reason = ? WHERE id = ?",
                (now, reason, pass_id),
            )
            connection.execute(
                "INSERT INTO agent_alerts(agent_id, pass_id, sequence, kind, "
                "message, created_at) "
                "VALUES (?, ?, ?, 'stalled', ?, ?)",
                (agent_id, pass_id, sequence, reason, now),
            )
            if auto_reset:
                connection.execute(
                    "DELETE FROM agent_claims WHERE pass_id = ?", (pass_id,)
                )
                connection.execute(
                    "UPDATE agents SET status = 'waiting', current_state = NULL, "
                    "current_pass_id = NULL, last_error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (reason, now, agent_id),
                )
            else:
                connection.execute(
                    "UPDATE agents SET status = 'stalled', last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (reason, now, agent_id),
                )

        self.database.transaction(stall)

    def reset(self, agent_id: str) -> bool:
        now = _now(self._clock)

        def reset_stall(connection: Any) -> bool:
            row = connection.execute(
                "SELECT current_pass_id, status FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) != "stalled":
                raise AgentConflict("only a stalled agent can be reset")
            pass_id = row["current_pass_id"]
            if pass_id:
                connection.execute(
                    "UPDATE agent_passes SET status = 'reset', finished_at = ? "
                    "WHERE id = ?",
                    (now, pass_id),
                )
                connection.execute(
                    "DELETE FROM agent_claims WHERE pass_id = ?", (pass_id,)
                )
            connection.execute(
                "INSERT INTO agent_alerts(agent_id, pass_id, kind, message, "
                "created_at) "
                "VALUES (?, ?, 'reset', 'operator reset the stalled agent', ?)",
                (agent_id, pass_id, now),
            )
            connection.execute(
                "UPDATE agents SET status = 'waiting', current_state = NULL, "
                "current_pass_id = NULL, last_error = NULL, updated_at = ? "
                "WHERE id = ?",
                (now, agent_id),
            )
            return True

        return self.database.transaction(reset_stall)

    def stop(self, agent_id: str, reason: str = "stopped by operator") -> bool:
        now = _now(self._clock)

        def stop_agent(connection: Any) -> bool:
            row = connection.execute(
                "SELECT current_pass_id FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return False
            pass_id = row["current_pass_id"]
            if pass_id:
                connection.execute(
                    "UPDATE agent_passes SET status = 'stopped', finished_at = ?, "
                    "stalled_reason = ? WHERE id = ? AND status = 'running'",
                    (now, reason, pass_id),
                )
                connection.execute(
                    "UPDATE agent_steps SET status = 'stopped', finished_at = ?, "
                    "summary = ? WHERE pass_id = ? AND status = 'running'",
                    (now, reason, pass_id),
                )
                connection.execute(
                    "DELETE FROM agent_claims WHERE pass_id = ?", (pass_id,)
                )
                connection.execute(
                    "INSERT INTO agent_alerts(agent_id, pass_id, kind, message, "
                    "created_at) "
                    "VALUES (?, ?, 'stopped', ?, ?)",
                    (agent_id, pass_id, reason, now),
                )
            connection.execute(
                "UPDATE agents SET status = 'stopped', current_state = NULL, "
                "current_pass_id = NULL, last_error = NULL, updated_at = ? "
                "WHERE id = ?",
                (now, agent_id),
            )
            return True

        return self.database.transaction(stop_agent)

    def holders(self) -> dict[str, dict[str, Any]]:
        return self.database.transaction(
            lambda connection: {
                str(row["item_key"]): {
                    "id": str(row["agent_id"]),
                    "name": str(row["name"]),
                    "pass_id": str(row["pass_id"]),
                }
                for row in connection.execute(
                    "SELECT c.item_key, c.agent_id, c.pass_id, a.name "
                    "FROM agent_claims c JOIN agents a ON a.id = c.agent_id"
                ).fetchall()
            }
        )

    def log_path(self, checkout_path: str, pass_id: str, sequence: int) -> Path:
        path = Path(checkout_path).expanduser() / ".beehaiive" / "logs" / pass_id
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{sequence}.jsonl"

    def next_step_sequence(self, pass_id: str) -> int:
        return self.database.transaction(
            lambda connection: int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                    "FROM agent_steps WHERE pass_id = ?",
                    (pass_id,),
                ).fetchone()["sequence"]
            )
        )

    @staticmethod
    def _agent_dict(row: Any) -> dict[str, Any]:
        try:
            parameters = json.loads(str(row["parameters_json"]))
        except json.JSONDecodeError:
            parameters = {}
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "workflow_id": int(row["workflow_id"]),
            "workflow_revision": int(row["workflow_revision"]),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "repository": str(row["repository"]),
            "checkout_path": str(row["checkout_path"]),
            "model": str(row["model"]) if row["model"] is not None else None,
            "status": str(row["status"]),
            "current_state": row["current_state"],
            "current_pass_id": row["current_pass_id"],
            "last_error": row["last_error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _pass_dict(cls, connection: Any, pass_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM agent_passes WHERE id = ?", (pass_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            item = json.loads(str(row["item_json"]))
        except json.JSONDecodeError:
            item = {}
        return {
            "id": str(row["id"]),
            "agent_id": str(row["agent_id"]),
            "item_key": str(row["item_key"]),
            "item": item if isinstance(item, dict) else {},
            "workflow_id": int(row["workflow_id"]),
            "workflow_revision": int(row["workflow_revision"]),
            "current_state": str(row["current_state"]),
            "status": str(row["status"]),
            "step_count": int(row["step_count"]),
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
            "stalled_reason": row["stalled_reason"],
            "steps": [
                cls._step_dict(step)
                for step in connection.execute(
                    "SELECT * FROM agent_steps WHERE pass_id = ? ORDER BY sequence",
                    (pass_id,),
                ).fetchall()
            ],
        }

    @staticmethod
    def _step_dict(row: Any) -> dict[str, Any]:
        try:
            handover = json.loads(str(row["handover_json"]))
        except json.JSONDecodeError:
            handover = {}
        try:
            command = json.loads(str(row["command_json"]))
        except json.JSONDecodeError:
            command = []
        return {
            "sequence": int(row["sequence"]),
            "state_id": str(row["state_id"]),
            "action": str(row["action"]),
            "status": str(row["status"]),
            "outcome": row["outcome"],
            "summary": str(row["summary"]),
            "handover": handover if isinstance(handover, dict) else {},
            "command": command if isinstance(command, list) else [],
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
            "exit_code": row["exit_code"],
            "log_path": row["log_path"],
        }

    def _finish_pass(
        self,
        agent_id: str,
        pass_id: str,
        status: str,
        reason: str | None,
        *,
        release: bool,
    ) -> None:
        now = _now(self._clock)

        def finish(connection: Any) -> None:
            connection.execute(
                "UPDATE agent_passes SET status = ?, finished_at = ?, "
                "stalled_reason = ? WHERE id = ?",
                (status, now, reason, pass_id),
            )
            if release:
                connection.execute(
                    "DELETE FROM agent_claims WHERE pass_id = ?", (pass_id,)
                )
            connection.execute(
                "UPDATE agents SET status = 'waiting', current_state = NULL, "
                "current_pass_id = NULL, last_error = NULL, updated_at = ? "
                "WHERE id = ?",
                (now, agent_id),
            )

        self.database.transaction(finish)


class AgentService:
    def __init__(
        self,
        config: CoreConfig,
        database: SnapshotDatabase,
        workflow_store: WorkflowStore,
        project_service: ProjectSnapshotService,
        *,
        clock: Callable[[], datetime] | None = None,
        process_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.workflow_store = workflow_store
        self.project_service = project_service
        self.store = AgentStore(database, clock)
        self.store.recover_abandoned()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._process_factory = process_factory or subprocess.Popen
        self._threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._processes: dict[str, Any] = {}
        self._lock = threading.RLock()

    def list_agents(self) -> list[dict[str, Any]]:
        agents = self.store.list()
        return [
            detail
            for agent in agents
            if (detail := self.store.get(str(agent["id"]))) is not None
        ]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.store.get(agent_id)

    def create_agent(self, body: Mapping[str, Any]) -> dict[str, Any]:
        values = self._validated_assignment(body, existing=None)
        try:
            return self.store.create(**values)
        except Exception as error:
            if "UNIQUE constraint failed: agents.name" in str(error):
                raise AgentConflict("agent name already exists") from error
            if "agents_checkout_path_unique" in str(
                error
            ) or "agents.checkout_path" in str(error):
                raise AgentConflict("checkout path is already assigned") from error
            raise

    def update_agent(
        self, agent_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        existing = self.store.get(agent_id)
        if existing is None:
            return None
        merged = dict(existing)
        merged.update(body)
        values = self._validated_assignment(merged, existing=existing)
        update_values = {
            "name": values["name"],
            "parameters_json": _json(values["parameters"]),
            "repository": values["repository"],
            "checkout_path": values["checkout_path"],
            "model": values["model"],
            "workflow_id": values["workflow_id"],
            "workflow_revision": values["workflow_revision"],
        }
        try:
            return self.store.update(
                agent_id,
                update_values,
                workflow_id=values["workflow_id"],
                workflow_revision=values["workflow_revision"],
            )
        except Exception as error:
            if "UNIQUE constraint failed: agents.name" in str(error):
                raise AgentConflict("agent name already exists") from error
            if "agents_checkout_path_unique" in str(
                error
            ) or "agents.checkout_path" in str(error):
                raise AgentConflict("checkout path is already assigned") from error
            raise

    def delete_agent(self, agent_id: str) -> str:
        return self.store.delete(agent_id)

    def start_agent(self, agent_id: str) -> dict[str, Any] | None:
        agent = self.store.get(agent_id)
        if agent is None:
            return None
        self._validated_assignment(agent, existing=agent)
        if agent["status"] == "stalled":
            raise AgentConflict("reset a stalled agent before restarting it")
        with self._lock:
            thread = self._threads.get(agent_id)
            if thread is not None and thread.is_alive():
                raise AgentConflict("agent is already running")
            stop_event = threading.Event()
            self._stop_events[agent_id] = stop_event
            self.store.set_status(agent_id, "waiting")
            thread = threading.Thread(
                target=self._run_agent,
                args=(agent_id, stop_event),
                name=f"beehaive-agent-{agent_id[:8]}",
                daemon=True,
            )
            self._threads[agent_id] = thread
            thread.start()
        return self.store.get(agent_id)

    def stop_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.store.get(agent_id) is None:
            return None
        with self._lock:
            event = self._stop_events.get(agent_id)
            if event is not None:
                event.set()
            process = self._processes.get(agent_id)
            thread = self._threads.get(agent_id)
        if process is not None:
            _terminate_process(process)
        self.store.stop(agent_id)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        return self.store.get(agent_id)

    def reset_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.store.get(agent_id) is None:
            return None
        self.store.reset(agent_id)
        return self.store.get(agent_id)

    def shutdown(self) -> None:
        with self._lock:
            agent_ids = list(self._threads)
        for agent_id in agent_ids:
            self.stop_agent(agent_id)
        for agent_id in agent_ids:
            with self._lock:
                thread = self._threads.get(agent_id)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

    def logs(
        self,
        agent_id: str,
        *,
        pass_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any] | None:
        agent = self.store.get(agent_id)
        if agent is None:
            return None
        passes = cast(list[dict[str, Any]], agent["history"])
        selected = next(
            (value for value in passes if pass_id is None or value["id"] == pass_id),
            None,
        )
        if selected is None:
            return {"agent_id": agent_id, "pass_id": pass_id, "lines": []}
        lines: list[str] = []
        for step in cast(list[dict[str, Any]], selected["steps"]):
            path_value = step.get("log_path")
            if not path_value:
                continue
            try:
                lines.extend(
                    Path(str(path_value)).read_text(encoding="utf-8").splitlines()
                )
            except OSError:
                continue
        return {
            "agent_id": agent_id,
            "pass_id": selected["id"],
            "lines": lines[-max(1, min(limit, 500)) :],
        }

    def holders(self) -> dict[str, dict[str, Any]]:
        return self.store.holders()

    def _validated_assignment(
        self,
        body: Mapping[str, Any],
        *,
        existing: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not name:
            raise AgentError("name is required")
        workflow_id = _int_value(body.get("workflow_id", body.get("workflow")))
        if workflow_id is None and existing is not None:
            workflow_id = int(existing["workflow_id"])
        if workflow_id is None:
            raise AgentError("workflow_id is required")
        workflow = self.workflow_store.get_workflow(workflow_id)
        if workflow is None:
            raise AgentError("workflow not found")
        requested_revision = _int_value(
            body.get("workflow_revision", body.get("revision"))
        )
        if requested_revision is None and existing is not None:
            requested_revision = int(existing["workflow_revision"])
        latest = workflow.get("latest")
        if not isinstance(latest, dict):
            raise AgentError("workflow has no revision")
        latest_value = cast(dict[str, Any], latest)
        revision = requested_revision or int(latest_value["revision"])
        revision_values = cast(list[dict[str, Any]], workflow.get("revisions", []))
        revision_value = next(
            (
                value
                for value in revision_values
                if int(value.get("revision", -1)) == revision
            ),
            None,
        )
        if not isinstance(revision_value, dict):
            raise AgentError("workflow revision not found")
        definition = revision_value.get("definition")
        status_options = self._status_options()
        validation = validate_workflow(
            definition,
            skills_dirs=self.config.skills_dirs,
            status_options=status_options,
        )
        if not validation.valid:
            raise AgentError(
                "workflow revision is invalid: "
                + "; ".join(issue.message for issue in validation.errors)
            )
        parameters = _parameter_values(
            body.get("parameters", existing.get("parameters") if existing else {}),
        )
        resolved_parameters = _validate_parameters(
            validation.definition.get("parameters", []),
            parameters,
            self.config.skills_dirs,
        )
        repository = str(
            body.get("repository", existing.get("repository") if existing else "")
        ).strip()
        owner_name = repository.split("/")
        if (
            len(owner_name) != 2
            or not all(owner_name)
            or any(character.isspace() for character in repository)
        ):
            raise AgentError("repository must be an owner/name value")
        checkout_path = str(
            body.get("checkout_path", existing.get("checkout_path") if existing else "")
        ).strip()
        if not checkout_path:
            raise AgentError("checkout_path is required")
        checkout = Path(checkout_path).expanduser().resolve()
        if _is_operator_checkout(checkout):
            raise AgentError("operator checkout cannot be used by an agent")
        model_value = body.get("model", existing.get("model") if existing else None)
        model = str(model_value).strip() if model_value else None
        return {
            "name": name,
            "workflow_id": workflow_id,
            "workflow_revision": revision,
            "parameters": resolved_parameters,
            "repository": repository,
            "checkout_path": os.path.normcase(str(checkout)),
            "model": model,
        }

    def _status_options(self) -> tuple[str, ...]:
        try:
            snapshot = self.project_service.get_snapshot()
        except Exception:
            return ()
        return tuple(
            column.status for column in snapshot.columns if column.status != "No status"
        )

    def _run_agent(self, agent_id: str, stop_event: threading.Event) -> None:
        try:
            agent = self.store.get(agent_id)
            if agent is None:
                return
            self._ensure_checkout(agent)
            workflow = self.workflow_store.get_workflow(int(agent["workflow_id"]))
            if workflow is None:
                raise AgentError("assigned workflow no longer exists")
            revision_values = cast(list[dict[str, Any]], workflow.get("revisions", []))
            revision = next(
                (
                    value
                    for value in revision_values
                    if int(value.get("revision", -1)) == int(agent["workflow_revision"])
                ),
                None,
            )
            if not isinstance(revision, dict) or not isinstance(
                revision.get("definition"), dict
            ):
                raise AgentError("assigned workflow revision no longer exists")
            definition = cast(dict[str, Any], revision["definition"])
            while not stop_event.is_set():
                agent = self.store.get(agent_id)
                if agent is None:
                    return
                snapshot = self.project_service.get_snapshot()
                candidate = self._find_candidate(snapshot, definition, agent)
                if candidate is None:
                    self.store.set_status(agent_id, "waiting")
                    self._wait_for_next_cycle(stop_event)
                    continue
                card, column_status = candidate
                pass_id = self.store.claim(
                    agent_id,
                    card,
                    status=column_status,
                    workflow_id=int(agent["workflow_id"]),
                    workflow_revision=int(agent["workflow_revision"]),
                    initial_state=str(definition["initial"]),
                )
                if pass_id is None:
                    self.store.set_status(agent_id, "waiting")
                    self._wait_for_next_cycle(stop_event)
                    continue
                self._run_pass(
                    agent, definition, card, column_status, pass_id, stop_event
                )
                current = self.store.get(agent_id)
                if current is None or current["status"] in {"stopped", "stalled"}:
                    return
                self._wait_for_next_cycle(stop_event)
        except AgentExecutionError as error:
            if stop_event.is_set():
                return
            current = self.store.get(agent_id)
            if current is not None and current.get("current_pass_id"):
                self.store.stall_pass(
                    agent_id,
                    str(current["current_pass_id"]),
                    str(error),
                    auto_reset=False,
                )
            else:
                self.store.set_status(agent_id, "stalled", last_error=str(error))
        except Exception as error:
            if stop_event.is_set():
                return
            current = self.store.get(agent_id)
            if current is not None and current.get("current_pass_id"):
                self.store.stall_pass(
                    agent_id,
                    str(current["current_pass_id"]),
                    str(error),
                    auto_reset=False,
                )
            elif current is not None:
                self.store.set_status(agent_id, "stalled", last_error=str(error))
        finally:
            with self._lock:
                self._threads.pop(agent_id, None)
                self._stop_events.pop(agent_id, None)
                self._processes.pop(agent_id, None)

    def _wait_for_next_cycle(self, stop_event: threading.Event) -> None:
        interval = min(max(self.config.refresh_seconds, 0.05), 60.0)
        stop_event.wait(interval)

    def _run_pass(
        self,
        agent: Mapping[str, Any],
        definition: Mapping[str, Any],
        card: ProjectCard,
        column_status: str,
        pass_id: str,
        stop_event: threading.Event,
    ) -> None:
        agent_id = str(agent["id"])
        states: dict[str, dict[str, Any]] = {}
        for raw_state in cast(list[Any], definition.get("states", [])):
            if isinstance(raw_state, dict):
                state = cast(dict[str, Any], raw_state)
                if state.get("id"):
                    states[str(state["id"])] = state
        transitions: list[dict[str, Any]] = []
        for raw_transition in cast(list[Any], definition.get("transitions", [])):
            if isinstance(raw_transition, dict):
                transitions.append(cast(dict[str, Any], raw_transition))
        current = str(definition["initial"])
        outcome: str | None = None
        visit_counts: dict[str, int] = {}
        parameter_values = cast(dict[str, Any], agent["parameters"])
        max_steps = int(definition.get("max_steps_per_pass", 1))
        auto_reset = bool(definition.get("auto_reset_on_stall", False))
        recent_steps: list[dict[str, Any]] = []
        for _ in range(max_steps):
            if stop_event.is_set():
                raise AgentExecutionError("agent stopped while pass was running")
            if current == str(definition["initial"]) and outcome is not None:
                self.store.complete_pass(agent_id, pass_id)
                return
            state = states.get(current)
            if state is None:
                self.store.stall_pass(
                    agent_id,
                    pass_id,
                    f"state {current!r} is missing",
                    auto_reset=auto_reset,
                )
                return
            visit_counts[current] = visit_counts.get(current, 0) + 1
            if visit_counts[current] > int(state.get("max_visits", 1)):
                self.store.stall_pass(
                    agent_id,
                    pass_id,
                    f"state {current!r} exceeded its visit cap",
                    auto_reset=auto_reset,
                )
                return
            action = str(state.get("action"))
            sequence = self.store.next_step_sequence(pass_id)
            log_path = self.store.log_path(
                str(agent["checkout_path"]), pass_id, sequence
            )
            command = (
                self._skill_command(agent, state, log_path)
                if action == "run_skill"
                else []
            )
            inserted_sequence = self.store.begin_step(
                pass_id, current, action, command, str(log_path)
            )
            if inserted_sequence != sequence:
                raise AgentExecutionError("step sequence changed while starting step")
            try:
                exit_code: int | None = None
                if action == "wait_for_work":
                    step_outcome = "item_claimed"
                    summary = "held Project item selected in deterministic order"
                    handover: dict[str, Any] = {}
                elif action == "run_skill":
                    result = self._run_skill(
                        agent,
                        state,
                        card,
                        column_status,
                        parameter_values,
                        log_path,
                        stop_event,
                        recent_steps,
                        command,
                    )
                    step_outcome = result["outcome"]
                    summary = result["summary"]
                    handover = result["handover"]
                    exit_code = result["exit_code"]
                    try:
                        refreshed_card, refreshed_status = (
                            self.project_service.fetch_held_item(item_key(card))
                        )
                    except Exception as error:
                        raise AgentExecutionError(
                            f"held item reread failed: {error}"
                        ) from error
                    if item_key(refreshed_card) != item_key(card):
                        raise AgentExecutionError(
                            "held item reread returned another item"
                        )
                    card = refreshed_card
                    column_status = refreshed_status
                elif action == "escalate":
                    step_outcome = "escalated"
                    summary = (
                        str(recent_steps[-1]["summary"])
                        if recent_steps
                        else "operator attention required"
                    )
                    self.store.record_alert(
                        agent_id, pass_id, sequence, "escalated", summary
                    )
                    handover = {"reason": summary}
                else:
                    raise AgentExecutionError(f"unsupported state action {action!r}")
                self.store.finish_step(
                    pass_id,
                    sequence,
                    status="completed",
                    outcome=step_outcome,
                    summary=summary,
                    handover=handover,
                    exit_code=exit_code,
                )
                recent_steps.append(
                    {
                        "state": current,
                        "outcome": step_outcome,
                        "summary": summary,
                        "handover": handover,
                    }
                )
                del recent_steps[:-5]
                next_state = _next_state(
                    transitions,
                    current,
                    card,
                    column_status,
                    step_outcome,
                    parameter_values,
                )
                if next_state is None:
                    self.store.stall_pass(
                        agent_id,
                        pass_id,
                        "no transition matched state "
                        f"{current!r} outcome {step_outcome!r}",
                        auto_reset=auto_reset,
                        sequence=sequence,
                    )
                    return
                current = next_state
                outcome = step_outcome
            except AgentExecutionError as error:
                if stop_event.is_set():
                    return
                self.store.finish_step(
                    pass_id,
                    sequence,
                    status="stalled",
                    summary=str(error),
                    exit_code=error.exit_code,
                )
                self.store.stall_pass(
                    agent_id,
                    pass_id,
                    str(error),
                    auto_reset=auto_reset,
                    sequence=sequence,
                )
                return
        self.store.stall_pass(
            agent_id,
            pass_id,
            "pass exceeded max_steps_per_pass",
            auto_reset=auto_reset,
        )

    def _run_skill(
        self,
        agent: Mapping[str, Any],
        state: Mapping[str, Any],
        card: ProjectCard,
        column_status: str,
        parameters: Mapping[str, Any],
        log_path: Path,
        stop_event: threading.Event,
        recent_steps: Sequence[Mapping[str, Any]],
        command: Sequence[str],
    ) -> dict[str, Any]:
        outcome_schema = log_path.with_name("outcome.schema.json")
        result_path = log_path.with_name("result.json")
        outcome_schema.write_text(
            _json(
                {
                    "type": "object",
                    "required": ["outcome", "summary", "handover"],
                    "properties": {
                        "outcome": {
                            "type": "string",
                            "enum": list(state.get("outcomes", [])),
                        },
                        "summary": {"type": "string"},
                        "handover": {"type": "object"},
                    },
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        prompt = _skill_prompt(
            state,
            card,
            column_status,
            parameters,
            skills_dirs=self.config.skills_dirs,
            recent_steps=recent_steps,
        )
        _append_log(log_path, {"event": "start", "command": command})
        try:
            process = self._start_process(command, str(agent["checkout_path"]))
        except OSError as error:
            raise AgentExecutionError(f"codex exec could not start: {error}") from error
        with self._lock:
            self._processes[str(agent["id"])] = process
        try:
            try:
                return_code = self._communicate_process(
                    process,
                    prompt,
                    log_path,
                    timeout=self.config.agent_step_timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                _terminate_process(process)
                raise AgentExecutionError(
                    "codex exec timed out after "
                    f"{self.config.agent_step_timeout_seconds:g} seconds",
                    exit_code=getattr(process, "returncode", None),
                ) from error
            except OSError as error:
                raise AgentExecutionError(
                    f"codex exec failed: {error}",
                    exit_code=getattr(process, "returncode", None),
                ) from error
        finally:
            with self._lock:
                self._processes.pop(str(agent["id"]), None)
        _append_log(log_path, {"event": "finish", "returncode": return_code})
        if stop_event.is_set():
            raise AgentExecutionError(
                "agent stopped while codex exec was running", exit_code=return_code
            )
        if return_code != 0:
            raise AgentExecutionError(
                f"codex exec exited with status {return_code}",
                exit_code=return_code,
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AgentExecutionError(
                "codex exec produced no valid structured result",
                exit_code=return_code,
            ) from error
        if not isinstance(payload, dict):
            raise AgentExecutionError(
                "codex exec result must be an object", exit_code=return_code
            )
        allowed = state.get("outcomes", [])
        result = cast(dict[str, Any], payload)
        outcome = result.get("outcome")
        summary = result.get("summary")
        handover = result.get("handover")
        if not isinstance(outcome, str) or outcome not in allowed:
            raise AgentExecutionError(
                "codex exec returned an undeclared outcome", exit_code=return_code
            )
        if not isinstance(summary, str) or not isinstance(handover, dict):
            raise AgentExecutionError(
                "codex exec result has invalid summary or handover",
                exit_code=return_code,
            )
        return {
            "outcome": outcome,
            "summary": summary,
            "handover": handover,
            "exit_code": return_code,
        }

    def _communicate_process(
        self,
        process: Any,
        prompt: str,
        log_path: Path,
        *,
        timeout: float,
    ) -> int:
        stdout = getattr(process, "stdout", None)
        stderr = getattr(process, "stderr", None)
        wait = getattr(process, "wait", None)
        if stdout is None or stderr is None or not callable(wait):
            output, error_output = process.communicate(prompt, timeout=timeout)
            _append_stream_text(log_path, "stdout", output)
            _append_stream_text(log_path, "stderr", error_output)
            return int(process.returncode)
        readers = [
            threading.Thread(
                target=_stream_log,
                args=(stdout, log_path, "stdout"),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_log,
                args=(stderr, log_path, "stderr"),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        stdin = getattr(process, "stdin", None)
        if stdin is None:
            raise OSError("codex exec stdin is unavailable")
        stdin.write(prompt)
        stdin.close()
        try:
            result = wait(timeout=timeout)
        finally:
            for reader in readers:
                reader.join(timeout=1)
        return_code = (
            result if result is not None else getattr(process, "returncode", None)
        )
        if not isinstance(return_code, int):
            raise OSError("codex exec exited without a return code")
        return return_code

    def _skill_command(
        self, agent: Mapping[str, Any], state: Mapping[str, Any], log_path: Path
    ) -> list[str]:
        del state
        outcome_schema = log_path.with_name("outcome.schema.json")
        result_path = log_path.with_name("result.json")
        command = [self.config.codex, "exec"]
        command.extend(
            self.config.codex_args or ("--sandbox", "workspace-write", "--ephemeral")
        )
        command.extend(
            ("--output-schema", str(outcome_schema), "-o", str(result_path), "--json")
        )
        if agent.get("model"):
            command.extend(("--model", str(agent["model"])))
        command.append("-")
        return command

    def _start_process(self, command: Sequence[str], checkout: str) -> Any:
        kwargs: dict[str, Any] = {
            "cwd": checkout,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        factory = cast(Callable[..., Any], self._process_factory)
        try:
            return factory(list(command), **kwargs)
        except TypeError:
            kwargs.pop("creationflags", None)
            kwargs.pop("start_new_session", None)
            return factory(list(command), **kwargs)

    def _ensure_checkout(self, agent: Mapping[str, Any]) -> None:
        path = Path(str(agent["checkout_path"])).expanduser().resolve()
        if _is_operator_checkout(path):
            raise AgentError("operator checkout cannot be used by an agent")
        if path.exists():
            if not path.is_dir():
                raise AgentError("agent checkout path is not a directory")
            remote_result = subprocess.run(
                ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                check=False,
                capture_output=True,
                text=True,
            )
            remote_repository = _remote_repository(remote_result.stdout)
            if remote_result.returncode != 0 or remote_repository is None:
                raise AgentError("agent checkout path is not a Git clone")
            if remote_repository.casefold() != str(agent["repository"]).casefold():
                raise AgentError("agent checkout repository does not match assignment")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                f"https://github.com/{agent['repository']}.git",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _find_candidate(
        self,
        snapshot: ProjectSnapshot,
        definition: Mapping[str, Any],
        agent: Mapping[str, Any],
    ) -> tuple[ProjectCard, str] | None:
        initial = str(definition["initial"])
        states: dict[str, dict[str, Any]] = {}
        for raw_state in cast(list[Any], definition.get("states", [])):
            if isinstance(raw_state, dict):
                state = cast(dict[str, Any], raw_state)
                states[str(state["id"])] = state
        initial_state = states.get(initial)
        if not initial_state:
            raise AgentError("workflow initial state is missing")
        if initial_state.get("action") != "wait_for_work":
            raise AgentError("workflow initial state must wait for work")
        parameters = cast(dict[str, Any], agent["parameters"])
        for column in snapshot.columns:
            for card in column.items:
                if agent["repository"] and card.repository != agent["repository"]:
                    continue
                if (
                    _transition_matches_item(
                        _outgoing(definition, initial), card, column.status, parameters
                    )
                    and item_key(card) not in self.store.holders()
                ):
                    return card, column.status
        return None

    @staticmethod
    def _held_item_exists(snapshot: ProjectSnapshot, card: ProjectCard) -> bool:
        key = item_key(card)
        return any(
            item_key(candidate) == key
            for column in snapshot.columns
            for candidate in column.items
        )


def _is_operator_checkout(path: Path) -> bool:
    operator_root = Path.cwd().resolve()
    try:
        path.relative_to(operator_root)
    except ValueError:
        return False
    return True


def _remote_repository(value: str) -> str | None:
    remote = value.strip()
    if remote.startswith("git@github.com:"):
        path = remote.split(":", 1)[1]
    else:
        parsed = urlsplit(remote)
        if parsed.hostname is None or parsed.hostname.casefold() != "github.com":
            return None
        path = parsed.path
    return path.strip("/").removesuffix(".git") or None


def _parameter_values(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {str(key): item for key, item in mapping.items()}
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for raw_item in cast(list[Any], value):
            if isinstance(raw_item, Mapping):
                item = cast(Mapping[str, Any], raw_item)
                if "name" in item:
                    result[str(item["name"])] = item.get("value")
        return result
    if value is None:
        return {}
    raise AgentError("parameters must be an object or named value list")


def _validate_parameters(
    definitions: Any,
    supplied: Mapping[str, Any],
    skills_dirs: Sequence[Path],
) -> dict[str, Any]:
    if not isinstance(definitions, list):
        raise AgentError("workflow parameters are invalid")
    known: set[str] = set()
    values: dict[str, Any] = {}
    for raw in cast(list[Any], definitions):
        if not isinstance(raw, Mapping):
            continue
        parameter = cast(Mapping[str, Any], raw)
        name = str(parameter.get("name", ""))
        kind = str(parameter.get("type", ""))
        known.add(name)
        if parameter.get("const") is True:
            value = parameter.get("value")
            if name in supplied and supplied[name] != value:
                raise AgentError(f"constant parameter {name!r} cannot be changed")
        elif name not in supplied:
            raise AgentError(f"parameter {name!r} is required")
        else:
            value = supplied[name]
        if not _parameter_type_matches(value, kind):
            raise AgentError(f"parameter {name!r} does not match type {kind!r}")
        if kind == "skill" and not _skill_exists(str(value), skills_dirs):
            raise AgentError(
                f"skill {value!r} does not resolve in configured skill roots"
            )
        values[name] = value
    extras = set(supplied) - known
    if extras:
        raise AgentError(f"unknown parameters: {', '.join(sorted(extras))}")
    return values


def _parameter_type_matches(value: Any, kind: str) -> bool:
    if kind in {"status", "label", "skill", "repository", "text"}:
        return isinstance(value, str) and bool(value)
    if kind == "item_type":
        return value in {"issue", "pull_request"}
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return False


def _skill_exists(name: str, skills_dirs: Sequence[Path]) -> bool:
    if not name or Path(name).name != name or name in {".", ".."}:
        return False
    for directory in skills_dirs:
        try:
            root = directory.expanduser().resolve()
            candidate = (root / name / "SKILL.md").resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return True
    return False


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _outgoing(definition: Mapping[str, Any], state_id: str) -> list[Mapping[str, Any]]:
    transitions: list[Mapping[str, Any]] = []
    for raw_transition in cast(list[Any], definition.get("transitions", [])):
        if isinstance(raw_transition, Mapping):
            transition = cast(Mapping[str, Any], raw_transition)
            if str(transition.get("from")) == state_id:
                transitions.append(transition)
    return [
        value[1]
        for value in sorted(
            enumerate(transitions),
            key=lambda value: (int(value[1].get("priority", 0)), value[0]),
        )
    ]


def _next_state(
    transitions: Sequence[Mapping[str, Any]],
    state_id: str,
    card: ProjectCard,
    column_status: str,
    outcome: str,
    parameters: Mapping[str, Any],
) -> str | None:
    candidates = sorted(
        [
            (index, transition)
            for index, transition in enumerate(transitions)
            if str(transition.get("from")) == state_id
        ],
        key=lambda value: (int(value[1].get("priority", 0)), value[0]),
    )
    for _, transition in candidates:
        conditions = transition.get("conditions", [])
        if not isinstance(conditions, list):
            continue
        typed_conditions = cast(list[Any], conditions)
        if all(
            isinstance(condition, Mapping) for condition in typed_conditions
        ) and all(
            _condition_matches(
                cast(Mapping[str, Any], condition),
                card,
                column_status,
                outcome,
                parameters,
            )
            for condition in typed_conditions
        ):
            return str(transition.get("to"))
    return None


def _transition_matches_item(
    transitions: Sequence[Mapping[str, Any]],
    card: ProjectCard,
    column_status: str,
    parameters: Mapping[str, Any],
) -> bool:
    for transition in transitions:
        conditions = transition.get("conditions", [])
        if not isinstance(conditions, list):
            continue
        typed_conditions = cast(list[Any], conditions)
        if typed_conditions and all(
            isinstance(condition, Mapping)
            and _condition_matches(
                cast(Mapping[str, Any], condition),
                card,
                column_status,
                None,
                parameters,
            )
            for condition in typed_conditions
        ):
            return True
    return False


def _condition_matches(
    condition: Mapping[str, Any],
    card: ProjectCard,
    column_status: str,
    outcome: str | None,
    parameters: Mapping[str, Any],
) -> bool:
    kind = str(condition.get("kind", ""))
    value = _resolve(condition.get("value"), parameters)
    if kind == "always":
        return True
    if kind == "outcome_is":
        return outcome is not None and outcome == value
    if kind == "item_status_is":
        return column_status == value
    if kind == "item_type_is":
        return card.type.casefold() == str(value).replace("_", "").casefold()
    if kind == "item_has_label":
        return str(value) in card.labels
    if kind == "item_lacks_label":
        return str(value) not in card.labels
    if kind == "item_repository_is":
        return card.repository == value
    return False


def _resolve(value: Any, parameters: Mapping[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) >= 3 and value.startswith("{") and value.endswith("}"):
        return parameters.get(value[1:-1], value)
    result = value
    for name, parameter in parameters.items():
        result = result.replace("{" + name + "}", str(parameter))
    return result


def _skill_prompt(
    state: Mapping[str, Any],
    card: ProjectCard,
    column_status: str,
    parameters: Mapping[str, Any],
    *,
    skills_dirs: Sequence[Path] = (),
    recent_steps: Sequence[Mapping[str, Any]] = (),
) -> str:
    prompt = _resolve(state.get("prompt", ""), parameters)
    skill = _resolve(state.get("skill", ""), parameters)
    facts = {
        "item": {"status": column_status, **card.as_dict()},
        "skill": skill,
        "parameters": dict(parameters),
        "unattended": True,
        "allowed_outcomes": list(state.get("outcomes", [])),
        "recent_handovers": [dict(step) for step in recent_steps[-5:]],
        "authority": (
            "Only the checked-out repository and the held Project item are in "
            "scope. Work unattended and never wait for input. The core remains "
            "read-only, while this child skill may use its authorized gh/git "
            "workflow on the held item and repository. Return blocked only when "
            "authority, credentials, or a destructive choice is missing."
        ),
    }
    facts_json = json.dumps(facts, sort_keys=True)
    skill_path = _skill_path(str(skill), skills_dirs)
    return (
        f"{prompt}\n\nRead and follow the resolved skill at "
        f"{skill_path}.\nResolved skill: {skill}\nFacts: {facts_json}"
    )


def _append_log(path: Path, event: Mapping[str, Any]) -> None:
    with _LOG_LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(_json(dict(event)) + "\n")


def _append_stream_text(path: Path, event: str, value: Any) -> None:
    text = "" if value is None else str(value)
    for line in text.splitlines() or ([text] if text else []):
        _append_log(path, {"event": event, "line": line})


def _stream_log(stream: Any, path: Path, event: str) -> None:
    for line in iter(stream.readline, ""):
        _append_log(path, {"event": event, "line": line.rstrip("\r\n")})


def _skill_path(name: str, skills_dirs: Sequence[Path]) -> str:
    for directory in skills_dirs:
        try:
            root = directory.expanduser().resolve()
            candidate = (root / name / "SKILL.md").resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return str(candidate)
    return f"<unresolved-skill>/{name}/SKILL.md"


def _terminate_process(process: Any) -> None:
    pid = getattr(process, "pid", None)
    try:
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        elif pid:
            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            if callable(getpgid) and callable(killpg):
                killpg(getpgid(int(pid)), signal.SIGTERM)
            else:
                process.terminate()
        else:
            process.terminate()
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):
            process.kill()
