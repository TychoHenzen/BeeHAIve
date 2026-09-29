from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

_DOCS_DIRECTORY = Path(__file__).resolve().parents[3] / "docs"


def docs_asset(relative_path: str | Path, *, media_type: str) -> FileResponse:
    return FileResponse(_DOCS_DIRECTORY / relative_path, media_type=media_type)


__all__ = ["docs_asset"]
