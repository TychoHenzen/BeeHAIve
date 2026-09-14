from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, cast

from ..provider import (
    GraphQLClient,
    ProviderError,
    UrllibGraphQLClient,
)


class GithubConfigurationMixin:
    def _token_from_environment(self: Any) -> str | None:
        return (
            self._token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        )

    def validate_configuration(self: Any) -> None:
        if self._client is None and not self._token_from_environment():
            raise ProviderError(
                "Set GITHUB_TOKEN or GH_TOKEN for production pull-request reviews"
            )

    def _configured_client(self: Any) -> GraphQLClient:
        with self._lock:
            if self._client is None:
                token = self._token_from_environment()
                if not token:
                    raise ProviderError(
                        "Set GITHUB_TOKEN or GH_TOKEN for production "
                        "pull-request reviews"
                    )
                self._token = token
                self._client = UrllibGraphQLClient(token, self._endpoint)
            return self._client

    def _redact(self: Any, value: object) -> object:
        if isinstance(value, str):
            from ..agent import redact_worker_text

            secrets = tuple(
                secret
                for secret in (
                    self._token,
                    os.environ.get("GITHUB_TOKEN"),
                    os.environ.get("GH_TOKEN"),
                )
                if secret
            )
            return redact_worker_text(value, secrets, max_length=None)
        if isinstance(value, Mapping):
            mapping = cast(Mapping[str, object], value)
            return {key: self._redact(nested) for key, nested in mapping.items()}
        if isinstance(value, list):
            return [self._redact(nested) for nested in cast(list[object], value)]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise ProviderError("GitHub review evidence contains an unsupported value")
