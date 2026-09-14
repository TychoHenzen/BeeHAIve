from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest

from beehaiive.github import transport as transport_module
from beehaiive.provider import (
    GitHubOutcomeUnknownError,
    UrllibGraphQLClient,
)


def test_urllib_rest_client_marks_http_408_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UrllibGraphQLClient("token", "https://example.test/graphql")

    def raise_timeout(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://example.test/repos/owner/api/issues",
            408,
            "request timeout",
            {},
            BytesIO(b'{"message":"request timeout"}'),
        )

    monkeypatch.setattr(transport_module, "urlopen", raise_timeout)
    with pytest.raises(GitHubOutcomeUnknownError) as timeout_error:
        client.request_rest("POST", "/repos/owner/api/issues", {"title": "A PBI"})

    assert timeout_error.value.status_code == 408
