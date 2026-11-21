from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from ..sessions import SessionIndex

router = APIRouter(prefix="/api")


def _index(request: Request) -> SessionIndex:
    return cast(SessionIndex, request.app.state.index)


def _found(value: dict[str, object] | None, what: str) -> dict[str, object]:
    if value is None:
        raise HTTPException(status_code=404, detail=f"No recent {what} has that id.")
    return value


@router.get("/sessions")
def list_sessions(request: Request) -> dict[str, object]:
    return _index(request).list_sessions()


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, request: Request) -> dict[str, object]:
    return _found(_index(request).session_detail(session_id), "session")


@router.get("/sessions/{session_id}/agents/{agent_id}")
def agent_detail(session_id: str, agent_id: str, request: Request) -> dict[str, object]:
    return _found(_index(request).agent_detail(session_id, agent_id), "agent")
