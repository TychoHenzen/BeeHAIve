from __future__ import annotations

from beehaiive.models import (
    ProjectSnapshot,
)
from tests.support.edges.stub_provider import StubProvider


class StorageProvider(StubProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return self.snapshot
