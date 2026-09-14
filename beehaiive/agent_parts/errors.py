from __future__ import annotations

from beehaiive.storage import StoreError


class WorkerCapacityError(StoreError):
    """A worker start lost a race for a configured capacity slot."""


__all__ = ["WorkerCapacityError"]
