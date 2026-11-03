from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
    RateLimitError,
)
from tests.support import FakeTransport, http_error, response

SECONDARY_URL = "https://api.github.com/users/owner/projectsV2/2"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def test_etag_reuses_cached_body() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    not_modified = HttpResponse(status_code=304, headers={}, body="")
    transport = FakeTransport(
        {url: [response([], headers={"ETag": '"abc"'}), not_modified]}
    )
    client = GithubRestClient("token", database, transport=transport)
    assert client.get_json(url).value == []
    cached = client.get_json(url)
    assert cached.from_cache
    assert transport.calls[1][1]["If-None-Match"] == '"abc"'
    database.close()


def test_primary_rate_limit_uses_headers_and_blocks_the_next_call() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    reset = int((datetime.now(UTC) + timedelta(minutes=2)).timestamp())
    rate_limit = http_error(
        403,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        "slow down",
    )
    limited = FakeTransport({url + "/fields": [rate_limit]})
    limited_database = SnapshotDatabase(":memory:")
    limited_client = GithubRestClient("token", limited_database, transport=limited)
    with pytest.raises(RateLimitError) as raised:
        limited_client.get_json(url + "/fields")
    assert raised.value.primary
    assert raised.value.rate_limited_until.timestamp() >= reset
    with pytest.raises(RateLimitError) as blocked:
        limited_client.get_json(url + "/fields")
    assert blocked.value.primary
    assert len(limited.calls) == 1
    limited_database.close()


def secondary_limit(status_code: int, retry_after: str | None = None) -> HttpResponse:
    headers = {"X-RateLimit-Remaining": "10"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return http_error(status_code, headers, "You have exceeded a secondary rate limit")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class RateLimitRig:
    transport: FakeTransport
    client: GithubRestClient
    clock: Clock
    database: SnapshotDatabase


def rate_limit_rig(responses: list[HttpResponse]) -> RateLimitRig:
    transport = FakeTransport({SECONDARY_URL: responses})
    database = SnapshotDatabase(":memory:")
    clock = Clock()
    client = GithubRestClient("token", database, transport=transport, clock=clock)
    return RateLimitRig(transport, client, clock, database)


def limited_error(client: GithubRestClient) -> RateLimitError:
    with pytest.raises(RateLimitError) as raised:
        client.get_json(SECONDARY_URL)
    return raised.value


def test_secondary_deadlines_and_429_are_enforced() -> None:
    rig = rate_limit_rig(
        [
            secondary_limit(403, "7"),
            secondary_limit(403),
            secondary_limit(403),
            secondary_limit(429, "5"),
        ]
    )

    first = limited_error(rig.client)
    assert not first.primary
    assert first.rate_limited_until == NOW + timedelta(seconds=7)
    assert limited_error(rig.client).rate_limited_until == first.rate_limited_until
    assert len(rig.transport.calls) == 1

    for wait, backoff in ((7, 60), (60, 120)):
        rig.clock.now += timedelta(seconds=wait)
        blocked = limited_error(rig.client)
        assert blocked.rate_limited_until == rig.clock.now + timedelta(seconds=backoff)

    rig.clock.now += timedelta(seconds=120)
    final = limited_error(rig.client)
    assert not final.primary
    assert final.rate_limited_until == rig.clock.now + timedelta(seconds=5)
    assert len(rig.transport.calls) == 4
    rig.database.close()


def test_forbidden_response_is_not_treated_as_rate_limit() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    forbidden = http_error(
        403, {"X-RateLimit-Remaining": "4999"}, "Resource not accessible by integration"
    )
    transport = FakeTransport({url: [forbidden]})
    client = GithubRestClient("token", database, transport=transport)

    with pytest.raises(GithubResponseError, match="403"):
        client.get_json(url)

    database.close()
