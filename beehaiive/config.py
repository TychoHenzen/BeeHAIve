from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class CoreConfigurationError(ValueError):
    pass


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class CoreConfig:
    """Where to find Claude Code transcripts and how far back to look."""

    projects_dir: Path
    lookback_hours: float = 24.0
    port: int = 8000

    def __post_init__(self) -> None:
        if not 0 < self.lookback_hours <= 24 * 30:
            raise CoreConfigurationError(
                "BEEHAIIVE_LOOKBACK_HOURS must be between 0 and 720"
            )
        if not 1 <= self.port <= 65535:
            raise CoreConfigurationError("BEEHAIIVE_PORT must be a valid port")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: str | Path = ".env",
    ) -> CoreConfig:
        values = _load_environment(environ, Path(dotenv_path))
        raw_dir = values.get("BEEHAIIVE_CLAUDE_PROJECTS", "").strip()
        projects_dir = Path(raw_dir).expanduser() if raw_dir else default_projects_dir()
        return cls(
            projects_dir=projects_dir,
            lookback_hours=_number(values, "BEEHAIIVE_LOOKBACK_HOURS", 24.0),
            port=int(_number(values, "BEEHAIIVE_PORT", 8000)),
        )


def _number(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise CoreConfigurationError(f"{name} must be a number") from error


def _load_environment(
    environ: Mapping[str, str] | None, dotenv_path: Path
) -> dict[str, str]:
    """Merge a local .env file under the real environment, which wins."""
    values: dict[str, str] = {}
    if dotenv_path.is_file():
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.removeprefix("export ").split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    values.update(os.environ if environ is None else environ)
    return values
