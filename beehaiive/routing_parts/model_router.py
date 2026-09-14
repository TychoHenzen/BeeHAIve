from __future__ import annotations

from typing import TYPE_CHECKING

from .execution_lock_manager import ExecutionLockManager
from .router_decision_mixin import RouterDecisionMixin
from .router_execution_mixin import RouterExecutionMixin
from .router_lifecycle_mixin import RouterLifecycleMixin
from .routing_config import RoutingConfig

if TYPE_CHECKING:
    from .routing_store import RoutingStore


class ModelRouter(RouterLifecycleMixin, RouterExecutionMixin, RouterDecisionMixin):
    def __init__(
        self, store: RoutingStore, config: RoutingConfig | None = None
    ) -> None:
        self.store = store
        self.config = config or RoutingConfig()
        self._execution_locks = ExecutionLockManager()
