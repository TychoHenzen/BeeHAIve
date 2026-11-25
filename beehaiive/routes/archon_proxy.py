"""Pass /archon/api/* through to Archon's own server, streams included.

The page talks to one origin, so it needs no CORS and the request guard covers
every action it takes. Archon's API is forwarded as is: BeeHAIve does not
reinterpret runs, it only adds what the API lacks (see archon_tools).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import cast
from urllib.parse import quote

import httpx2
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter()
logger = logging.getLogger(__name__)

REQUEST_HEADERS = ("accept", "content-type", "last-event-id")
RESPONSE_HEADERS = ("cache-control", "content-disposition", "content-type")
# Archon streams live events; such a request may stay open for hours.
STREAM_TIMEOUT = httpx2.Timeout(10.0, read=None)
CALL_TIMEOUT = httpx2.Timeout(10.0, read=120.0)


def _client(request: Request) -> httpx2.AsyncClient:
    return cast(httpx2.AsyncClient, request.app.state.archon_http)


async def _relay(upstream: httpx2.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()


@router.api_route(
    "/archon/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def forward(path: str, request: Request) -> Response:
    streaming = path.startswith("stream/") or path.endswith("/stream")
    # FastAPI hands over the path decoded. Encoding it again keeps an escaped
    # "#", "?" or "%" in an artifact name from reading as URL syntax upstream.
    upstream_request = _client(request).build_request(
        request.method,
        f"/api/{quote(path, safe='/')}",
        params=request.url.query,
        headers={k: v for k in REQUEST_HEADERS if (v := request.headers.get(k))},
        content=await request.body() or None,
        timeout=STREAM_TIMEOUT if streaming else CALL_TIMEOUT,
    )
    try:
        upstream = await _client(request).send(upstream_request, stream=True)
    except httpx2.HTTPError:
        # The library's own error text goes to BeeHAIve's log, not the reply.
        logger.warning("Archon's server did not answer.", exc_info=True)
        return JSONResponse(
            {"error": "Archon's server did not answer. BeeHAIve's log has why."},
            status_code=502,
        )
    headers = {k: v for k in RESPONSE_HEADERS if (v := upstream.headers.get(k))}
    if upstream.headers.get("content-type", "").startswith("text/event-stream"):
        headers["cache-control"] = "no-cache"
        return StreamingResponse(
            _relay(upstream), status_code=upstream.status_code, headers=headers
        )
    try:
        content = await upstream.aread()
    finally:
        await upstream.aclose()
    return Response(content, status_code=upstream.status_code, headers=headers)
