from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

import beehaiive.__main__ as main_module
import beehaiive.app as app_module
import beehaiive.config as config_module
from beehaiive.config import CoreConfig, CoreConfigurationError
from tests.support import config

DOTENV_LINES = (
    "export BEEHAIIVE_PROJECT_OWNER=dotenv-owner",
    "BEEHAIIVE_PROJECT_OWNER_TYPE='org'",
    "BEEHAIIVE_PROJECT_NUMBER=7",
    "GITHUB_TOKEN='dotenv-token'",
    "BEEHAIIVE_DB='custom.db'",
    "BEEHAIIVE_CODEX='codex-custom'",
    "BEEHAIIVE_CODEX_ARGS=--model 'fast model'",
    f"BEEHAIIVE_SKILLS_DIRS=one{os.pathsep}two",
    "BEEHAIIVE_PROJECT_REFRESH_SECONDS=120",
    "BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS=12",
    "ignored line",
)

BASE_VALUES = {
    "BEEHAIIVE_PROJECT_OWNER": "owner",
    "BEEHAIIVE_PROJECT_NUMBER": "2",
    "GITHUB_TOKEN": "token",
}

INVALID_ENVIRONMENTS = (
    ({"BEEHAIIVE_PROJECT_OWNER_TYPE": "team"}, "OWNER_TYPE"),
    ({"BEEHAIIVE_PROJECT_NUMBER": "not-a-number"}, "must be an integer"),
    ({"BEEHAIIVE_PROJECT_REFRESH_SECONDS": "nope"}, "REFRESH"),
    ({"BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS": "nope"}, "AGENT_STEP_TIMEOUT"),
    ({"BEEHAIIVE_CODEX_ARGS": "'unterminated"}, "shell-like"),
)

INVALID_FIELDS = (
    ({"owner_type": "team"}, "OWNER_TYPE"),
    ({"project_number": 0}, "positive"),
    ({"agent_step_timeout_seconds": 901}, "between 1 and 900"),
)

DOTENV_OVERRIDES = {
    "BEEHAIIVE_PROJECT_OWNER": "override-owner",
    "BEEHAIIVE_PROJECT_NUMBER": "9",
}

VALID_FIELDS = {
    "owner": "owner",
    "owner_type": "user",
    "project_number": 2,
}


def test_config_reads_dotenv_with_environment_overrides(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("\n".join(DOTENV_LINES), encoding="utf-8")
    loaded = CoreConfig.from_environment(environ=DOTENV_OVERRIDES, dotenv_path=dotenv)
    assert loaded.owner == "override-owner"
    assert loaded.owner_type == "org"
    assert loaded.project_number == 9
    assert loaded.github_token == "dotenv-token"
    assert loaded.codex_args == ("--model", "fast model")
    assert loaded.skills_dirs == (Path("one"), Path("two"))
    assert loaded.refresh_seconds == 120
    assert loaded.agent_step_timeout_seconds == 12


def test_config_falls_back_to_the_gh_auth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_module, "_gh_auth_token", lambda: "gh-token")
    fallback = CoreConfig.from_environment(
        environ={
            "BEEHAIIVE_PROJECT_OWNER": "owner",
            "BEEHAIIVE_PROJECT_NUMBER": "2",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert fallback.github_token == "gh-token"


@pytest.mark.parametrize(
    ("overrides", "message"),
    INVALID_ENVIRONMENTS,
    ids=[
        "owner-type",
        "project-number",
        "refresh-seconds",
        "step-timeout",
        "codex-args",
    ],
)
def test_config_rejects_invalid_environment_values(
    overrides: dict[str, str], message: str, tmp_path: Path
) -> None:
    values = BASE_VALUES | overrides
    with pytest.raises(CoreConfigurationError, match=message):
        CoreConfig.from_environment(values, dotenv_path=tmp_path / "missing.env")


@pytest.mark.parametrize(
    ("overrides", "message"),
    INVALID_FIELDS,
    ids=["owner-type", "project-number", "step-timeout"],
)
def test_config_rejects_invalid_fields(overrides: dict[str, Any], message: str) -> None:
    values = VALID_FIELDS | overrides
    with pytest.raises(CoreConfigurationError, match=message):
        CoreConfig(**values)


def test_environment_rejects_unknown_owner_type() -> None:
    with pytest.raises(CoreConfigurationError, match="OWNER_TYPE"):
        CoreConfig.from_environment(
            environ=BASE_VALUES | {"BEEHAIIVE_PROJECT_OWNER_TYPE": "team"},
            dotenv_path=Path("missing.env"),
        )


def test_entrypoint_calls_uvicorn_on_the_core_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_value = config()
    monkeypatch.setattr(
        main_module.CoreConfig,
        "from_environment",
        classmethod(lambda _cls: config_value),
    )
    monkeypatch.setattr(main_module, "create_app", lambda value: value)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda application, **options: calls.append((application, options)),
    )
    main_module.main()
    assert calls == [
        (
            config_value,
            {"host": "127.0.0.1", "port": 8000},
        )
    ]
    monkeypatch.setattr(app_module, "create_app", lambda value: value)
    monkeypatch.delitem(sys.modules, "beehaiive.__main__")
    runpy.run_module("beehaiive.__main__", run_name="__main__")
    assert len(calls) == 2


def test_config_gh_token_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    result = type("Result", (), {"stdout": " gh-token \n"})()
    monkeypatch.setattr(
        config_module.subprocess,
        "run",
        lambda *_args, **_kwargs: result,
    )
    assert config_module._gh_auth_token() == "gh-token"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise config_module.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(config_module.subprocess, "run", fail)
    with pytest.raises(CoreConfigurationError, match="gh auth token"):
        config_module._gh_auth_token()
