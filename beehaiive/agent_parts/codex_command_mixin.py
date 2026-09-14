from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from beehaiive.contracts import TaskContract
from beehaiive.routing import ModelSpec, RoutingDecision

from .constants import _ROUTING_MODEL_ALIASES, DEMO_TASK_NAME


class CodexCommandMixin:
    def _prompt(
        self: Any,
        spec: ModelSpec,
        decision: RoutingDecision,
        contract: TaskContract | None = None,
        repository: Path | None = None,
        workspace_write: bool = False,
    ) -> str:
        branch = (
            self._discover_repository_branch()
            if repository is None
            else self._discover_repository_branch(repository)
        )
        tracked_file_count = len(
            self._repository_files()
            if repository is None
            else self._repository_files(repository)
        )
        with self._lock:
            task = self._session_tasks.get(decision.problem_id, self.task)
        scope = (
            "The exact leased worktree is the only writable path. Work only inside "
            "it. Do not access the network or credentials, modify another checkout, "
            "start other agents, commit, or push."
            if workspace_write
            else "The task is read-only. Do not report success unless the inspection "
            "completed."
        )
        prompt = (
            "BeeHAIve dashboard demo.\n"
            f"Task name: {DEMO_TASK_NAME}\n"
            f"Task: {task}\n"
            f"Repository identity: {self.repository_name or self.repository.name}\n"
            f"Verified current branch: {branch}\n"
            f"Verified tracked file count: {tracked_file_count}\n"
            f"Routing tier: {spec.tier.value}. Routing reason: {decision.reason}.\n"
            f"{scope}"
        )
        if contract is not None:
            prompt += (
                "\nThe following versioned task contract is authoritative. Return "
                "exactly one JSON object with outcome, evidence, artifact_refs, "
                "question, required_action, and validation_reason. Always include "
                "evidence as an object and artifact_refs as an array, even when "
                "empty. Use null for unused optional fields. Do not include answer. "
                "Add no markdown fences or extra text.\n"
                f"{json.dumps(contract.as_dict(), sort_keys=True)}"
            )
        return prompt

    def _command(
        self: Any,
        prompt: str,
        model: str | None = None,
        repository: Path | None = None,
        *,
        workspace_write: bool = False,
    ) -> list[str]:
        selected_model = model.strip() if model and model.strip() else self.model
        if self.model is None and selected_model in _ROUTING_MODEL_ALIASES:
            selected_model = None
        command = [
            self.executable,
            "--approve-for-me",
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
        ]
        if workspace_write:
            command.extend(
                (
                    "--strict-config",
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                    "-c",
                    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-c",
                    "agents.enabled=false",
                )
            )
        command.extend(
            [
                "--sandbox",
                "workspace-write" if workspace_write else "read-only",
                "--cd",
                str(repository or self.repository),
            ]
        )
        if selected_model is not None:
            command.extend(("--model", selected_model))
        command.append(prompt)
        return command

    def _command_for_execution(
        self: Any, prompt: str, model: str, repository: Path
    ) -> list[str]:
        from .executor import CodexExecModelExecutor

        executor_class: Any = cast(Any, type(self))
        if executor_class._command is not CodexExecModelExecutor._command:
            return self._command(prompt)
        return self._command(prompt, model, repository)

    def _workspace_command_for_execution(
        self: Any, prompt: str, model: str, repository: Path
    ) -> list[str]:
        from .executor import CodexExecModelExecutor

        return CodexExecModelExecutor._command(
            self, prompt, model, repository, workspace_write=True
        )

    def _repair_command(
        self: Any,
        prompt: str,
        repository: Path,
        model: str | None = None,
    ) -> list[str]:
        selected_model = self.model if model is None else model
        command = [
            self.executable,
            "--approve-for-me",
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "agents.enabled=false",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(repository),
        ]
        if selected_model is not None:
            command.extend(("--model", selected_model))
        command.append(prompt)
        return command


__all__ = ["CodexCommandMixin"]
