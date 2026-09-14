from __future__ import annotations

from tests.support.scheduler.fake_store import FakeStore as FakeStore


class FakeOrchestrator:
    def __init__(self, states: dict[str, dict[str, object]]) -> None:
        self.store = FakeStore(states)
        self.synchronized: list[str] = []
        self.sync_error: Exception | None = None
        self.stopped: list[tuple[str, str]] = []
        self.stop_error: Exception | None = None

    def synchronize(self, project_id: str) -> None:
        self.synchronized.append(project_id)
        if self.sync_error is not None:
            raise self.sync_error

    def stop(self, run_id: str, reason: str) -> None:
        self.stopped.append((run_id, reason))
        if self.stop_error is not None:
            raise self.stop_error
