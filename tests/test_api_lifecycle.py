from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beehaiive.api import lifecycle
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def _runtime(
    events: list[str],
    *,
    require_review_adapters: bool = False,
    include_worker: bool = True,
    include_scheduler: bool = True,
    include_review_repair: bool = True,
    has_review_provider: bool = True,
) -> SimpleNamespace:
    class Store:
        def recover_interrupted_operator_notifications(self) -> None:
            events.append("notifications.recover")

    class Worker:
        def recover(self, project_ids=None) -> None:
            del project_ids
            events.append("worker.recover")

        def shutdown(self) -> None:
            events.append("worker.shutdown")

    class Scheduler:
        project_ids = ("owner:7",)
        config = SimpleNamespace(enabled=True)

        def start(self) -> None:
            events.append("scheduler.start")

        def shutdown(self) -> None:
            events.append("scheduler.shutdown")

    class ReviewProvider:
        def validate_configuration(self) -> None:
            events.append("review.validate")

    class ReviewRepair:
        def recover(self) -> None:
            events.append("review_repair.recover")

    return SimpleNamespace(
        orchestrator=SimpleNamespace(store=Store()),
        agent_worker=Worker() if include_worker else None,
        scheduler=Scheduler() if include_scheduler else None,
        require_review_adapters=require_review_adapters,
        review_service=SimpleNamespace(
            provider=ReviewProvider() if has_review_provider else None,
            readers={},
        ),
        review_repair_service=ReviewRepair() if include_review_repair else None,
    )


def test_lifespan_preserves_startup_order_and_shutdown_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "dispatch_pending_operator_notifications",
        lambda _store: events.append("notifications.dispatch"),
    )
    runtime = _runtime(
        events,
        require_review_adapters=True,
    )
    runtime.review_service.readers = {
        concern: object() for concern in lifecycle.REQUIRED_CONCERNS
    }
    app = FastAPI(lifespan=lifecycle.register_background_handlers(runtime))

    with TestClient(app):
        assert events == [
            "notifications.recover",
            "notifications.dispatch",
            "review.validate",
            "review_repair.recover",
            "worker.recover",
            "scheduler.start",
        ]

    assert events == [
        "notifications.recover",
        "notifications.dispatch",
        "review.validate",
        "review_repair.recover",
        "worker.recover",
        "scheduler.start",
        "scheduler.shutdown",
        "worker.shutdown",
    ]


def test_lifespan_skips_optional_steps_without_worker_or_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "dispatch_pending_operator_notifications",
        lambda _store: events.append("notifications.dispatch"),
    )
    runtime = _runtime(
        events,
        include_worker=False,
        include_scheduler=False,
        include_review_repair=False,
    )
    app = FastAPI(lifespan=lifecycle.register_background_handlers(runtime))

    with TestClient(app):
        pass

    assert events == ["notifications.recover", "notifications.dispatch"]
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []


def test_lifespan_stops_startup_before_recovery_when_review_adapters_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "dispatch_pending_operator_notifications",
        lambda _store: events.append("notifications.dispatch"),
    )
    runtime = _runtime(
        events,
        require_review_adapters=True,
        has_review_provider=False,
    )
    app = FastAPI(lifespan=lifecycle.register_background_handlers(runtime))

    with (
        pytest.raises(
            RuntimeError,
            match="Production review adapters are not configured",
        ),
        TestClient(app),
    ):
        pass

    assert events == ["notifications.recover", "notifications.dispatch"]


def test_create_app_has_no_legacy_lifecycle_event_handlers() -> None:
    store = OrchestratorStore()
    try:
        app = create_app(orchestrator=Orchestrator(store, ApiProvider()))
        assert app.router.on_startup == []
        assert app.router.on_shutdown == []
    finally:
        store.close()
