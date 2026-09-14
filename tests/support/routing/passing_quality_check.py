from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class PassingQualityCheck:
    name = "fixture-pass"

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.is_dir()
        return CheckResult(self.name, True, "fixture passed")
