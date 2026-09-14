from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock

from .execution_lock_entry import ExecutionLockEntry


class ExecutionLockManager:
    def __init__(self) -> None:
        self._entries: dict[str, ExecutionLockEntry] = {}
        self._guard = Lock()

    @contextmanager
    def acquire(self, key: str) -> Generator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = ExecutionLockEntry()
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]
