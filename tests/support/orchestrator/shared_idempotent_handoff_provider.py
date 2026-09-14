from threading import Event, Lock

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
)
from tests.conftest import FakeProvider


class SharedIdempotentHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.external_artifacts: dict[str, HandoffResult] = {}
        self._external_lock = Lock()
        self.first_started = Event()
        self.second_started = Event()
        self.release_first = Event()

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        with self._external_lock:
            first_call = not self.handoffs
            self.handoffs.append(request)
            result = self.external_artifacts.setdefault(
                request.branch,
                HandoffResult(
                    request.branch,
                    f"https://example.test/{request.repository}/pull/1",
                    1,
                ),
            )
            if first_call:
                self.first_started.set()
            else:
                self.second_started.set()
        if first_call:
            assert self.release_first.wait(timeout=2)
        return result
