from beehaiive.review import (
    PullRequestTarget,
    ReaderExecution,
)


class FailingReader:
    def review(self, target: PullRequestTarget) -> ReaderExecution:
        raise RuntimeError("reader dependency unavailable")
