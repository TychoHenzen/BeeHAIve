from __future__ import annotations

from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class PassingCheck:
    name = "fixture"

    def run(self, _repository: Path) -> CheckResult:
        return CheckResult("fixture", True, "passed")
