from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .check_result import CheckResult
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .deterministic_check import DeterministicCheck


class DeterministicCheckRunner:
    """Run configured checks in stable order and turn failures into evidence."""

    def __init__(self, checks: Iterable[DeterministicCheck]) -> None:
        self._checks = tuple(checks)
        names = [check.name for check in self._checks]
        if not names:
            raise WorkflowError("At least one deterministic check is required")
        if any(not name.strip() for name in names):
            raise WorkflowError("Every deterministic check needs a name")
        if len(set(names)) != len(names):
            raise WorkflowError("Deterministic check names must be unique")

    def run(self, workspace: Path) -> tuple[CheckResult, ...]:
        results: list[CheckResult] = []
        for check in self._checks:
            try:
                result = check.run(workspace)
                if result.name != check.name:
                    raise WorkflowError(
                        f"Check {check.name} returned evidence for {result.name}"
                    )
                if not result.evidence.strip():
                    results.append(
                        CheckResult(check.name, False, "Check returned no evidence")
                    )
                else:
                    results.append(result)
            except Exception as exc:
                results.append(
                    CheckResult(check.name, False, f"Check failed to run: {exc}")
                )
        return tuple(results)
