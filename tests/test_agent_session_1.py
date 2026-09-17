import json
from pathlib import Path

import pytest

from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.agent.helpers import agent_snapshot as agent_snapshot


def test_session_event_parser_ignores_non_message_payloads(tmp_path: Path) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    events: list[tuple[str, str, str | None, str]] = []

    def record(kind: str, source: str, role: str | None, text: str) -> None:
        events.append((kind, source, role, text))

    executor._record_session_line("not json", record)
    assert executor._session_event("[]") is None
    assert executor._session_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "reasoning", "text": "private reasoning"},
            }
        )
    ) == ("progress", "item.completed", None, "item.completed")
    assert (
        executor._session_event(json.dumps({"type": "custom.event", "text": "ignored"}))
        is None
    )
    executor._record_session_line(
        json.dumps({"type": "agent_message", "text": "top-level message"}), record
    )

    assert events == [
        ("gap", "malformed", None, ""),
        ("message", "agent_message", "assistant", "top-level message"),
    ]


def test_agent_session_round_trips_through_project_state(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = OrchestratorStore(database)
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    store.start_agent_session(
        run.run_id, "agent-process-1", "bounded repository inventory", lease_token
    )
    store.record_agent_session_event(
        run.run_id, lease_token, "progress", "turn.started", None, "turn started"
    )
    store.record_agent_session_event(
        run.run_id,
        lease_token,
        "message",
        "item.completed",
        "assistant",
        "Repository inventory is ready.",
    )

    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert session["session_id"] == run.run_id
    assert session["worker_id"] == "agent-process-1"
    assert session["task"] == "bounded repository inventory"
    assert session["state"] == "active"
    assert [event["sequence"] for event in session["events"]] == [1, 2]

    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert pbi["agent_session"] == session
    store.close()

    reopened = OrchestratorStore(database)
    persisted = reopened.get_agent_session(run.run_id)
    assert persisted == session
    reopened.close()


def test_agent_session_rejects_invalid_start_and_event_requests(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    with pytest.raises(StoreError, match="worker and task are required"):
        store.start_agent_session(run.run_id, "", "task", lease_token)
    with pytest.raises(StoreError, match="active run is required"):
        store.start_agent_session("missing", "worker", "task", lease_token)
    with pytest.raises(StoreError, match="event kind and source type are required"):
        store.record_agent_session_event(
            run.run_id, lease_token, "tool", "item.completed", None, "ignored"
        )
    with pytest.raises(StoreError, match="active run is required"):
        store.record_agent_session_event(
            "missing", lease_token, "progress", "turn.started", None, "ignored"
        )
    with pytest.raises(StoreError, match="session has not been started"):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, "ignored"
        )
    store.close()


def test_agent_session_events_are_bounded_and_lease_fenced(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    store.start_agent_session(run.run_id, "worker-1", "bounded task", lease_token)

    for index in range(110):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, str(index)
        )
    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert [event["sequence"] for event in session["events"]] == list(range(11, 111))

    for _ in range(10):
        store.record_agent_session_event(
            run.run_id,
            lease_token,
            "message",
            "agent_message",
            "assistant",
            "é" * 4_000,
        )
    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert [event["sequence"] for event in session["events"]] == list(range(113, 121))
    total_text_bytes = sum(
        len(str(event["text"]).encode("utf-8")) for event in session["events"]
    )
    assert total_text_bytes == 64_000
    assert all(len(str(event["text"])) <= 4_000 for event in session["events"])

    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )
    reclaimed = service.claim("project-1", "owner/api", "worker-2")
    assert reclaimed is not None and reclaimed.run_id == run.run_id
    with pytest.raises(StoreError, match="Invalid or missing run lease token"):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, "stale"
        )
    store.close()
