from __future__ import annotations

from beehaiive.scheduler import (
    AgentScheduler,
    SchedulerConfig,
)
from tests.support.scheduler.fake_orchestrator import (
    FakeOrchestrator as FakeOrchestrator,
)
from tests.support.scheduler.fake_worker import FakeWorker as FakeWorker


def create_scheduler(
    states: dict[str, dict[str, object]],
    worker: FakeWorker | None = None,
    projects: set[str] | None = None,
    *,
    maximum: int = 1,
) -> tuple[AgentScheduler, FakeOrchestrator, FakeWorker]:
    actual_worker = worker or FakeWorker()
    orchestrator = FakeOrchestrator(states)
    scheduler = AgentScheduler(
        orchestrator,
        actual_worker,
        projects or set(states),
        SchedulerConfig(enabled=True, max_concurrency=maximum),
    )
    return scheduler, orchestrator, actual_worker
