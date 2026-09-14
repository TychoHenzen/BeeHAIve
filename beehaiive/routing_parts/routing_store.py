from __future__ import annotations

from .routing_store_core_mixin import RoutingStoreCoreMixin
from .routing_store_problem_mixin import RoutingStoreProblemMixin


class RoutingStore(RoutingStoreCoreMixin, RoutingStoreProblemMixin):
    pass
