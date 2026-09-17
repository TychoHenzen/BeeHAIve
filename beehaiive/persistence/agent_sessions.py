from __future__ import annotations

import sqlite3
from typing import Any

from beehaiive.contract_types.validation import _redact_text
from beehaiive.models import RunState, RunStatus
from beehaiive.session_evidence import (
    CAPTURE_GAPS,
    allowed_session_event,
    transcript_projection,
)

from .constants import MAX_AGENT_SESSION_EVENT_LENGTH as MAX_AGENT_SESSION_EVENT_LENGTH
from .constants import MAX_AGENT_SESSION_EVENTS as MAX_AGENT_SESSION_EVENTS
from .constants import MAX_AGENT_SESSION_TEXT_BYTES as MAX_AGENT_SESSION_TEXT_BYTES
from .constants import MAX_META_REVIEW_RECORDS as MAX_META_REVIEW_RECORDS
from .errors import StoreError
from .helpers.lease_helpers import _now as _now
from .helpers.value_helpers import _bounded_event_details as _bounded_event_details
from .helpers.value_helpers import _json_mapping_or_none as _json_mapping_or_none


class AgentSessionsMixin:
    def completed_session_records(
        self: Any, project_id: str, since: str | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        """Return bounded, durable completion records for meta-review."""

        if not project_id.strip():
            raise StoreError("A project id is required")
        if not 1 <= limit <= MAX_META_REVIEW_RECORDS:
            raise StoreError(
                f"record_limit must be between 1 and {MAX_META_REVIEW_RECORDS}"
            )
        with self._lock:
            query = """
                SELECT r.run_id, r.project_id, r.repository_name, r.pbi_number,
                       r.status, r.attempt, r.last_result, r.last_error,
                       r.updated_at, p.title
                FROM runs AS r
                JOIN pbis AS p
                  ON p.project_id = r.project_id
                 AND p.repository_name = r.repository_name
                 AND p.number = r.pbi_number
                WHERE r.project_id = ? AND r.status = 'completed'
            """
            parameters: list[object] = [project_id]
            if since is not None:
                query += " AND r.updated_at >= ?"
                parameters.append(since)
            query += " ORDER BY r.updated_at DESC, r.run_id LIMIT ?"
            parameters.append(limit)
            rows = self._connection.execute(query, parameters).fetchall()
            records: list[dict[str, object]] = []
            for row in rows:
                event_rows = self._connection.execute(
                    """
                    SELECT event_id, event_type, from_stage, to_stage,
                           details_json, created_at
                    FROM events
                    WHERE run_id = ?
                    ORDER BY event_id DESC
                    LIMIT 20
                    """,
                    (row["run_id"],),
                ).fetchall()
                records.append(
                    {
                        "source_id": f"run:{row['run_id']}",
                        "run_id": row["run_id"],
                        "project_id": row["project_id"],
                        "repository": row["repository_name"],
                        "pbi_number": row["pbi_number"],
                        "title": row["title"],
                        "status": row["status"],
                        "attempt": row["attempt"],
                        "result": row["last_result"],
                        "error": row["last_error"],
                        "updated_at": row["updated_at"],
                        "transcript": self._transcript_for_run(row["run_id"]),
                        "events": [
                            {
                                "id": event["event_id"],
                                "type": event["event_type"],
                                "from_stage": event["from_stage"],
                                "to_stage": event["to_stage"],
                                "details": _bounded_event_details(
                                    event["details_json"]
                                ),
                                "created_at": event["created_at"],
                            }
                            for event in reversed(event_rows)
                        ],
                    }
                )
            return tuple(records)

    def _transcript_for_run(self: Any, run_id: str) -> dict[str, object]:
        session = self._connection.execute(
            "SELECT capture_flags FROM agent_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if session is None:
            return transcript_projection(run_id, None)
        events = self._connection.execute(
            """SELECT sequence, kind, source_type, role, text, timestamp
               FROM agent_session_events WHERE run_id = ?
               ORDER BY sequence DESC LIMIT ?""",
            (run_id, MAX_AGENT_SESSION_EVENTS),
        ).fetchall()
        flags = session["capture_flags"]
        gaps = [name for name, flag in CAPTURE_GAPS.items() if flags & flag]
        return transcript_projection(run_id, [dict(event) for event in events], gaps)

    @staticmethod
    def _upsert_agent_session(
        connection: sqlite3.Connection, run_id: str, worker_id: str, task: str
    ) -> None:
        now = _now()
        connection.execute(
            """
            INSERT INTO agent_sessions(
                run_id, worker_id, task, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                worker_id = excluded.worker_id,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                worker_id.strip(),
                task,
                now,
                now,
            ),
        )

    def start_agent_session(
        self: Any, run_id: str, worker_id: str, task: str, lease_token: str
    ) -> None:
        if not worker_id.strip() or not task.strip():
            raise StoreError("An agent worker and task are required")
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None or run.status is not RunStatus.ACTIVE:
                raise StoreError("An active run is required for an agent session")
            self._require_lease(run, lease_token)
            self._upsert_agent_session(connection, run_id, worker_id, task)

    def record_agent_session_event(
        self: Any,
        run_id: str,
        lease_token: str,
        kind: str,
        source_type: str,
        role: str | None,
        text: str,
    ) -> None:
        is_gap = kind == "gap" and source_type in CAPTURE_GAPS
        if not is_gap and not allowed_session_event(kind, source_type, role):
            raise StoreError("An agent event kind and source type are required")
        safe_text = _redact_text(text, len(text)) if kind == "message" else source_type
        event_text = safe_text[:MAX_AGENT_SESSION_EVENT_LENGTH]
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None or run.status is not RunStatus.ACTIVE:
                raise StoreError("An active run is required for an agent event")
            self._require_lease(run, lease_token)
            if (
                connection.execute(
                    "SELECT 1 FROM agent_sessions WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise StoreError("Agent session has not been started")
            if is_gap:
                self._mark_capture_gap(connection, run_id, source_type)
                return
            if len(safe_text) > MAX_AGENT_SESSION_EVENT_LENGTH:
                self._mark_capture_gap(connection, run_id, "truncated")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM agent_session_events WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO agent_session_events(
                    run_id, sequence, kind, source_type, role, text, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    kind,
                    source_type.strip()[:120],
                    role.strip()[:20] if role and role.strip() else None,
                    event_text,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE agent_sessions SET updated_at = ? WHERE run_id = ?",
                (timestamp, run_id),
            )
            history = connection.execute(
                """
                SELECT sequence, length(CAST(text AS BLOB)) AS text_bytes
                FROM agent_session_events WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
            total_bytes = sum(int(row["text_bytes"]) for row in history)
            while (
                len(history) > MAX_AGENT_SESSION_EVENTS
                or total_bytes > MAX_AGENT_SESSION_TEXT_BYTES
            ):
                oldest = history.pop(0)
                self._mark_capture_gap(connection, run_id, "trimmed")
                total_bytes -= int(oldest["text_bytes"])
                connection.execute(
                    """
                    DELETE FROM agent_session_events
                    WHERE run_id = ? AND sequence = ?
                    """,
                    (run_id, oldest["sequence"]),
                )

    @staticmethod
    def _mark_capture_gap(
        connection: sqlite3.Connection, run_id: str, reason: str
    ) -> None:
        connection.execute(
            "UPDATE agent_sessions SET capture_flags = capture_flags | ? "
            "WHERE run_id = ?",
            (CAPTURE_GAPS[reason], run_id),
        )

    def get_agent_session(self: Any, run_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._agent_session_for_run(self._connection, run_id)

    def active_agent_sessions(self: Any) -> tuple[RunState, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.run_id FROM agent_sessions AS s
                JOIN runs AS r ON r.run_id = s.run_id
                WHERE r.status = 'active' ORDER BY s.created_at
                """
            ).fetchall()
            return tuple(
                run
                for row in rows
                if (run := self._run_for_id(self._connection, str(row["run_id"])))
                is not None
            )

    def _agent_session_for_run(
        self: Any, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT s.*, r.status, r.attempt, r.lease_expires_at,
                   r.task_contract_json, r.task_result_json,
                   p.branch, p.pull_request_url
            FROM agent_sessions AS s
            JOIN runs AS r ON r.run_id = s.run_id
            JOIN pbis AS p
              ON p.project_id = r.project_id
             AND p.repository_name = r.repository_name
             AND p.number = r.pbi_number
            WHERE s.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        events = connection.execute(
            """
            SELECT sequence, kind, source_type, role, text, timestamp
            FROM agent_session_events WHERE run_id = ? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return {
            "session_id": str(row["run_id"]),
            "worker_id": str(row["worker_id"]),
            "task": str(row["task"]),
            "state": str(row["status"]),
            "attempt": int(row["attempt"]),
            "lease_expires_at": row["lease_expires_at"],
            "branch": row["branch"],
            "pull_request_url": row["pull_request_url"],
            "task_contract": _json_mapping_or_none(row["task_contract_json"]),
            "task_result": _json_mapping_or_none(row["task_result_json"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "events": [
                {
                    "sequence": int(event["sequence"]),
                    "kind": str(event["kind"]),
                    "source_type": str(event["source_type"]),
                    "role": event["role"],
                    "text": str(event["text"]),
                    "timestamp": str(event["timestamp"]),
                }
                for event in events
            ],
        }


__all__ = ["AgentSessionsMixin"]
