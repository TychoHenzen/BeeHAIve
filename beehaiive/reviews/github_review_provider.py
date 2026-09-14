from __future__ import annotations

from threading import Lock

from ..provider import (
    GraphQLClient,
)
from .github_configuration_mixin import GithubConfigurationMixin
from .github_publish_mixin import GithubPublishMixin
from .github_read_mixin import GithubReadMixin


class GitHubReviewProvider(
    GithubConfigurationMixin, GithubReadMixin, GithubPublishMixin
):
    def __init__(
        self,
        token: str | None = None,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._client = client
        self._endpoint = endpoint
        self._lock = Lock()
