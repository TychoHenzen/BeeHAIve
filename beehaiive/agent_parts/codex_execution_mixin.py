from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from beehaiive.contracts import ContractError, TaskResult
from beehaiive.routing import AttemptOutcome, ModelExecution, ModelSpec, RoutingDecision
from beehaiive.storage import StoreError
from beehaiive.workflow import WorkflowError

from .worker_text import redact_worker_text as redact_worker_text


class CodexExecutionMixin:
    def execute(
        self: Any, spec: ModelSpec, decision: RoutingDecision
    ) -> ModelExecution:
        """Run the fixed safe task and return only its final agent message."""

        problem_id = decision.problem_id
        with self._lock:
            contract = self._task_contracts.get(problem_id)
            event_handler = self._session_event_handlers.get(problem_id)
            workspace_lease = self._workspace_leases.get(problem_id)
            validate_workspace_lease = self._workspace_validators.get(problem_id)
        execution_repository = (
            None if workspace_lease is None else Path(workspace_lease.worktree_path)
        )
        if workspace_lease is not None:
            try:
                if validate_workspace_lease is None:
                    raise WorkflowError("Workspace lease validator is missing")
                validate_workspace_lease()
            except (StoreError, WorkflowError) as exc:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context=f"Workflow lease validation failed: {exc}",
                )
            if execution_repository is None or not execution_repository.is_dir():
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Leased worker worktree does not exist",
                )
        prompt = self._prompt(
            spec, decision, contract, execution_repository, workspace_lease is not None
        )
        with self._lock:
            self._active_attempts.add(problem_id)
            if problem_id in self._cancelled:
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
        process: subprocess.Popen[str] | None = None
        try:
            checkout = (
                self._safe_checkout()
                if workspace_lease is None
                else nullcontext(execution_repository)
            )
            with checkout as execution_repository:
                assert execution_repository is not None
                command = (
                    self._command_for_execution(
                        prompt, spec.model, execution_repository
                    )
                    if workspace_lease is None
                    else self._workspace_command_for_execution(
                        prompt, spec.model, execution_repository
                    )
                )
                process = self._start_process(
                    command,
                    self._safe_environment(),
                )
                assert process is not None
                with self._lock:
                    self._processes[problem_id] = process
                    cancelled = problem_id in self._cancelled
                if cancelled:
                    self._terminate_process(process)
                try:
                    if event_handler is None:
                        stdout, stderr, timed_out = self._communicate_bounded(
                            process, self.timeout_seconds
                        )
                    else:

                        def record_line(line: str) -> None:
                            self._record_session_line(line, event_handler)

                        stdout, stderr, timed_out = self._communicate_bounded(
                            process, self.timeout_seconds, record_line
                        )
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    stdout, stderr = "", ""
                    timed_out = True
                if validate_workspace_lease is not None:
                    try:
                        validate_workspace_lease()
                    except (StoreError, WorkflowError) as exc:
                        return ModelExecution(
                            AttemptOutcome.FAILURE,
                            failure_context=f"Workflow lease validation failed: {exc}",
                        )
                if timed_out:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        failure_context=(
                            f"Agent timed out after {self.timeout_seconds:g} seconds"
                        ),
                    )
                with self._lock:
                    was_cancelled = problem_id in self._cancelled
                if was_cancelled:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        failure_context="Agent stopped by operator",
                    )

                result, input_tokens, output_tokens = self._parse_output(stdout)
                result = redact_worker_text(result, self._secret_values).strip()
                if process.returncode != 0:
                    detail = redact_worker_text(
                        stderr or stdout, self._secret_values
                    ).strip()
                    if not detail:
                        detail = f"codex exec exited with status {process.returncode}"
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        failure_context=f"Bounded agent failed: {detail}",
                        task_result=(
                            TaskResult.invalid(detail) if contract is not None else None
                        ),
                    )
                if not result:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        failure_context="Bounded agent returned no result",
                        task_result=(
                            TaskResult.invalid("Bounded agent returned no result")
                            if contract is not None
                            else None
                        ),
                    )
                task_result = None
                if contract is not None:
                    try:
                        task_result = TaskResult.from_payload(
                            json.loads(result), contract
                        )
                    except (ContractError, json.JSONDecodeError) as exc:
                        reason = f"Invalid structured task result: {exc}"
                        return ModelExecution(
                            AttemptOutcome.SUCCESS,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            failure_context=reason,
                            task_result=TaskResult.invalid(reason),
                        )
                return ModelExecution(
                    AttemptOutcome.SUCCESS,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    result=result,
                    task_result=task_result,
                )
        finally:
            with self._lock:
                if process is not None:
                    self._processes.pop(problem_id, None)
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)


__all__ = ["CodexExecutionMixin"]
