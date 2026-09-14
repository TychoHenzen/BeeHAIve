from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .worker_text import safe_worker_environment as safe_worker_environment


class CodexRepositoryMixin:
    def _safe_environment(self: Any) -> dict[str, str]:
        return safe_worker_environment()

    @contextmanager
    def _safe_checkout(self: Any) -> Generator[Path]:
        """Give the child only a temporary copy without local credentials."""

        with TemporaryDirectory(prefix="beehaiive-agent-") as directory:
            destination = Path(directory)
            for relative in self._repository_files():
                if not self._is_safe_repository_file(relative):
                    continue
                source = (self.repository / relative).resolve()
                try:
                    source.relative_to(self.repository)
                except ValueError:
                    continue
                if not source.is_file() or source.is_symlink():
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            yield destination

    def _repository_files(
        self: Any, repository: Path | None = None
    ) -> tuple[Path, ...]:
        source = self.repository if repository is None else repository.resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "ls-files", "-z"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            return tuple(
                Path(os.fsdecode(value))
                for value in result.stdout.split(b"\0")
                if value
            )
        return tuple(
            path.relative_to(source)
            for path in source.rglob("*")
            if path.is_file()
            and not any(
                part in {".git", ".beehaiive", "__pycache__"} for part in path.parts
            )
        )

    @staticmethod
    def _is_safe_repository_file(relative: Path) -> bool:
        name = relative.name.lower()
        return not (
            name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
            or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
            or any(
                marker in name
                for marker in (
                    "secret",
                    "credential",
                    "password",
                    "token",
                    "api-key",
                    "api_key",
                    "apikey",
                    "private-key",
                    "private_key",
                    "service-account",
                    "service_account",
                )
            )
            or relative.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
        )

    def _discover_repository_name(self: Any) -> str | None:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "config",
                    "--get",
                    "remote.origin.url",
                ],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        remote = result.stdout.strip()
        match = re.search(r"([^/:\s]+/[^/\s]+?)(?:\.git)?$", remote)
        return match.group(1) if match else None

    def _discover_repository_branch(self: Any, repository: Path | None = None) -> str:
        source = self.repository if repository is None else repository.resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "branch", "--show-current"],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return result.stdout.strip() or "unknown"


__all__ = ["CodexRepositoryMixin"]
