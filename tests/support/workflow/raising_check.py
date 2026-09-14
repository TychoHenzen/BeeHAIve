from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class RaisingCheck:
    name = "raising"

    def run(self, workspace: Path) -> CheckResult:
        del workspace
        raise RuntimeError("fixture exploded")
