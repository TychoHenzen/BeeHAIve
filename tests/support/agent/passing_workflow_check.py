from pathlib import Path

from beehaiive.workflow import (
    CheckResult,
)


class PassingWorkflowCheck:
    name = "tests"

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.exists()
        return CheckResult(self.name, True, "fixture passed")
