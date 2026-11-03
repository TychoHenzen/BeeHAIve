from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
    RateLimitError,
)
from tests.support import FakeTransport, response


def test_etag_reuses_cached_body_and_rate_limit_uses_headers() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    transport = FakeTransport(
        {
            url: [
                response([], headers={"ETag": '"abc"'}),
                HttpResponse(status_code=304, headers={}, body=""),
            ]
        }
    )
    client = GithubRestClient("token", database, transport=transport)
    assert client.get_json(url).value == []
    cached = client.get_json(url)
    assert cached.from_cache
    assert transport.calls[1][1]["If-None-Match"] == '"abc"'

    reset = int((datetime.now(UTC) + timedelta(minutes=2)).timestamp())
    limited = FakeTransport(
        {
            url + "/fields": [
                HttpResponse(
                    status_code=403,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset),
                    },
                    body='{"message":"slow down"}',
                )
            ]
        }
    )
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
    database.close()


def test_secondary_deadlines_and_429_are_enforced() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    current = [now]

    def secondary_response(
        status_code: int, headers: Mapping[str, str]
    ) -> HttpResponse:
        return HttpResponse(
            status_code=status_code,
            headers=headers,
            body='{"message":"You have exceeded a secondary rate limit"}',
        )

    transport = FakeTransport(
        {
            url: [
                secondary_response(
                    403,
                    {
                        "X-RateLimit-Remaining": "10",
                        "Retry-After": "7",
                    },
                ),
                secondary_response(403, {"X-RateLimit-Remaining": "10"}),
                secondary_response(403, {"X-RateLimit-Remaining": "10"}),
                secondary_response(
                    429,
                    {
                        "X-RateLimit-Remaining": "10",
                        "Retry-After": "5",
                    },
                ),
            ]
        }
    )
    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        "token", database, transport=transport, clock=lambda: current[0]
    )

    with pytest.raises(RateLimitError) as retry_after:
        client.get_json(url)
    assert not retry_after.value.primary
    assert retry_after.value.rate_limited_until == now + timedelta(seconds=7)

    with pytest.raises(RateLimitError) as blocked:
        client.get_json(url)
    assert blocked.value.rate_limited_until == retry_after.value.rate_limited_until
    assert len(transport.calls) == 1

    current[0] += timedelta(seconds=7)
    with pytest.raises(RateLimitError) as first_backoff:
        client.get_json(url)
    assert first_backoff.value.rate_limited_until == current[0] + timedelta(seconds=60)

    current[0] += timedelta(seconds=60)
    with pytest.raises(RateLimitError) as second_backoff:
        client.get_json(url)
    assert second_backoff.value.rate_limited_until == current[0] + timedelta(
        seconds=120
    )

    current[0] += timedelta(seconds=120)
    with pytest.raises(RateLimitError) as too_many_requests:
        client.get_json(url)
    assert not too_many_requests.value.primary
    assert too_many_requests.value.rate_limited_until == current[0] + timedelta(
        seconds=5
    )
    assert len(transport.calls) == 4
    database.close()


def test_forbidden_response_is_not_treated_as_rate_limit() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    transport = FakeTransport(
        {
            url: [
                HttpResponse(
                    status_code=403,
                    headers={"X-RateLimit-Remaining": "4999"},
                    body='{"message":"Resource not accessible by integration"}',
                )
            ]
        }
    )
    client = GithubRestClient("token", database, transport=transport)

    with pytest.raises(GithubResponseError, match="403"):
        client.get_json(url)

    database.close()
