from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from .held_item import HeldItem
from .models import ProjectSnapshot


class SnapshotSource(Protocol):
    def fetch_snapshot(self) -> ProjectSnapshot: ...

    def fetch_item(
        self,
        item_key: str,
        *,
        status_field_id: str | None,
        status_options: Sequence[str] = (),
        status_option_ids: Mapping[str, str] | None = None,
    ) -> HeldItem: ...
