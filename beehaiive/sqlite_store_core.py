from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any


class SQLiteStoreCoreMixin:
    def __init__(
        self: Any,
        database: str | Path,
        initialize: Callable[[], None],
    ) -> None:
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        try:
            initialize()
        except Exception:
            self._connection.close()
            raise

    def close(self: Any) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _sqlite_transaction(
        self: Any,
        before_yield: Callable[[sqlite3.Connection], None] | None = None,
    ) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if before_yield is not None:
                    before_yield(self._connection)
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()


__all__ = ["SQLiteStoreCoreMixin"]
