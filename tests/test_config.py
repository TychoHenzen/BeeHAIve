from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.config import CoreConfig, CoreConfigurationError, default_projects_dir


def test_defaults_point_at_claude_projects(tmp_path: Path) -> None:
    config = CoreConfig.from_environment({}, tmp_path / "missing.env")

    assert config.projects_dir == default_projects_dir()
    assert config.lookback_hours == 24.0
    assert config.port == 8000


def test_dotenv_values_load_and_the_environment_wins(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n"
        "\n"
        "not a setting\n"
        "export BEEHAIIVE_CLAUDE_PROJECTS='~/transcripts'\n"
        'BEEHAIIVE_LOOKBACK_HOURS="6"\n'
        "BEEHAIIVE_PORT=9000\n",
        encoding="utf-8",
    )

    config = CoreConfig.from_environment({"BEEHAIIVE_PORT": "9100"}, dotenv)

    assert config.projects_dir == Path("~/transcripts").expanduser()
    assert config.lookback_hours == 6.0
    assert config.port == 9100


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"BEEHAIIVE_LOOKBACK_HOURS": "soon"}, "must be a number"),
        ({"BEEHAIIVE_LOOKBACK_HOURS": "0"}, "between 0 and 720"),
        ({"BEEHAIIVE_LOOKBACK_HOURS": "721"}, "between 0 and 720"),
        ({"BEEHAIIVE_PORT": "70000"}, "valid port"),
    ],
)
def test_bad_values_are_rejected(
    tmp_path: Path, environ: dict[str, str], message: str
) -> None:
    with pytest.raises(CoreConfigurationError, match=message):
        CoreConfig.from_environment(environ, tmp_path / "missing.env")
