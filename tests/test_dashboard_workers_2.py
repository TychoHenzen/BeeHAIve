import pytest

import main as main_module
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_start_failure_stops_claimed_run() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")

    class FailingWorker:
        def claim(self, project_id, repository, owner_id, task):
            assert task == main_module.DEFAULT_DEMO_TASK
            return service.claim(project_id, repository, owner_id)

        def start(self, run) -> None:
            del run
            raise RuntimeError("thread start failed")

    try:
        with pytest.raises(StoreError, match="thread start failed"):
            main_module._execute_dashboard_action(
                service,
                "project-1",
                main_module.DashboardStartRequest(
                    action="start", approved=True, repository="owner/api"
                ),
                FailingWorker(),
            )
        run = service.store.active_runs_for_project("project-1")
        assert run == ()
        failed = service.store.project_state("project-1")["repositories"][0]["pbis"][0]
        assert (
            failed["last_error"] == "Agent worker failed to start: thread start failed"
        )
    finally:
        service.store.close()


def test_dashboard_start_cleanup_failure_is_reported() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")

    class FailingWorker:
        def claim(self, project_id, repository, owner_id, task):
            del task
            return service.claim(project_id, repository, owner_id)

        def start(self, run) -> None:
            del run
            raise RuntimeError("thread start failed")

    original_stop = service.stop

    def fail_stop(run_id: str, reason: str):
        del run_id, reason
        raise StoreError("cleanup unavailable")

    service.stop = fail_stop  # type: ignore[method-assign]
    try:
        with pytest.raises(StoreError, match="cleanup failed: cleanup unavailable"):
            main_module._execute_dashboard_action(
                service,
                "project-1",
                main_module.DashboardStartRequest(
                    action="start", approved=True, repository="owner/api"
                ),
                FailingWorker(),
            )
    finally:
        service.stop = original_stop  # type: ignore[method-assign]
        service.store.close()
