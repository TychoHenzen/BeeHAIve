from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from beehaiive.repos import (
    REFRESH_SECONDS,
    ProjectLink,
    RepoResolver,
    Repository,
    RepoState,
    parse_remote,
    parse_repo_state,
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


def repo_payload(
    projects: list[object], issues: list[int], total_issues: int | None = None
) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "projectsV2": {"nodes": projects},
                    "issues": {
                        "totalCount": len(issues)
                        if total_issues is None
                        else total_issues,
                        "nodes": [{"number": n} for n in issues],
                    },
                    "pullRequests": {"totalCount": 1, "nodes": [{"number": 7}]},
                }
            }
        }
    )


def test_parse_repo_state_keeps_open_projects_and_open_numbers() -> None:
    payload = repo_payload(
        [
            {"number": 2, "title": "Board", "url": "https://p/2", "closed": False},
            {"number": 3, "title": "Old", "url": "https://p/3", "closed": True},
            {"number": 4, "title": "No url"},
            None,
        ],
        [5, 6],
    )

    assert parse_repo_state(payload) == RepoState(
        [ProjectLink(2, "Board", "https://p/2")], frozenset({5, 6, 7})
    )
    assert parse_repo_state("not json") is None
    assert parse_repo_state('{"data": null}') is None


def test_open_numbers_are_unknown_when_github_listed_only_some() -> None:
    state = parse_repo_state(repo_payload([], [5], total_issues=300))
    assert state is not None and state.open_numbers is None
    partial = json.dumps({"data": {"repository": {"projectsV2": {"nodes": []}}}})
    state = parse_repo_state(partial)
    assert state == RepoState([], None)


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


PAYLOAD = repo_payload([{"number": 9, "title": "T", "url": "u"}], [1])
STATE = RepoState([ProjectLink(9, "T", "u")], frozenset({1, 7}))


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


def test_resolver_repeats_the_github_lookup_and_keeps_the_last_good_one() -> None:
    runner = FakeRunner({"gh": None})
    clock = FakeClock()
    resolver = RepoResolver(runner, clock)
    repo = Repository("o", "r")

    assert resolver.repo_state(repo) is None
    assert resolver.repo_state(repo) is None
    assert runner.calls == ["gh"]

    runner.outputs["gh"] = PAYLOAD
    clock.now += REFRESH_SECONDS + 1
    assert resolver.repo_state(repo) == STATE
    assert resolver.repo_state(repo) == STATE
    assert runner.calls == ["gh", "gh"]

    runner.outputs["gh"] = None
    clock.now += REFRESH_SECONDS + 1
    assert resolver.repo_state(repo) == STATE
    assert resolver.known_state(repo) == STATE
    assert resolver.known_state(Repository("x", "y")) is None
    assert runner.calls == ["gh", "gh", "gh"]


def test_run_command_returns_none_on_failure() -> None:
    assert run_command(["definitely-not-a-real-command-beehaiive"]) is None
    assert run_command(["git", "--definitely-not-a-flag"]) is None
    output = run_command(["git", "--version"])
    assert output is not None and output.startswith("git version")
