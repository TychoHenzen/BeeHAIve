from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


@dataclass
class FixtureCheck:
    name: str
    passed: bool = True
    evidence: str = "fixture passed"
    calls: int = 0

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.exists()
        self.calls += 1
        return CheckResult(self.name, self.passed, self.evidence)
