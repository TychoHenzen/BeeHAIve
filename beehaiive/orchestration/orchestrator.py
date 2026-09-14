from __future__ import annotations

from collections.abc import Callable

from ..provider import ProjectProvider
from ..routing import (
    ModelExecutor,
    ModelRouter,
)
from ..storage import OrchestratorStore
from .keyed_lock_manager import KeyedLockManager
from .orchestration_attempt_mixin import OrchestrationAttemptMixin
from .orchestration_failure_mixin import OrchestrationFailureMixin
from .orchestration_handoff_mixin import OrchestrationHandoffMixin
from .orchestration_sync_mixin import OrchestrationSyncMixin


class Orchestrator(
    OrchestrationSyncMixin,
    OrchestrationAttemptMixin,
    OrchestrationHandoffMixin,
    OrchestrationFailureMixin,
):
    def __init__(
        self,
        store: OrchestratorStore,
        provider: ProjectProvider,
        model_router: ModelRouter | None = None,
        model_executor: ModelExecutor | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.model_router = model_router
        self.model_executor = model_executor
        self._handoff_locks = KeyedLockManager()
        self._worker_canceller: Callable[[str], None] | None = None

    def register_worker_canceller(self, canceller: Callable[[str], None]) -> None:
        self._worker_canceller = canceller
