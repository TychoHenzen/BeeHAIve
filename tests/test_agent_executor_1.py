import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    ModelTier,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.agent.helpers import agent_snapshot as agent_snapshot
from tests.support.agent.script_executor import ScriptExecutor as ScriptExecutor


def test_codex_executor_parses_final_message_without_passing_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "operator-secret")
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'agent_message', 'text': 'inventory complete; token present='"
        "+ str('GITHUB_TOKEN' in os.environ)}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {"
        "'input_tokens': 12, 'output_tokens': 7}}))\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script)
    router = ModelRouter(RoutingStore())
    decision = router.begin("run-1").decision

    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        decision,
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.result == "inventory complete; token present=False"
    assert result.input_tokens == 12
    assert result.output_tokens == 7


def test_codex_executor_streams_redacted_session_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "abc-secret")
    script = tmp_path / "session_runner.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'type': 'thread.started'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'agent_message', 'text': 'Bearer abc-secret'}}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'command_execution', 'command': 'password=hidden'}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script)
    events: list[tuple[str, str, str | None, str]] = []
    executor.set_session_event_handler("run-1", lambda *event: events.append(event))
    router = ModelRouter(RoutingStore())
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA), router.begin("run-1").decision
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert events == [
        ("progress", "thread.started", None, "thread.started"),
        ("message", "item.completed", "assistant", "Bearer [redacted]"),
        ("progress", "item.completed", None, "item.completed"),
        ("progress", "turn.completed", None, "turn.completed"),
    ]
    executor.set_session_event_handler("run-1", None)
    assert "run-1" not in executor._session_event_handlers


def test_bounded_communicator_skips_oversized_lines_and_flushes_final_line() -> None:
    payload = json.dumps({"type": "turn.started"})
    script = (
        "import sys\n"
        "sys.stdout.write('x' * 64001 + '\\n')\n"
        "sys.stdout.write('y' * 100000 + '\\n')\n"
        f"sys.stdout.write({payload!r})\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines: list[str] = []
    gaps: list[str] = []

    _, _, timed_out = CodexExecModelExecutor._communicate_bounded(
        process, 5, lines.append, capture_gap_handler=gaps.append
    )

    assert not timed_out
    assert lines == [payload]
    assert gaps == ["oversized", "oversized"]


def test_bounded_communicator_persists_flushed_event_before_process_exit(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    store.start_agent_session(run.run_id, "worker-1", "task", lease_token)
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    payload = json.dumps({"type": "thread.started"})
    script = f"import time\nprint({payload!r}, flush=True)\ntime.sleep(30)\n"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    event_persisted = Event()
    outcome: list[tuple[str, str, bool]] = []

    def record_event(kind: str, source_type: str, role: str | None, text: str) -> None:
        store.record_agent_session_event(
            run.run_id, lease_token, kind, source_type, role, text
        )
        event_persisted.set()

    def communicate() -> None:
        def record_line(line: str) -> None:
            executor._record_session_line(line, record_event)

        outcome.append(
            CodexExecModelExecutor._communicate_bounded(process, 30, record_line)
        )

    communicator = Thread(target=communicate, daemon=True)
    communicator.start()
    try:
        assert event_persisted.wait(timeout=5)
        assert process.poll() is None
        pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
        session = pbi["agent_session"]
        assert session["state"] == "active"
        assert session["events"][0]["text"] == "thread.started"
    finally:
        if process.poll() is None:
            process.kill()
        communicator.join(timeout=5)
        store.close()
    assert not communicator.is_alive()
    assert len(outcome) == 1


def test_bounded_communicator_propagates_session_write_failures() -> None:
    def fail_write(_line: str) -> None:
        raise RuntimeError("session persistence failed")

    for script in (
        "print('session event')",
        "import sys; sys.stdout.write('session event')",
    ):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with pytest.raises(RuntimeError, match="session persistence failed"):
            CodexExecModelExecutor._communicate_bounded(process, 5, fail_write)
