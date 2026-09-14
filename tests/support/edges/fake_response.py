from __future__ import annotations

import json


class FakeResponse:
    def __init__(self, payload: object, headers: object | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")
