"""Refuse requests that did not come from BeeHAIve's own page on this machine.

BeeHAIve can start, answer and cancel Archon runs and fire trigger bindings, so
a page on another site must not be able to make the browser do that. Two checks
stop it:

- The Host header must name this machine. That defeats DNS rebinding, where a
  hostile name is pointed at 127.0.0.1 to look same-origin.
- A request that changes something must carry the X-BeeHAIve header. A browser
  sends a custom header cross-origin only after a CORS preflight, which BeeHAIve
  never approves.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# The page sends it as X-BeeHAIve (web/console/api.js); Starlette lowercases names.
ACTION_HEADER = "x-beehaive"


def host_name(value: str) -> str:
    """The host part of a Host header or URL netloc, without the port."""
    return (urlsplit(f"//{value}").hostname or "").lower()


def refusal(method: str, headers: Headers) -> str | None:
    if host_name(headers.get("host", "")) not in LOCAL_HOSTS:
        return "BeeHAIve answers only on this machine's loopback address."
    origin = headers.get("origin")
    if origin is not None and host_name(urlsplit(origin).netloc) not in LOCAL_HOSTS:
        return "That page is not BeeHAIve."
    if method not in SAFE_METHODS and headers.get(ACTION_HEADER) != "1":
        return "An action must come from BeeHAIve's page."
    return None


class LocalRequestGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            reason = refusal(str(scope["method"]), Headers(scope=scope))
            if reason is not None:
                response = JSONResponse({"error": reason}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
