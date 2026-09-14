from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeWorker:
    def __init__(self) -> None:
        self.workflow_service = object()
        self.executor = SimpleNamespace(
            task="scheduled task", _secret_values=("scheduler-secret",)
        )
        self.maximum = 0
        self.active = 0
        self.claims: list[tuple[str, str, str, str]] = []
        self.started: list[str] = []
        self.claim_error: Exception | None = None
        self.start_error: Exception | None = None
        self.recover_error: Exception | None = None
        self.claim_result: bool = True
        self.recovered_projects: tuple[str, ...] | None = None

    @property
    def active_worker_count(self) -> int:
        return self.active

    def set_max_concurrent_workers(self, maximum: int) -> None:
        self.maximum = maximum

    def has_capacity(self) -> bool:
        return self.active < self.maximum

    def claim(
        self, project_id: str, repository: str, owner_id: str, *, task: str
    ) -> SimpleNamespace | None:
        self.claims.append((project_id, repository, owner_id, task))
        if self.claim_error is not None:
            raise self.claim_error
        if not self.claim_result:
            return None
        return SimpleNamespace(run_id=f"run-{len(self.claims)}")

    def start(self, run: SimpleNamespace) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(run.run_id)
        self.active += 1

    def recover(self, project_ids: Any = None) -> tuple[str, ...]:
        self.recovered_projects = tuple(project_ids or ())
        if self.recover_error is not None:
            raise self.recover_error
        return ()
