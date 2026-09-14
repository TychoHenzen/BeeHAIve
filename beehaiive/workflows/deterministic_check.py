from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .check_result import CheckResult


class DeterministicCheck(Protocol):
    """A repeatable repository check used before a gate can continue."""

    name: str

    def run(self, workspace: Path) -> CheckResult: ...
