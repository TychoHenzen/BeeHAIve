import json
import sqlite3

import pytest

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.meta_review import MetaReviewError, MetaReviewService
from beehaiive.models import Stage
from beehaiive.reviews.meta_review_helpers import estimate_tokens, source_run_id
from beehaiive.session_evidence import CAPTURE_GAPS, transcript_projection
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.meta_review.helpers import complete_meta_review, seed_meta_review


def start_session(store):
    seed_meta_review(store)
    run = store.claim_next("project-1", "owner/api", "worker")
    assert run is not None
    store.start_agent_session(
        run.run_id, "worker", "private-task-fixture", run.lease_token
    )
    return run


def finish_session(store, run):
    store.advance(run.run_id, Stage.IMPLEMENT, run.lease_token)
    store.ensure_task_contract(
        run.run_id, TaskContract.inventory("owner/api", 1, "PBI 1"), run.lease_token
    )
    store.record_task_result(
        run.run_id, TaskResult(TaskOutcome.PASS, {}), run.lease_token
    )
    store.complete_agent_run(run.run_id, "completed result", run.lease_token)


def message(store, run, text):
    store.record_agent_session_event(
        run.run_id, run.lease_token, "message", "item.completed", "assistant", text
    )


def test_completed_transcripts_are_bounded_redacted_and_stable_after_restart(tmp_path):
    database = tmp_path / "state.sqlite3"
    store = OrchestratorStore(database)
    run = start_session(store)
    for index in range(105):
        message(
            store,
            run,
            f"event {index}\n" + "pass" + "word=credential-fixture\n" + "x" * 550,
        )
    for reason in ("malformed", "oversized", "interrupted"):
        store.record_agent_session_event(
            run.run_id, run.lease_token, "gap", reason, None, "private-payload"
        )
    session = store.get_agent_session(run.run_id)
    assert len(session["events"]) == 100
    assert "credential-fixture" not in json.dumps(session)
    assert "private-payload" not in json.dumps(session)
    finish_session(store, run)
    before = store.completed_session_records("project-1", None, 25)[0]
    store.close()
    store = OrchestratorStore(database)
    assert store.completed_session_records("project-1", None, 25)[0] == before
    captured = []
    service = MetaReviewService(
        store, analyzer=lambda records: captured.extend(records) or []
    )
    result = service.run("project-1")
    assert result["status"] == "completed"
    assert result["selected_records"] == 1
    record = captured[0]
    events = record["transcript"]["events"]
    assert [event["sequence"] for event in events] == list(range(86, 106))
    assert all(len(event["text"]) == 500 for event in events)
    assert events[0]["source_id"] == f"run:{run.run_id}:transcript:86"
    assert set(events[0]) == {
        "source_id",
        "sequence",
        "kind",
        "source_type",
        "role",
        "text",
        "timestamp",
    }
    assert result["input_tokens"] == estimate_tokens(record) <= 8_000
    for secret in ("credential-fixture", "private-task-fixture", "private-payload"):
        assert secret not in json.dumps(record)
    for gap in (
        "malformed",
        "oversized",
        "trimmed",
        "truncated",
        "interrupted",
        "partial",
    ):
        assert gap in str(result["missing_evidence"])
    too_small = service.run("project-1", input_token_limit=result["input_tokens"] - 1)
    assert too_small["selected_records"] == 0
    assert "Input token limit reached" in too_small["missing_evidence"]
    store.close()


def test_storage_redacts_before_truncation_and_bounds_unicode(tmp_path):
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    run = start_session(store)
    message(store, run, "x" * 3_985 + "\n" + "pass" + "word=do-not-retain\n" + "z" * 50)
    stored = store.get_agent_session(run.run_id)["events"][0]["text"]
    assert "do-not" not in stored
    assert len(stored) == 4_000
    for _ in range(10):
        message(store, run, "\U0001f41d" * 4_001)
    events = store.get_agent_session(run.run_id)["events"]
    assert [event["sequence"] for event in events] == [8, 9, 10, 11]
    assert sum(len(event["text"].encode()) for event in events) == 64_000
    store.close()
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    message(store, run, "after restart")
    assert store.get_agent_session(run.run_id)["events"][-1]["sequence"] == 12
    for kind, source, role in (
        ("message", "pass" + "word=source-secret", "assistant"),
        ("message", "agent_message", "pass" + "word=role-secret"),
        ("progress", "command_execution", None),
        ("message", "agent_message", "user"),
    ):
        with pytest.raises(StoreError):
            store.record_agent_session_event(
                run.run_id, run.lease_token, kind, source, role, "private-file-fixture"
            )
    store.record_agent_session_event(
        run.run_id,
        run.lease_token,
        "progress",
        "item.completed",
        None,
        "private-tool-result",
    )
    assert store.get_agent_session(run.run_id)["events"][-1]["text"] == "item.completed"
    flags = store._connection.execute(
        "SELECT capture_flags FROM agent_sessions"
    ).fetchone()[0]
    assert flags & CAPTURE_GAPS["trimmed"] and flags & CAPTURE_GAPS["truncated"]
    store.close()


def test_projection_rejects_malformed_entries_and_redacts_metadata():
    def event(sequence, **values):
        return {
            "sequence": sequence,
            "kind": "message",
            "source_type": "agent_message",
            "role": "assistant",
            "text": "pass" + "word=text-fixture",
            "timestamp": "token" + "=timestamp-fixture",
            "command": "private-file-fixture",
            **values,
        }

    projected = transcript_projection(
        "id",
        [
            event(4),
            event(1),
            event(2, role="user"),
            event(3, source_type="token" + "=source-fixture"),
            event(5, text=None),
            event(6, timestamp=None),
            event(True),
            None,
            event(7),
            event(7),
            event(8, kind="progress", source_type="turn.completed", role=None),
        ],
        ["pass" + "word=gap-fixture"],
    )
    assert [entry["sequence"] for entry in projected["events"]] == [1, 4, 8]
    assert "fixture" not in json.dumps(projected)
    assert "malformed" in projected["gaps"]
    assert "trimmed" in projected["gaps"]
    assert projected["events"][-1]["text"] == "turn.completed"
    assert "missing" in transcript_projection("id", None)["gaps"]


def test_legacy_database_missing_transcript_and_malformed_rows_are_usable(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    store = OrchestratorStore(database)
    seed_meta_review(store)
    complete_meta_review(store)
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE agent_sessions DROP COLUMN capture_flags")
    store = OrchestratorStore(database)
    result = MetaReviewService(store).run("project-1")
    assert result["selected_records"] == 1
    assert "transcript missing" in str(result["missing_evidence"])
    store.close()

    store = OrchestratorStore()
    run = start_session(store)
    message(store, run, "retained")
    message(store, run, "malformed")
    store._connection.execute(
        "UPDATE agent_session_events SET role = 'user' WHERE sequence = 2"
    )
    finish_session(store, run)
    result = MetaReviewService(store).run("project-1")
    assert result["selected_records"] == 1
    assert "transcript malformed" in str(result["missing_evidence"])
    store.close()


@pytest.mark.parametrize("status", ["active", "failed", "cancelled", "timed_out"])
def test_noncompleted_runs_never_enter_analysis(status):
    store = OrchestratorStore()
    run = start_session(store)
    message(store, run, "diagnostic evidence")
    store._connection.execute("UPDATE runs SET status = ?", (status,))
    assert store.completed_session_records("project-1", None, 25) == ()
    result = MetaReviewService(store).run("project-1")
    assert result["selected_records"] == 0
    store.close()


def test_transcript_reference_keeps_suggestion_identity_and_operator_decision():
    store = OrchestratorStore()
    run = start_session(store)
    message(store, run, "evidence")
    finish_session(store, run)
    reference = f"run:{run.run_id}:transcript:1"

    def analyzer(records):
        return [
            {
                "suggestion_key": "transcript-guard",
                "proposed_outcome": "Document evidence",
                "rationale": "Observed bounded evidence",
                "evidence_refs": [reference],
            }
        ]

    service = MetaReviewService(store, analyzer=analyzer)
    first = service.run("project-1")["suggestions"][0]
    requests = []
    service.decide(
        "project-1",
        first["suggestion_id"],
        "accept",
        pbi_creator=lambda request, key: requests.append((request, key)) or {},
    )
    second = service.run("project-1")["suggestions"][0]
    assert first["suggestion_id"] == second["suggestion_id"]
    assert second["status"] == "accepted"
    assert len(requests) == 1
    assert source_run_id(reference) == run.run_id
    for invalid in ("0", "-1", "text", "1:extra", ""):
        with pytest.raises(MetaReviewError):
            source_run_id(f"run:{run.run_id}:transcript:{invalid}")
    store.close()
