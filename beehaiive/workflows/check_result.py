from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Evidence produced by one deterministic check."""

    name: str
    passed: bool
    evidence: str
    status: str = ""
    category: str = "workflow"
    required: bool = True
    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.status:
            object.__setattr__(self, "status", "passed" if self.passed else "failed")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
            "status": self.status,
            "category": self.category,
            "required": self.required,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }
