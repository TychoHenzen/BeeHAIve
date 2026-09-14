from threading import Event

from beehaiive.review import (
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
)
from tests.support.review.reader_double import ReaderDouble


class BlockingReader(ReaderDouble):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return ReaderExecution(ReaderStatus.PASS)
