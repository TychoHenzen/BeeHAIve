from __future__ import annotations

import json
import os
from collections.abc import Mapping
from http.client import HTTPResponse
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .behavior import (
    BEHAVIOR_DEFINITION_SCHEMA,
    MAX_BEHAVIOR_PROMPT_LENGTH,
    BehaviorValidationError,
    normalize_definition,
)


class BehaviorModelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BehaviorModelClient(Protocol):
    def generate(self, prompt: str) -> object:
        """Return one structured behavior object for a prompt."""


class StructuredBehaviorModelClient(Protocol):
    def generate_structured(self, prompt: str, schema: Mapping[str, object]) -> object:
        """Return one object constrained by the supplied JSON schema."""


class OllamaBehaviorModelClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        configured_url = base_url or os.environ.get(
            "BEEHAIIVE_OLLAMA_URL", "http://127.0.0.1:11434"
        )
        parsed = urlparse(configured_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("The behavior model must use a local Ollama URL")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("Behavior model timeout must be between 0 and 30 seconds")
        self._url = configured_url.rstrip("/") + "/api/chat"
        self._model = (
            model or os.environ.get("BEEHAIIVE_OLLAMA_MODEL", "llama3.2").strip()
        )
        if not self._model or len(self._model) > 128:
            raise ValueError("A behavior model name is required")
        self._timeout_seconds = timeout_seconds

    def generate(self, prompt: str) -> object:
        try:
            response = self.generate_structured(prompt, BEHAVIOR_DEFINITION_SCHEMA)
            return normalize_definition(response)
        except BehaviorValidationError as exc:
            raise BehaviorModelError(
                "invalid_response", "The behavior model returned malformed JSON"
            ) from exc

    def generate_structured(self, prompt: str, schema: Mapping[str, object]) -> object:
        if not prompt.strip():
            raise BehaviorModelError("invalid_prompt", "A behavior prompt is required")
        if len(prompt) > MAX_BEHAVIOR_PROMPT_LENGTH:
            raise BehaviorModelError(
                "invalid_prompt", "The behavior prompt is too long"
            )
        request = Request(
            self._url,
            data=json.dumps(
                {
                    "model": self._model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Return only one JSON behavior definition. "
                                "Use the allowed bounded action schema."
                            ),
                        },
                        {"role": "user", "content": prompt.strip()},
                    ],
                    "format": dict(schema),
                    "stream": False,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = cast(HTTPResponse, response).read(1_000_001)
        except HTTPError as exc:
            raise BehaviorModelError(
                "model_unavailable", "The local behavior model rejected the request"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BehaviorModelError(
                "model_unavailable", "The local behavior model is unavailable"
            ) from exc
        if len(payload) > 1_000_000:
            raise BehaviorModelError(
                "response_too_large", "The behavior model response is too large"
            )
        try:
            response_data = json.loads(payload.decode("utf-8"))
            if not isinstance(response_data, Mapping):
                raise ValueError("response is not an object")
            response_mapping = cast(Mapping[str, object], response_data)
            message = response_mapping.get("message")
            message_mapping = (
                cast(Mapping[str, object], message)
                if isinstance(message, Mapping)
                else None
            )
            content = (
                message_mapping.get("content") if message_mapping is not None else None
            )
            if isinstance(content, str):
                return json.loads(content)
            elif isinstance(content, Mapping):
                return dict(cast(Mapping[str, object], content))
            else:
                raise ValueError("message content is not JSON")
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BehaviorModelError(
                "invalid_response", "The behavior model returned malformed JSON"
            ) from exc


__all__ = [
    "BehaviorModelClient",
    "BehaviorModelError",
    "OllamaBehaviorModelClient",
    "StructuredBehaviorModelClient",
]
