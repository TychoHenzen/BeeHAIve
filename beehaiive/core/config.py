from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class CoreConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class CoreConfig:
    owner: str
    owner_type: str
    project_number: int
    db_path: str | Path = ".beehaiive/hive.db"
    github_token: str = ""
    codex: str = "codex"
    skills_dirs: tuple[Path, ...] = ()
    refresh_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.owner_type not in {"user", "org"}:
            raise CoreConfigurationError(
                "BEEHAIIVE_PROJECT_OWNER_TYPE must be 'user' or 'org'"
            )
        if self.project_number <= 0:
            raise CoreConfigurationError("BEEHAIIVE_PROJECT_NUMBER must be positive")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: str | Path = ".env",
    ) -> CoreConfig:
        values = _load_environment(environ, Path(dotenv_path))
        owner = values.get("BEEHAIIVE_PROJECT_OWNER", "").strip()
        if not owner:
            raise CoreConfigurationError("BEEHAIIVE_PROJECT_OWNER is required")
        owner_type = values.get("BEEHAIIVE_PROJECT_OWNER_TYPE", "user").strip().lower()
        if owner_type not in {"user", "org"}:
            raise CoreConfigurationError(
                "BEEHAIIVE_PROJECT_OWNER_TYPE must be 'user' or 'org'"
            )
        raw_number = values.get("BEEHAIIVE_PROJECT_NUMBER", "").strip()
        try:
            project_number = int(raw_number)
        except ValueError as error:
            raise CoreConfigurationError(
                "BEEHAIIVE_PROJECT_NUMBER must be an integer"
            ) from error
        if project_number <= 0:
            raise CoreConfigurationError("BEEHAIIVE_PROJECT_NUMBER must be positive")
        raw_refresh = values.get("BEEHAIIVE_PROJECT_REFRESH_SECONDS", "60")
        try:
            refresh_seconds = max(60.0, float(raw_refresh))
        except ValueError as error:
            raise CoreConfigurationError(
                "BEEHAIIVE_PROJECT_REFRESH_SECONDS must be a number"
            ) from error
        token = values.get("GITHUB_TOKEN", "").strip()
        if not token:
            token = values.get("GH_TOKEN", "").strip()
        if not token:
            token = _gh_auth_token()
        skills_dirs = tuple(
            Path(value)
            for value in values.get("BEEHAIIVE_SKILLS_DIRS", "").split(os.pathsep)
            if value
        )
        return cls(
            owner=owner,
            owner_type=owner_type,
            project_number=project_number,
            db_path=values.get("BEEHAIIVE_DB") or ".beehaiive/hive.db",
            github_token=token,
            codex=values.get("BEEHAIIVE_CODEX", "codex") or "codex",
            skills_dirs=skills_dirs,
            refresh_seconds=refresh_seconds,
        )


def _load_environment(
    environ: Mapping[str, str] | None, dotenv_path: Path
) -> dict[str, str]:
    values = dict(_read_dotenv(dotenv_path))
    values.update(dict(os.environ if environ is None else environ))
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _gh_auth_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CoreConfigurationError(
            "GITHUB_TOKEN is missing and gh auth token could not be read"
        ) from error
    token = result.stdout.strip()
    if not token:
        raise CoreConfigurationError("gh auth token returned an empty token")
    return token
