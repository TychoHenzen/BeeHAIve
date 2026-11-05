from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from beehaiive.repos import (
    RETRY_SECONDS,
    ProjectLink,
    RepoResolver,
    Repository,
    parse_projects,
    parse_remote,
    run_command,
)


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/TychoHenzen/BeeHAIve.git",
        "https://github.com/TychoHenzen/BeeHAIve",
        "git@github.com:TychoHenzen/BeeHAIve.git",
        "ssh://git@github.com/TychoHenzen/BeeHAIve/\n",
    ],
)
def test_parse_remote_accepts_github_forms(remote: str) -> None:
    repo = parse_remote(remote)

    assert repo == Repository("TychoHenzen", "BeeHAIve")
    assert repo is not None
    assert repo.url == "https://github.com/TychoHenzen/BeeHAIve"
    assert repo.issue_url(4) == "https://github.com/TychoHenzen/BeeHAIve/issues/4"


def test_parse_remote_rejects_other_hosts() -> None:
    assert parse_remote("https://gitlab.com/a/b.git") is None


def test_parse_projects_keeps_open_projects_with_urls() -> None:
    payload = json.dumps(
        {
            "data": {
                "repository": {
                    "projectsV2": {
                        "nodes": [
                            {
                                "number": 2,
                                "title": "Board",
                                "url": "https://p/2",
                                "closed": False,
                            },
                            {
                                "number": 3,
                                "title": "Old",
                                "url": "https://p/3",
                                "closed": True,
                            },
                            {"number": 4, "title": "No url"},
                            None,
                        ]
                    }
                }
            }
        }
    )

    assert parse_projects(payload) == [ProjectLink(2, "Board", "https://p/2")]
    assert parse_projects("not json") == []
    assert parse_projects('{"data": null}') == []


class FakeRunner:
    def __init__(self, outputs: dict[str, str | None]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(args[0])
        return self.outputs.get(args[0])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


PROJECTS = json.dumps(
    {
        "data": {
            "repository": {
                "projectsV2": {"nodes": [{"number": 9, "title": "T", "url": "u"}]}
            }
        }
    }
)


def test_resolver_caches_repository_lookups() -> None:
    runner = FakeRunner({"git": "git@github.com:o/r.git\n"})
    resolver = RepoResolver(runner)

    assert resolver.known_repository("C:/w") is None
    assert resolver.repository("C:/w") == Repository("o", "r")
    assert resolver.repository("C:/w") == Repository("o", "r")
    assert resolver.known_repository("C:/w") == Repository("o", "r")
    assert resolver.repository(None) is None
    assert resolver.known_repository(None) is None
    assert runner.calls == ["git"]


def test_resolver_remembers_a_folder_without_a_github_remote() -> None:
    runner = FakeRunner({})
    resolver = RepoResolver(runner)

    assert resolver.repository("C:/w") is None
    assert resolver.repository("C:/w") is None
    assert runner.calls == ["git"]


def test_resolver_caches_projects_and_retries_failures_later() -> None:
    runner = FakeRunner({"gh": None})
    clock = FakeClock()
    resolver = RepoResolver(runner, clock)
    repo = Repository("o", "r")

    assert resolver.projects(repo) == []
    assert resolver.projects(repo) == []
    assert runner.calls == ["gh"]

    runner.outputs["gh"] = PROJECTS
    clock.now += RETRY_SECONDS + 1
    assert resolver.projects(repo) == [ProjectLink(9, "T", "u")]
    assert resolver.projects(repo) == [ProjectLink(9, "T", "u")]
    assert resolver.known_projects(repo) == [ProjectLink(9, "T", "u")]
    assert resolver.known_projects(Repository("x", "y")) == []
    assert runner.calls == ["gh", "gh"]


def test_run_command_returns_none_on_failure() -> None:
    assert run_command(["definitely-not-a-real-command-beehaiive"]) is None
    assert run_command(["git", "--definitely-not-a-flag"]) is None
    output = run_command(["git", "--version"])
    assert output is not None and output.startswith("git version")
