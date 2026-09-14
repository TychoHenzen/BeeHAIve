from __future__ import annotations

from .stage import Stage


def project_stage_from_status(status: str | None) -> Stage | None:
    normalized = (status or "").strip().lower()
    return {
        "backlog": Stage.BACKLOG,
        "todo": Stage.REFINE,
        "in progress": Stage.IMPLEMENT,
    }.get(normalized)


def _empty_metadata() -> dict[str, object]:
    return {}


__all__ = ["project_stage_from_status", "_empty_metadata"]
