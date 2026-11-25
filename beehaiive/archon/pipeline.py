"""Where every open issue of a repository stands, read through archon-home.

The pbi queues are workflows in archon-home, and `ledger.mjs` beside them says,
for one repository, whether each open issue is worked by a live run, waits in a
queue, waits on a question, or is stuck, and whether each queue can run. A read
takes a GitHub snapshot and most of a minute, so one read per repository runs
at a time, and its answer is kept for five minutes.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..records import Record, as_dict
from .cli import ArchonCliError, Runner, run_process

LEDGER = ("workflows", "dod-guard", "archon-pbi", "scripts", "ledger.mjs")
# What a pbi queue's trigger binding claims: pbi:github.com/<owner/name>:<queue>.
PBI_RESOURCE = re.compile(
    r"pbi:github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._][A-Za-z0-9._-]*):[a-z]+"
)
READ_TIMEOUT = 240.0
KEEP_FOR = 300.0


def pipelines(bindings: Sequence[Record]) -> dict[str, Path]:
    """Each repository a pbi queue binding serves, with the folder its queues run in.

    Only these repositories can be read, so a browser never names one freely.
    """
    found: dict[str, Path] = {}
    for binding in bindings:
        resource, cwd = binding.get("resource"), binding.get("cwd")
        match = PBI_RESOURCE.fullmatch(resource) if isinstance(resource, str) else None
        if match and isinstance(cwd, str) and cwd.strip():
            found.setdefault(match.group(1), Path(cwd))
    return found


class PipelineReader:
    def __init__(
        self,
        home: Path,
        runner: Runner = run_process,
        node: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.script = home.joinpath(*LEDGER)
        self._node = node or shutil.which("node") or "node"
        self._runner = runner
        self._clock = clock
        self._kept: dict[str, tuple[float, Record]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def kept(self, repository: str) -> Record | None:
        """The last reading, while it is under five minutes old."""
        kept = self._kept.get(repository)
        if kept is None or self._clock() - kept[0] >= KEEP_FOR:
            return None
        return kept[1]

    def read(self, repository: str, cwd: Path) -> Record:
        """A fresh reading. A request that waited while another read the same
        repository takes that reading instead of starting a second one."""
        asked = self._clock()
        with self._lock(repository):
            kept = self._kept.get(repository)
            if kept is not None and kept[0] >= asked:
                return kept[1]
            ledger = self._run(repository, cwd)
            self._kept[repository] = (self._clock(), ledger)
            return ledger

    def _lock(self, repository: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repository, threading.Lock())

    def _run(self, repository: str, cwd: Path) -> Record:
        if not self.script.is_file():
            raise ArchonCliError(
                "archon-home has no ledger.mjs, so the pipeline cannot be read.",
                str(self.script),
            )
        args = [self._node, str(self.script), "read", f"--repository={repository}"]
        folder = cwd if cwd.is_dir() else None
        result = self._runner(args, folder, READ_TIMEOUT)
        if not result.ok:
            raise ArchonCliError("The pipeline could not be read.", result.output)
        try:
            loaded: object = json.loads(result.stdout)
        except ValueError as error:
            raise ArchonCliError(
                "ledger.mjs did not print JSON.", result.output
            ) from error
        ledger = as_dict(loaded)
        if ledger is None:
            raise ArchonCliError(
                "ledger.mjs did not print a JSON object.", result.output
            )
        return ledger
