from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from beehaiive.routing import AttemptOutcome, ModelExecution

from .constants import MAX_AGENT_OUTPUT_BYTES
from .worker_text import redact_worker_text as redact_worker_text


class CodexRepairExecutionMixin:
    def execute_repair(
        self: Any,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        """Run one bounded write-enabled repair inside the leased worktree."""

        if not worktree.is_dir():
            return ModelExecution(
                AttemptOutcome.FAILURE,
                failure_context="Repair worktree does not exist",
            )
        prompt = (
            "BeeHAIve conflict repair.\n"
            f"Source branch: {source_branch}\n"
            f"Target branch: {target_branch}\n"
            "This is the exact leased repair worktree. Resolve every merge conflict "
            "left by the target integration. Work only inside this worktree. Do not "
            "push, force-push, access credentials, edit another checkout, or start "
            "another agent. Run the configured checks only when useful. Stage the "
            "resolved files and create one normal merge commit. Return a concise "
            "plain-text result after the commit succeeds."
        )
        return self.execute_scoped_repair(problem_id, worktree, prompt, self.model)

    def execute_scoped_repair(
        self: Any,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str | None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        """Run a bounded repair prompt in the exact leased worktree."""

        if not worktree.is_dir():
            return ModelExecution(
                AttemptOutcome.FAILURE,
                failure_context="Repair worktree does not exist",
            )
        prompt = redact_worker_text(prompt, self._secret_values, max_length=None)
        if len(prompt.encode("utf-8")) > MAX_AGENT_OUTPUT_BYTES:
            return ModelExecution(
                AttemptOutcome.FAILURE,
                failure_context="Repair prompt exceeds the size limit",
            )

        def cancellation_requested() -> bool:
            with self._lock:
                active_cancel = problem_id in self._cancelled
            return active_cancel or (cancelled is not None and cancelled())

        with self._lock:
            if problem_id in self._cancelled or (cancelled is not None and cancelled()):
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
            if problem_id in self._active_attempts:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Repair attempt is already active",
                )
            self._active_attempts.add(problem_id)
        process: subprocess.Popen[str] | None = None
        try:
            process = self._start_process(
                self._repair_command(prompt, worktree, model), self._safe_environment()
            )
            assert process is not None
            with self._lock:
                self._processes[problem_id] = process
            if cancellation_requested():
                self._terminate_process(process)
            try:
                stdout, stderr, timed_out = self._communicate_bounded(
                    process, self.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                stdout, stderr, timed_out = "", "", True
            if timed_out:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context=(
                        f"Repair agent timed out after {self.timeout_seconds:g} seconds"
                    ),
                )
            if cancellation_requested():
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
            result, input_tokens, output_tokens = self._parse_output(stdout)
            result = redact_worker_text(result, self._secret_values).strip()
            if process.returncode != 0:
                detail = redact_worker_text(stderr or stdout, self._secret_values)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    failure_context=(
                        "Bounded repair agent failed: "
                        f"{detail.strip() or 'process failed'}"
                    ),
                )
            return ModelExecution(
                AttemptOutcome.SUCCESS,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                result=result or "Conflict repair agent completed",
            )
        finally:
            with self._lock:
                if process is not None:
                    self._processes.pop(problem_id, None)
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)


__all__ = ["CodexRepairExecutionMixin"]
