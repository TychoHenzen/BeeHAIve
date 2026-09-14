from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from threading import Lock
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from beehaiive.github.constants import (
    PROVIDER_REQUEST_TIMEOUT as PROVIDER_REQUEST_TIMEOUT,
)
from beehaiive.github.constants import (
    SECONDARY_RATE_LIMIT_FALLBACK_SECONDS as SECONDARY_RATE_LIMIT_FALLBACK_SECONDS,
)
from beehaiive.github.errors.outcome_unknown_error import (
    GitHubOutcomeUnknownError as GitHubOutcomeUnknownError,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.transport_helpers import _header_float as _header_float
from beehaiive.github.transport_helpers import (
    _rate_error_details as _rate_error_details,
)


class UrllibGraphQLClient:
    """Minimal GitHub GraphQL client using only the Python standard library."""

    def __init__(
        self,
        token: str,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._cooldown_lock = Lock()
        self._cooldown_until = 0.0
        self._cooldown_reset_at: float | None = None
        self._cooldown_retry_after: float | None = None
        self._cooldown_remaining: float | None = None
        self._cooldown_primary = False

    def _raise_if_cooling_down(self) -> None:
        now = time.time()
        with self._cooldown_lock:
            if self._cooldown_until <= now:
                return
            reset_at = self._cooldown_reset_at or self._cooldown_until
            retry_after = self._cooldown_retry_after
            remaining = self._cooldown_remaining
            primary = self._cooldown_primary
        raise GitHubRateLimitError(
            "GitHub GraphQL rate limit cooldown is active",
            reset_at=reset_at,
            retry_after=retry_after,
            primary=primary,
            remaining=remaining,
        )

    def _set_cooldown(
        self,
        *,
        reset_at: float | None,
        retry_after: float | None,
        primary: bool,
        remaining: float | None,
    ) -> float:
        now = time.time()
        valid_reset_at = (
            reset_at
            if reset_at is not None and math.isfinite(reset_at) and reset_at > now
            else None
        )
        valid_retry_after = (
            retry_after
            if retry_after is not None and math.isfinite(retry_after)
            else None
        )
        if primary and valid_reset_at is not None:
            cooldown_until = valid_reset_at
        elif valid_retry_after is not None:
            cooldown_until = now + max(valid_retry_after, 0.0)
        elif remaining is not None and remaining <= 0 and valid_reset_at is not None:
            cooldown_until = valid_reset_at
        else:
            cooldown_until = now + SECONDARY_RATE_LIMIT_FALLBACK_SECONDS

        with self._cooldown_lock:
            if cooldown_until > self._cooldown_until:
                self._cooldown_until = cooldown_until
                self._cooldown_reset_at = valid_reset_at
                self._cooldown_retry_after = valid_retry_after
                self._cooldown_remaining = (
                    remaining
                    if remaining is not None and math.isfinite(remaining)
                    else None
                )
                self._cooldown_primary = primary
            return self._cooldown_until

    def _rate_limit_error(
        self,
        response: object,
        errors: object = None,
        *,
        status_code: int | None = None,
    ) -> GitHubRateLimitError | None:
        details = _rate_error_details(errors)
        remaining = _header_float(response, "x-ratelimit-remaining")
        reset_at = _header_float(response, "x-ratelimit-reset")
        retry_after = _header_float(response, "retry-after")
        status_is_rate_limited = status_code == 429 or (
            status_code == 403
            and (retry_after is not None or (remaining is not None and remaining <= 0))
        )
        has_rate_limit_header = (
            remaining is not None and remaining <= 0 and errors is not None
        )
        if details is None and not has_rate_limit_header and not status_is_rate_limited:
            return None

        default_primary = status_code not in {403, 429}
        message, primary = details or (
            "GitHub GraphQL rate limit exceeded",
            default_primary,
        )
        cooldown_until = self._set_cooldown(
            reset_at=reset_at,
            retry_after=retry_after,
            primary=primary,
            remaining=remaining,
        )
        effective_reset_at = reset_at or cooldown_until
        kind = "primary" if primary else "secondary"
        return GitHubRateLimitError(
            f"GitHub GraphQL {kind} rate limit exceeded: {message}",
            reset_at=effective_reset_at,
            retry_after=retry_after,
            primary=primary,
            remaining=remaining,
        )

    def _record_exhausted_headers(self, response: object) -> None:
        remaining = _header_float(response, "x-ratelimit-remaining")
        if remaining is not None and remaining <= 0:
            self._set_cooldown(
                reset_at=_header_float(response, "x-ratelimit-reset"),
                retry_after=_header_float(response, "retry-after"),
                primary=True,
                remaining=remaining,
            )

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self._raise_if_cooling_down()
        request = Request(
            self._endpoint,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(
                "utf-8"
            ),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response_headers: object | None = None
        try:
            with urlopen(request, timeout=PROVIDER_REQUEST_TIMEOUT) as response:
                response_headers = response
                raw_payload: object = json.loads(response.read())
        except HTTPError as exc:
            rate_error = self._rate_limit_error(exc, status_code=exc.code)
            if rate_error is not None:
                raise rate_error from exc
            message = f"GitHub GraphQL request failed: {exc}"
            if 500 <= exc.code < 600:
                raise GitHubOutcomeUnknownError(message, status_code=exc.code) from exc
            raise ProviderError(message) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub GraphQL returned invalid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub GraphQL request failed: {exc}"
            ) from exc

        if not isinstance(raw_payload, dict):
            raise ProviderError("GitHub GraphQL returned a non-object response")
        payload = cast(dict[str, Any], raw_payload)
        errors = payload.get("errors")
        rate_error = self._rate_limit_error(response_headers, errors)
        if rate_error is not None:
            raise rate_error
        self._record_exhausted_headers(response_headers)
        if errors:
            raise ProviderError(f"GitHub GraphQL returned errors: {errors}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError("GitHub GraphQL response did not contain data")
        return cast(dict[str, Any], data)

    def request_rest(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, Any] | list[Any]]:
        """Call a GitHub REST endpoint without hiding its response status."""

        request = Request(
            f"{self._endpoint.rsplit('/graphql', 1)[0]}{path}",
            data=(
                json.dumps(dict(payload)).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        response_headers: object | None = None
        try:
            with urlopen(request, timeout=PROVIDER_REQUEST_TIMEOUT) as response:
                response_headers = response
                status = int(getattr(response, "status", 200))
                raw_payload = response.read()
        except HTTPError as exc:
            rate_error = self._rate_limit_error(exc, status_code=exc.code)
            if rate_error is not None:
                raise rate_error from exc
            status = exc.code
            response_headers = exc
            raw_payload = exc.read()
            if status == 408 or status >= 500:
                raise GitHubOutcomeUnknownError(
                    f"GitHub REST request failed with HTTP {status}",
                    status_code=status,
                ) from exc
        except OSError as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub REST request failed: {exc}"
            ) from exc

        try:
            decoded: object = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubOutcomeUnknownError(
                "GitHub REST returned invalid JSON", status_code=status
            ) from exc
        if not isinstance(decoded, (Mapping, list)):
            raise GitHubOutcomeUnknownError(
                "GitHub REST returned an invalid response", status_code=status
            )
        self._record_exhausted_headers(response_headers)
        return status, cast(Mapping[str, Any] | list[Any], decoded)


__all__ = ["UrllibGraphQLClient"]
