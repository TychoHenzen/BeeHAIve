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
    parse_open_numbers,
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


REPO = Repository("o", "Card-Cleaner")


def test_parse_projects_keeps_the_open_project_named_after_the_repo() -> None:
    payload = json.dumps(
        [
            {"number": 2, "title": "card cleaner", "state": "open"},
            {"number": 3, "title": "CardCleaner", "state": "closed"},
            {"number": 4, "title": "Other", "state": "open"},
            {"title": "CardCleaner", "state": "open"},
            None,
        ]
    )

    assert parse_projects(payload, REPO, "users") == [
        ProjectLink(2, "card cleaner", "https://github.com/users/o/projects/2")
    ]
    assert parse_projects("not json", REPO, "users") == []


def test_parse_open_numbers_reads_one_number_per_line() -> None:
    assert parse_open_numbers("5\n7\n") == frozenset({5, 7})
    assert parse_open_numbers("") == frozenset()
    assert parse_open_numbers("5\noops\n") is None


class FakeRunner:
    """Answer git, the Projects lookups and the open issues lookup by name."""

    def __init__(self, outputs: dict[str, str | None]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        joined = " ".join(args)
        if args[0] == "git":
            key = "git"
        elif "projectsV2" in joined:
            key = joined.split()[2].split("/")[0]  # users or orgs
        else:
            key = "issues"
        self.calls.append(key)
        return self.outputs.get(key)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


PROJECTS = json.dumps([{"number": 9, "title": "Card-Cleaner", "state": "open"}])
LINK = ProjectLink(9, "Card-Cleaner", "https://github.com/orgs/o/projects/9")


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
    runner = FakeRunner({})
    clock = FakeClock()
    resolver = RepoResolver(runner, clock)

    assert resolver.repo_state(REPO) == RepoState([], None)
    assert resolver.repo_state(REPO) == RepoState([], None)
    assert runner.calls == ["users", "orgs", "issues"]

    runner.outputs.update(orgs=PROJECTS, issues="1\n7\n")
    clock.now += REFRESH_SECONDS + 1
    good = RepoState([LINK], frozenset({1, 7}))
    assert resolver.repo_state(REPO) == good
    assert resolver.repo_state(REPO) == good
    assert runner.calls.count("issues") == 2

    runner.outputs.update(orgs=None, issues=None)
    clock.now += REFRESH_SECONDS + 1
    assert resolver.repo_state(REPO) == good
    assert resolver.known_state(REPO) == good
    assert resolver.known_state(Repository("x", "y")) is None
    assert runner.calls.count("issues") == 3


def test_run_command_returns_none_on_failure() -> None:
    assert run_command(["definitely-not-a-real-command-beehaiive"]) is None
    assert run_command(["git", "--definitely-not-a-flag"]) is None
    output = run_command(["git", "--version"])
    assert output is not None and output.startswith("git version")
