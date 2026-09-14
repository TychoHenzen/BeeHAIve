from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .check_result import CheckResult


class CheckSuite(Protocol):
    """Run a repository's checks and return one result per check."""

    def run(self, workspace: Path) -> tuple[CheckResult, ...]: ...
