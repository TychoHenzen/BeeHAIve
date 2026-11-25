from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter()
health_router = APIRouter()


def _page(name: str) -> FileResponse:
    # Without no-cache a browser may keep an old page after an upgrade and call
    # API routes that no longer exist.
    return FileResponse(
        WEB_DIR / name,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/", include_in_schema=False)
def console() -> FileResponse:
    return _page("index.html")


@router.get("/sessions", include_in_schema=False)
def sessions() -> FileResponse:
    return _page("sessions.html")


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@health_router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}
