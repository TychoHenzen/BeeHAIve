from beehaiive.review import (
    PullRequestTarget,
    ReviewError,
)


class FixtureProvider:
    def __init__(self, targets: dict[str, PullRequestTarget]) -> None:
        self.targets = targets

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        try:
            return self.targets[pull_request_id]
        except KeyError as exc:
            raise ReviewError(f"Unknown pull request: {pull_request_id}") from exc
