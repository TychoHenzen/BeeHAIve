from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class FixtureCheck:
    name = "fixture"

    def run(self, workspace: Path) -> CheckResult:
        return CheckResult(self.name, workspace.exists(), "fixture checked")
