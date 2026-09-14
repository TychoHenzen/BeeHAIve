from beehaiive.review import (
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
)


class ReaderDouble:
    def __init__(
        self,
        status: ReaderStatus = ReaderStatus.PASS,
        findings: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.findings = findings
        self.calls = 0

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        assert target.ready
        self.calls += 1
        return ReaderExecution(self.status, self.findings)
