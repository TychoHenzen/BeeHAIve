from __future__ import annotations

from threading import Lock


class ExecutionLockEntry:
    def __init__(self) -> None:
        self.lock = Lock()
        self.users = 0
