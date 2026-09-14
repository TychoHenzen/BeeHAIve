from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reader_status import ReaderStatus


@dataclass(frozen=True, slots=True)
class ReaderExecution:
    """Result returned by one specialized reader double or adapter."""

    status: ReaderStatus | str
    findings: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
