from __future__ import annotations

import json

import pytest

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.task_contract.helpers import snapshot


class Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


def _pending_question(tmp_path, filename: str = "notification.sqlite3"):
    provider = FakeProvider(snapshot())
    store = OrchestratorStore(tmp_path / filename)
    orchestrator = Orchestrator(store, provider)
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = (
        store.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "").lease_token
        or ""
    )
    contract = TaskContract.inventory(run.repository, run.pbi_number, run.title)
    store.ensure_task_contract(run.run_id, contract, lease)
    result = TaskResult(
        TaskOutcome.QUESTION, {}, question="Which branch should be used?"
    )
    store.record_task_result(run.run_id, result, lease)
    question = store.await_operator(
        run.run_id,
        lease,
        kind="question",
        question=result.question or "",
        evidence=result.evidence,
    )
    return store, question


def test_notification_sends_deduplicated_webhook_and_stores_delivery(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    calls: list[dict[str, object]] = []

    def post(
        url: str, *, json: dict[str, object], headers: dict[str, str], timeout: float
    ):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return Response(204)

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr("beehaiive.operator_notifications.httpx.post", post)

    result = dispatch_pending_operator_notifications(store)
    saved = store.operator_question_for_run(question["run_id"])

    assert result["delivered"] == 1
    assert len(calls) == 1
    assert calls[0]["url"] == "https://hooks.example/test"
    assert calls[0]["headers"]["Authorization"] == "Bearer hook-secret"
    assert calls[0]["headers"]["Idempotency-Key"] == question["question_id"]
    assert "hook-secret" not in json.dumps(calls[0]["json"])
    assert saved is not None
    assert saved["notification_status"] == "delivered"
    assert saved["notification_attempts"] == 1
    assert store.operator_question_for_run(question["run_id"]) == saved
    store.close()


def test_missing_webhook_config_is_visible_then_can_be_recovered(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    monkeypatch.delenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", raising=False)
    result = dispatch_pending_operator_notifications(store)
    missing = store.operator_question_for_run(question["run_id"])
    assert result["not_configured"] == 1
    assert missing is not None
    assert missing["notification_status"] == "not_configured"
    assert missing["notification_attempts"] == 0

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr(
        "beehaiive.operator_notifications.httpx.post",
        lambda *_args, **_kwargs: Response(202),
    )
    recovered = dispatch_pending_operator_notifications(store)
    delivered = store.operator_question_for_run(question["run_id"])
    assert recovered["delivered"] == 1
    assert delivered is not None
    assert delivered["notification_status"] == "delivered"
    assert delivered["notification_attempts"] == 1
    store.close()


def test_transient_webhook_failure_retries_with_same_idempotency_key(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    responses = iter((Response(503), Response(204)))
    keys: list[str] = []

    def post(_url: str, *, headers: dict[str, str], **_kwargs):
        keys.append(headers["Idempotency-Key"])
        return next(responses)

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr("beehaiive.operator_notifications.httpx.post", post)
    monkeypatch.setattr(
        "beehaiive.operator_notifications.time.sleep", lambda _delay: None
    )

    result = dispatch_pending_operator_notifications(store)
    saved = store.operator_question_for_run(question["run_id"])

    assert result["delivered"] == 1
    assert keys == [question["question_id"], question["question_id"]]
    assert saved is not None
    assert saved["notification_status"] == "delivered"
    assert saved["notification_attempts"] == 2
    store.close()


def test_stopping_run_closes_pending_question_and_suppresses_notification(
    tmp_path,
) -> None:
    store, question = _pending_question(tmp_path)

    stopped = store.stop(str(question["run_id"]))

    closed = store.operator_question_for_run(str(question["run_id"]))
    assert stopped.status.value == "failed"
    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["notification_status"] == "cancelled"
    assert str(question["question_id"]) not in store.pending_operator_notification_ids()
    with pytest.raises(StoreError, match="not awaiting"):
        store.answer_operator_question(
            str(question["run_id"]),
            question_id=str(question["question_id"]),
            revision=int(question["revision"]),
            answer="main",
            authorization_method="X-API-Key",
        )
    store.close()


def test_transient_webhook_failures_stop_after_three_attempts(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response(503)

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr("beehaiive.operator_notifications.httpx.post", post)
    monkeypatch.setattr(
        "beehaiive.operator_notifications.time.sleep", lambda _delay: None
    )

    result = dispatch_pending_operator_notifications(store)
    saved = store.operator_question_for_run(str(question["run_id"]))

    assert result["attempted"] == 3
    assert result["failed"] == 1
    assert calls == 3
    assert saved is not None
    assert saved["notification_status"] == "failed"
    assert saved["notification_attempts"] == 3
    assert saved["notification_last_status_code"] == 503
    store.close()


def test_permanent_webhook_failure_is_terminal_and_https_is_required(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response(401)

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "http://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr("beehaiive.operator_notifications.httpx.post", post)
    rejected = dispatch_pending_operator_notifications(store)
    assert rejected["not_configured"] == 1
    assert calls == 0

    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    result = dispatch_pending_operator_notifications(store)
    saved = store.operator_question_for_run(str(question["run_id"]))
    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert calls == 1
    assert saved is not None
    assert saved["notification_status"] == "failed"
    assert saved["notification_attempts"] == 1
    assert saved["notification_last_status_code"] == 401
    assert "hook-secret" not in str(saved["notification_last_error"])
    store.close()


def test_interrupted_notification_is_recovered_after_store_reopen(
    tmp_path, monkeypatch
) -> None:
    from beehaiive.operator_notifications import dispatch_pending_operator_notifications

    store, question = _pending_question(tmp_path)
    claimed = store.claim_operator_notification(str(question["question_id"]))
    assert claimed is not None
    store.close()

    reopened = OrchestratorStore(tmp_path / "notification.sqlite3")
    assert reopened.recover_interrupted_operator_notifications() == 1
    monkeypatch.setenv(
        "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/test"
    )
    monkeypatch.setenv("BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr(
        "beehaiive.operator_notifications.httpx.post",
        lambda *_args, **_kwargs: Response(204),
    )

    result = dispatch_pending_operator_notifications(reopened)
    saved = reopened.operator_question_for_run(str(question["run_id"]))
    assert result["delivered"] == 1
    assert saved is not None
    assert saved["notification_status"] == "delivered"
    assert saved["notification_attempts"] == 2
    reopened.close()


def test_answer_cancels_an_inflight_transient_notification(tmp_path) -> None:
    store, question = _pending_question(tmp_path)
    claimed = store.claim_operator_notification(str(question["question_id"]))
    assert claimed is not None

    answered = store.answer_operator_question(
        str(question["run_id"]),
        question_id=str(question["question_id"]),
        revision=int(question["revision"]),
        answer="main",
        authorization_method="X-API-Key",
    )
    completion = store.complete_operator_notification_attempt(
        str(question["question_id"]),
        str(claimed["lease_token"]),
        status_code=503,
        delivered=False,
        retryable=True,
        error="Webhook returned HTTP 503",
    )
    saved = store.operator_question_for_run(str(question["run_id"]))

    assert answered["status"] == "answered"
    assert completion is None
    assert saved is not None
    assert saved["notification_status"] == "cancelled"
    assert str(question["question_id"]) not in store.pending_operator_notification_ids()
    store.close()
