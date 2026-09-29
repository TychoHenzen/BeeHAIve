from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .database import SnapshotDatabase


class GithubRestError(RuntimeError):
    pass


class GithubResponseError(GithubRestError):
    pass


class RateLimitError(GithubRestError):
    def __init__(
        self, message: str, rate_limited_until: datetime, *, primary: bool
    ) -> None:
        super().__init__(message)
        self.rate_limited_until = rate_limited_until
        self.primary = primary


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes | str


@dataclass(frozen=True)
class RestPayload:
    value: Any
    headers: dict[str, str]
    url: str
    from_cache: bool


Transport = Callable[[str, Mapping[str, str]], HttpResponse]


class GithubRestClient:
    def __init__(
        self,
        token: str,
        database: SnapshotDatabase,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._token = token
        self._database = database
        self._transport = transport or _urlopen
        self._clock = clock or (lambda: datetime.now(UTC))
        self._secondary_attempts: dict[str, int] = {}

    def get_json(
        self, url: str, params: Mapping[str, str | int] | None = None
    ) -> RestPayload:
        request_url = _with_query(url, params)
        cache = self._database.get_http_cache(request_url)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if cache is not None and cache.etag:
            headers["If-None-Match"] = cache.etag
        response = self._transport(request_url, headers)
        response_headers = _normalise_headers(response.headers)
        if response.status_code == 304:
            if cache is None:
                raise GithubResponseError(f"304 without cached body for {request_url}")
            merged_headers = dict(cache.headers)
            merged_headers.update(response_headers)
            self._secondary_attempts.pop(request_url, None)
            return RestPayload(
                value=_decode_json(cache.body, request_url),
                headers=merged_headers,
                url=request_url,
                from_cache=True,
            )
        if response.status_code in {403, 429}:
            raise self._rate_limit_error(request_url, response_headers)
        if response.status_code < 200 or response.status_code >= 300:
            message = _response_message(response.body)
            raise GithubResponseError(
                f"GitHub REST request failed ({response.status_code}) "
                f"for {request_url}: {message}"
            )
        body = _body_text(response.body)
        try:
            value = _decode_json(body, request_url)
        except GithubResponseError:
            raise
        self._database.save_http_cache(
            request_url,
            response_headers.get("etag"),
            body,
            response_headers,
        )
        self._secondary_attempts.pop(request_url, None)
        return RestPayload(
            value=value,
            headers=response_headers,
            url=request_url,
            from_cache=False,
        )

    def _rate_limit_error(self, url: str, headers: Mapping[str, str]) -> RateLimitError:
        remaining = _parse_int(headers.get("x-ratelimit-remaining"))
        if remaining == 0:
            reset = _parse_int(headers.get("x-ratelimit-reset"))
            until = (
                datetime.fromtimestamp(reset, UTC)
                if reset is not None
                else self._clock() + timedelta(minutes=1)
            )
            return RateLimitError(
                f"GitHub primary rate limit reached for {url}",
                until,
                primary=True,
            )
        retry_after = _parse_float(headers.get("retry-after"))
        if retry_after is None:
            attempt = self._secondary_attempts.get(url, 0) + 1
            self._secondary_attempts[url] = attempt
            retry_after = 60.0 * (2 ** (attempt - 1))
        until = self._clock() + timedelta(seconds=max(0.0, retry_after))
        return RateLimitError(
            f"GitHub secondary rate limit reached for {url}",
            until,
            primary=False,
        )


def _urlopen(url: str, headers: Mapping[str, str]) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return HttpResponse(
                status_code=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            status_code=error.code,
            headers=dict(error.headers.items()),
            body=error.read(),
        )


def _with_query(url: str, params: Mapping[str, str | int] | None) -> str:
    if not params:
        return url
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, str(value)) for key, value in params.items())
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _normalise_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _body_text(body: bytes | str) -> str:
    return body.decode("utf-8") if isinstance(body, bytes) else body


def _decode_json(body: bytes | str, url: str) -> Any:
    try:
        return json.loads(_body_text(body))
    except json.JSONDecodeError as error:
        raise GithubResponseError(f"GitHub returned invalid JSON for {url}") from error


def _response_message(body: bytes | str) -> str:
    text = _body_text(body).strip()
    if not text:
        return "empty response"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    if isinstance(value, dict):
        payload = cast(dict[str, Any], value)
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return text[:200]


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


_NEXT_LINK = re.compile(r"<([^>]+)>\s*;\s*rel\s*=\s*[\"']?next[\"']?", re.IGNORECASE)


def next_link(headers: Mapping[str, str]) -> str | None:
    value: str | None = headers.get("link")
    if value is None:
        value = headers.get("Link")
    if value is None:
        return None
    match = _NEXT_LINK.search(value)
    return match.group(1) if match else None
