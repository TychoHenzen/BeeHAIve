from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from beehaiive.archon.cli import ArchonCli, ArchonCliError, CliResult, run_process
from beehaiive.archon.settings import ArchonSettings, default_home, read_json_object
from tests.archon_fakes import FakeRunner, printed


def test_settings_read_the_port_and_binary_from_the_archon_home(
    tmp_path: Path,
) -> None:
    (tmp_path / "server.json").write_text(json.dumps({"port": 4100}), "utf-8")
    (tmp_path / "install.json").write_text(json.dumps({"binary": "C:/a.exe"}), "utf-8")

    settings = ArchonSettings.from_values({"BEEHAIIVE_ARCHON_HOME": str(tmp_path)})

    assert settings.url == "http://127.0.0.1:4100"
    assert settings.binary == Path("C:/a.exe")
    assert settings.triggers_dir == tmp_path / "triggers"
    assert settings.is_default_home is False


def test_explicit_settings_win_over_the_archon_home(tmp_path: Path) -> None:
    settings = ArchonSettings.from_values(
        {
            "ARCHON_HOME": str(tmp_path),
            "BEEHAIIVE_ARCHON_URL": "http://box:9000/",
            "BEEHAIIVE_ARCHON_BIN": str(tmp_path / "custom"),
        }
    )

    assert settings.home == tmp_path
    assert settings.url == "http://box:9000"
    assert settings.binary == tmp_path / "custom"


def test_bad_or_missing_files_fall_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / "server.json").write_text(json.dumps({"port": True}), "utf-8")
    (tmp_path / "install.json").write_text("not json", "utf-8")

    settings = ArchonSettings.from_values({"ARCHON_HOME": str(tmp_path)})

    assert settings.url == "http://127.0.0.1:3090"
    assert settings.binary == tmp_path / "bin" / "archon"


def test_no_values_means_the_default_home() -> None:
    settings = ArchonSettings.from_values({})

    assert settings.home == default_home()
    assert settings.is_default_home is True


def test_read_json_object_refuses_anything_but_an_object(tmp_path: Path) -> None:
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2]", "utf-8")

    assert read_json_object(listed) is None
    assert read_json_object(tmp_path / "missing.json") is None


def test_cli_result_joins_both_streams() -> None:
    assert CliResult(0, " out \n", "").output == "out"
    assert CliResult(2, "out", "err").output == "out\nerr"
    assert CliResult(2, "", "").ok is False


def test_cli_prefixes_the_binary_and_parses_json() -> None:
    runner = FakeRunner({"trigger whoami": printed({"runAsUserId": "u1"})})
    cli = ArchonCli(Path("archon"), runner=runner)

    assert cli.json(["trigger", "whoami"]) == {"runAsUserId": "u1"}
    assert runner.calls == [["archon", "trigger", "whoami"]]


def test_cli_failure_names_the_first_real_line() -> None:
    notice = "[archon] stripped 5 keys from .env\n"
    runner = FakeRunner({"list": CliResult(1, "", notice + "Database locked\nmore")})
    cli = ArchonCli(Path("archon"), runner=runner)

    with pytest.raises(ArchonCliError, match="Database locked") as caught:
        cli.json(["trigger", "list"])
    assert "more" in caught.value.detail


def test_cli_failure_without_output_still_says_so() -> None:
    cli = ArchonCli(Path("archon"), runner=FakeRunner({"x": CliResult(1, "", "")}))

    with pytest.raises(ArchonCliError, match="archon failed"):
        cli.json(["x"])


def test_cli_refuses_output_that_is_not_json() -> None:
    cli = ArchonCli(Path("archon"), runner=FakeRunner({"x": CliResult(0, "hi", "")}))

    with pytest.raises(ArchonCliError, match="did not print JSON"):
        cli.json(["x"])


def test_run_process_captures_a_real_program() -> None:
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        None,
        30.0,
    )

    assert result.ok and result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_run_process_reports_a_missing_program(tmp_path: Path) -> None:
    with pytest.raises(ArchonCliError, match="was not found"):
        run_process([str(tmp_path / "nothing.exe")], None, 5.0)


def test_run_process_stops_a_program_that_hangs() -> None:
    with pytest.raises(ArchonCliError, match="did not finish"):
        run_process([sys.executable, "-c", "import time; time.sleep(10)"], None, 0.5)
