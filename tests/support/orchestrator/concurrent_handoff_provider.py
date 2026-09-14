from threading import Event

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
)
from tests.conftest import FakeProvider


class ConcurrentHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.started = Event()
        self.release = Event()

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        return super().create_handoff(request)
