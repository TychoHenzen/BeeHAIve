import json
from threading import Event, Thread

import pytest

from beehaiive.agent import CodexExecModelExecutor
from beehaiive.routing import AttemptOutcome, ModelRouter, ModelTier, RoutingStore
from beehaiive.session_evidence import CAPTURE_GAPS
from beehaiive.storage import OrchestratorStore
from tests.support.agent.script_executor import ScriptExecutor
from tests.test_meta_review_transcripts import finish_session, start_session


def test_capture_excludes_private_payloads_and_redacts_before_persistence(tmp_path):
    store = OrchestratorStore()
    run = start_session(store)
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    executor._secret_values = ("known-worker-fixture",)

    def handler(*event):
        store.record_agent_session_event(run.run_id, run.lease_token, *event)

    for kind in ("reasoning", "command_execution", "user_message", "file_change"):
        executor._record_session_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": kind,
                        "text": "private-fixture",
                        "command": "private-fixture",
                    },
                }
            ),
            handler,
        )
    for line in ("malformed-private-fixture", "[]"):
        executor._record_session_line(line, handler)
    executor._record_session_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "known-worker-fixture\n" + "x" * 4_001,
                },
            }
        ),
        handler,
    )
    session = store.get_agent_session(run.run_id)
    assert "fixture" not in json.dumps(session["events"])
    assert session["events"][-1]["text"].startswith("[redacted]")
    assert len(session["events"][-1]["text"]) == 4_000
    assert len(session["events"]) == 5
    finish_session(store, run)
    transcript = store.completed_session_records("project-1", None, 25)[0]["transcript"]
    assert {"malformed", "truncated"}.issubset(transcript["gaps"])
    store.close()


def test_capture_message_blocks_exclude_reasoning_and_tool_payloads(tmp_path):
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    events = []
    for payload in (
        {},
        {"type": "agent_message", "text": 42},
        {"type": "item.completed", "item": {"type": "agent_message", "text": {}}},
        {
            "type": "agent_message",
            "content": [
                {"type": "reasoning", "text": "private-reasoning"},
                {"type": "tool_result", "text": "private-file"},
                {"type": "output_text", "text": "visible answer"},
                None,
            ],
        },
    ):
        executor._record_session_line(
            json.dumps(payload), lambda *event: events.append(event)
        )
    assert events[:3] == [("gap", "malformed", None, "")] * 3
    assert events[-1] == ("message", "agent_message", "assistant", "visible answer")


def test_cancelled_capture_retains_evidence_without_completion(tmp_path):
    store = OrchestratorStore()
    run = start_session(store)
    script = tmp_path / "cancel.py"
    script.write_text(
        "import json, time\n"
        "print(json.dumps({'type':'agent_message','text':'before stop'}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script, timeout_seconds=10)
    captured = Event()

    def handler(*event):
        store.record_agent_session_event(run.run_id, run.lease_token, *event)
        captured.set()

    executor.set_session_event_handler(run.run_id, handler)
    routing = RoutingStore()
    router = ModelRouter(routing)
    decision = router.begin(run.run_id).decision
    results = []
    thread = Thread(
        target=lambda: results.append(
            executor.execute(router.config.spec_for(ModelTier.LUNA), decision)
        )
    )
    thread.start()
    try:
        assert captured.wait(5)
    finally:
        executor.cancel(run.run_id)
        thread.join(10)
    assert not thread.is_alive()
    assert results[0].outcome is AttemptOutcome.FAILURE
    assert "stopped by operator" in results[0].failure_context
    assert store.get_agent_session(run.run_id)["events"][0]["text"] == "before stop"
    assert store._transcript_for_run(run.run_id)["gaps"] == [
        "interrupted",
        "partial: bounded capture does not establish transcript completeness",
    ]
    assert store.completed_session_records("project-1", None, 25) == ()
    routing.close()
    store.close()


@pytest.mark.parametrize(
    "ending", ["raise SystemExit(2)", "import time; time.sleep(30)"]
)
def test_failed_and_timed_out_capture_stays_diagnostic(tmp_path, ending):
    store = OrchestratorStore()
    run = start_session(store)
    script = tmp_path / "partial.py"
    script.write_text(
        "import json\nprint(json.dumps({'type':'agent_message', "
        "'text':'partial evidence'}), flush=True)\n" + ending + "\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script, timeout_seconds=0.5)
    executor.set_session_event_handler(
        run.run_id,
        lambda *event: store.record_agent_session_event(
            run.run_id, run.lease_token, *event
        ),
    )
    routing = RoutingStore()
    router = ModelRouter(routing)
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA), router.begin(run.run_id).decision
    )
    assert result.outcome is AttemptOutcome.FAILURE
    assert (
        store.get_agent_session(run.run_id)["events"][0]["text"] == "partial evidence"
    )
    flags = store._connection.execute(
        "SELECT capture_flags FROM agent_sessions"
    ).fetchone()[0]
    assert flags & CAPTURE_GAPS["interrupted"]
    assert store.completed_session_records("project-1", None, 25) == ()
    # A later completion keeps earlier diagnostics and the gap.
    finish_session(store, run)
    transcript = store.completed_session_records("project-1", None, 25)[0]["transcript"]
    assert "interrupted" in transcript["gaps"]
    routing.close()
    store.close()
