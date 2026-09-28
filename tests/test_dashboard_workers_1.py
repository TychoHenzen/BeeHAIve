from types import SimpleNamespace

import pytest

import main as main_module
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import (
    dashboard_snapshot,
)


def test_dashboard_delivery_rejects_a_run_outside_the_requested_scope() -> None:
    orchestrator = SimpleNamespace(store=SimpleNamespace(get_run=lambda _run_id: None))
    with pytest.raises(main_module.HTTPException) as raised:
        main_module._require_dashboard_delivery_run(
            orchestrator,
            "project-1",
            "owner/api",
            40,
            "missing-run",
        )
    assert raised.value.status_code == 403


def test_dashboard_commit_push_requires_a_worker() -> None:
    request = main_module.DashboardCommitPushRequest(
        action="commit_push",
        approved=True,
        repository="owner/api",
        pbi_number=40,
        run_id="run-40",
    )
    with pytest.raises(StoreError, match="Agent worker is not configured"):
        main_module._execute_dashboard_action(SimpleNamespace(), "project-1", request)


def test_dashboard_stop_cancels_worker_before_stopping_run() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-api")
    assert run is not None

    class RecordingWorker:
        def __init__(self) -> None:
            self.cancelled: str | None = None

        def cancel(self, run_id: str) -> None:
            self.cancelled = run_id

    worker = RecordingWorker()
    try:
        result = main_module._execute_dashboard_action(
            service,
            "project-1",
            main_module.DashboardStopRequest(
                action="stop",
                run_id=run.run_id,
                approved=True,
            ),
            worker,
        )
        assert worker.cancelled == run.run_id
        assert result["run"]["status"] == "failed"
    finally:
        service.store.close()
