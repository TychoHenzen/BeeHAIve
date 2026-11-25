from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..records import Record, as_dict

DEFAULT_PORT = 3090


@dataclass(frozen=True)
class ArchonSettings:
    """Where the local Archon install lives and how to reach its server."""

    home: Path
    url: str
    binary: Path

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> ArchonSettings:
        home = _path(values, "BEEHAIIVE_ARCHON_HOME") or _path(values, "ARCHON_HOME")
        home = home or default_home()
        url = values.get("BEEHAIIVE_ARCHON_URL", "").strip().rstrip("/")
        # server.json names 0.0.0.0 as the bind address; this machine reaches
        # it on the loopback address.
        url = url or f"http://127.0.0.1:{_server_port(home)}"
        binary = _path(values, "BEEHAIIVE_ARCHON_BIN") or _installed_binary(home)
        return cls(home=home, url=url, binary=binary)

    @property
    def triggers_dir(self) -> Path:
        return self.home / "triggers"

    @property
    def is_default_home(self) -> bool:
        return self.home == default_home()


def default_home() -> Path:
    return Path.home() / ".archon"


def read_json_object(path: Path) -> Record | None:
    """The JSON object in a file, or None when it is missing or not an object."""
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return as_dict(loaded)


def _path(values: Mapping[str, str], name: str) -> Path | None:
    raw = values.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _server_port(home: Path) -> int:
    port = (read_json_object(home / "server.json") or {}).get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def _installed_binary(home: Path) -> Path:
    binary = (read_json_object(home / "install.json") or {}).get("binary")
    if isinstance(binary, str) and binary.strip():
        return Path(binary)
    return home / "bin" / "archon"
