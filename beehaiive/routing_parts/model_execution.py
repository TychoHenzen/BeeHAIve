from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..contracts import TaskResult

if TYPE_CHECKING:
    from .attempt_outcome import AttemptOutcome


@dataclass(frozen=True, slots=True)
class ModelExecution:
    """Measured result returned by one configured model invocation."""

    outcome: AttemptOutcome | str
    input_tokens: int = 0
    output_tokens: int = 0
    failure_context: str = ""
    recursive_spawn_depth: int = 0
    result: str = ""
    task_result: TaskResult | None = None

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("Model token usage must not be negative")
        if self.recursive_spawn_depth < 0:
            raise ValueError("Model recursive spawn depth must not be negative")
