from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class FailingCheck:
    name = "fixture"

    def run(self, workspace: Path) -> CheckResult:
        return CheckResult(self.name, False, "gate failed")
