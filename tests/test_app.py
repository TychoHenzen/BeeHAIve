from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from beehaiive import __main__ as entrypoint
from beehaiive.app import create_app
from beehaiive.archon.settings import ArchonSettings
from beehaiive.config import CoreConfig
from beehaiive.repos import RepoResolver
from beehaiive.sessions import SessionIndex
from tests.archon_fakes import LOCAL
from tests.transcripts import at, human, tool_use, write_jsonl


def no_commands(_args: object) -> str | None:
    return None


def client_for(projects: Path) -> TestClient:
    index = SessionIndex(
        projects,
        timedelta(hours=1),
        resolver=RepoResolver(no_commands),
        clock=lambda: at(30),
    )
    write_jsonl(
        projects / "C--w" / "s1.jsonl",
        [human("hi", 0), tool_use("t1", "Bash", {"command": "ls"}, 1)],
    )
    index.refresh()
    app = create_app(CoreConfig(projects, archon=archon_settings(projects)), index)
    # No context manager: the lifespan would start the background thread.
    return TestClient(app, base_url=LOCAL)


def archon_settings(root: Path) -> ArchonSettings:
    return ArchonSettings(home=root / "archon", url="http://archon.test", binary=root)


def test_console_and_sessions_pages_and_assets_are_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    console = client.get("/")
    assert console.status_code == 200 and "/web/console/main.js" in console.text
    assert console.headers["cache-control"] == "no-cache"
    sessions = client.get("/sessions")
    assert sessions.status_code == 200 and "/web/app.js" in sessions.text
    script = client.get("/web/console/main.js")
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-cache"
    for asset in ("app.js", "format.js", "console/console.css", "console/api.js"):
        assert client.get(f"/web/{asset}").status_code == 200, asset
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/healthz").json() == {"status": "ok"}


def test_session_api_lists_and_details_sessions(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    listing = client.get("/api/sessions").json()
    assert listing["indexing"] is False
    assert [s["id"] for s in listing["sessions"]] == ["s1"]
    detail = client.get("/api/sessions/s1").json()
    assert detail["id"] == "s1" and len(detail["turns"]) == 1
    agent = client.get("/api/sessions/s1/agents/s1").json()
    assert [s["name"] for s in agent["steps"]] == ["Prompt", "Bash"]


def test_unknown_ids_answer_404(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    assert client.get("/api/sessions/nope").status_code == 404
    assert client.get("/api/sessions/s1/agents/nope").status_code == 404


def test_lifespan_starts_and_stops_the_index_and_closes_archon(
    tmp_path: Path,
) -> None:
    app = create_app(CoreConfig(tmp_path, archon=archon_settings(tmp_path)))
    with TestClient(app, base_url=LOCAL) as client:
        assert client.get("/api/sessions").status_code == 200
    index = app.state.index
    assert isinstance(index, SessionIndex)
    assert index._thread is None
    http = app.state.archon_http
    assert isinstance(http, httpx2.AsyncClient) and http.is_closed


def test_main_runs_uvicorn_on_localhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_run(_app: object, host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)
    monkeypatch.setenv("BEEHAIIVE_CLAUDE_PROJECTS", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_PORT", "8123")
    monkeypatch.chdir(tmp_path)

    entrypoint.main()

    assert calls == [("127.0.0.1", 8123)]
