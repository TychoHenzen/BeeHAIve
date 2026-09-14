from beehaiive.review import (
    PullRequestTarget,
)


class ExplodingProvider:
    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        raise RuntimeError("provider dependency unavailable")
