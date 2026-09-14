from __future__ import annotations

import json
import subprocess
from pathlib import Path

from beehaiive.quality_gates import MANIFEST_NAME

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def make_gate(
    name: str,
    argv: list[str],
    *,
    required: bool = True,
    external_only: bool = False,
    timeout_seconds: int | float = 10,
    category: str = "tests",
) -> dict[str, object]:
    return {
        "name": name,
        "argv": argv,
        "timeout_seconds": timeout_seconds,
        "category": category,
        "required": required,
        "external_only": external_only,
    }


def write_manifest(workspace: Path, gates: list[dict[str, object]]) -> None:
    (workspace / MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "gates": gates}), encoding="utf-8"
    )


def make_gate_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Quality Gate Tests"),
    ):
        subprocess.run(("git", *arguments), cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=path, check=True, capture_output=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path
