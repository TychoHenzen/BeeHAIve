from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateDefinition:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    category: str
    required: bool
    external_only: bool
