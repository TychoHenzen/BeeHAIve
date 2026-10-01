from __future__ import annotations

import json

import pytest

from tests.support.disposable_delivery_fixture import (
    STAGES,
    run_disposable_delivery_fixture,
)


def test_fixture_proves_successful_delivery_and_redacted_evidence() -> None:
    fixture = run_disposable_delivery_fixture()
    report = fixture.report()

    assert tuple(event.stage for event in fixture.events) == tuple(
        stage.value for stage in STAGES
    )
    assert fixture.completed is True
    assert fixture.cleaned is True
    assert report["evidence"] == "fixture"
    assert "a" * 40 in json.dumps(report)
    assert "fixture" in json.dumps(report)
    assert "C:\\" not in json.dumps(report)
    assert "token" not in json.dumps(report).lower()


@pytest.mark.parametrize(
    "failure",
    ("failed checks", "unknown provider", "stale head", "cancelled", "cleanup error"),
)
def test_fixture_failures_never_claim_completion(failure: str) -> None:
    fixture = run_disposable_delivery_fixture(failure)

    assert fixture.completed is False
    assert fixture.events[-1].outcome in {"blocked", "recoverable"}


def test_fixture_retry_is_idempotent() -> None:
    fixture = run_disposable_delivery_fixture()
    original_artifacts = dict(fixture.artifacts)
    original_event_count = len(fixture.events)

    fixture.retry()
    fixture.retry()

    assert fixture.attempts == {"run": 2}
    assert fixture.artifacts == original_artifacts
    assert len(fixture.events) == original_event_count
