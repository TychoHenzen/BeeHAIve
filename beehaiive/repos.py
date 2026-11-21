"""Link a session's working folder to its GitHub repository and Projects."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .records import as_dict, as_list, get_int, get_str

Runner = Callable[[Sequence[str]], str | None]

_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)
# Open issues change while a run works, so the lookup is repeated this often.
REFRESH_SECONDS = 300.0
# GitHub is read over REST only: GraphQL has its own, much smaller budget.
_OWNER_KINDS = ("users", "orgs")


def run_command(args: Sequence[str]) -> str | None:
    """Run a local read-only command; any failure just means "unknown"."""
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    def issue_url(self, number: int) -> str:
        # GitHub redirects /issues/N to /pull/N when N is a pull request.
        return f"{self.url}/issues/{number}"


@dataclass(frozen=True)
class ProjectLink:
    number: int
    title: str
    url: str


@dataclass(frozen=True)
class RepoState:
    """What GitHub says about a repository: its Projects and what is open."""

    projects: list[ProjectLink]
    # Open issue and pull request numbers; None until GitHub has answered, so
    # nothing is ruled closed on a guess.
    open_numbers: frozenset[int] | None


def parse_remote(url: str) -> Repository | None:
    match = _GITHUB_REMOTE.match(url.strip())
    if not match:
        return None
    return Repository(match.group("owner"), match.group("name"))


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_projects(payload: str, repo: Repository, kind: str) -> list[ProjectLink]:
    """Keep the owner's open Projects that are named after the repository.

    REST cannot list the Projects linked to a repository, only an owner's.
    ASSUMPTION: each repository has a Project with the same name, as on
    TychoHenzen's account; a repository without one shows no Project.
    """
    try:
        data: object = json.loads(payload)
    except ValueError:
        return []
    links: list[ProjectLink] = []
    for item in as_list(data):
        project = as_dict(item)
        number = get_int(project, "number")
        title = get_str(project, "title") or ""
        if (
            number
            and get_str(project, "state") == "open"
            and _plain(title) == _plain(repo.name)
        ):
            url = f"https://github.com/{kind}/{repo.owner}/projects/{number}"
            links.append(ProjectLink(number, title, url))
    return links


def parse_open_numbers(output: str) -> frozenset[int] | None:
    """Read one number per line, as `gh api --jq '.[].number'` prints them."""
    lines = output.split()
    if not all(line.isdigit() for line in lines):
        return None
    return frozenset(int(line) for line in lines)


class RepoResolver:
    """Cache git remote and GitHub lookups, repeating the GitHub one now and then.

    repository() and repo_state() may run a command; the index thread calls
    them. The known_* methods only read the cache, so a request never waits on
    git or gh.
    """

    def __init__(
        self,
        run: Runner = run_command,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = run
        self._clock = clock
        self._lock = threading.Lock()
        self._repos: dict[str, Repository | None] = {}
        self._states: dict[str, tuple[float, RepoState | None]] = {}

    def repository(self, cwd: str | None) -> Repository | None:
        if not cwd:
            return None
        with self._lock:
            if cwd in self._repos:
                return self._repos[cwd]
        output = self._run(["git", "-C", cwd, "remote", "get-url", "origin"])
        repo = parse_remote(output) if output else None
        with self._lock:
            self._repos[cwd] = repo
        return repo

    def repo_state(self, repo: Repository) -> RepoState | None:
        key = repo.full_name
        now = self._clock()
        with self._lock:
            cached = self._states.get(key)
        if cached is not None and now - cached[0] < REFRESH_SECONDS:
            return cached[1]
        last = cached[1] if cached else None
        # A failed lookup keeps the last good answer for that part.
        projects = self._fetch_projects(repo)
        if projects is None:
            projects = last.projects if last else []
        numbers = self._fetch_open_numbers(repo)
        if numbers is None and last is not None:
            numbers = last.open_numbers
        state = RepoState(projects, numbers)
        with self._lock:
            self._states[key] = (now, state)
        return state

    def _fetch_projects(self, repo: Repository) -> list[ProjectLink] | None:
        # An owner is a user or an organization; only one of the two answers.
        for kind in _OWNER_KINDS:
            output = self._run(
                ["gh", "api", f"{kind}/{repo.owner}/projectsV2?per_page=100"]
            )
            if output is not None:
                return parse_projects(output, repo, kind)
        return None

    def _fetch_open_numbers(self, repo: Repository) -> frozenset[int] | None:
        # The issues list includes pull requests. --paginate reads every page,
        # so the set is complete or the command fails.
        output = self._run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo.full_name}/issues?state=open&per_page=100",
                "--jq",
                ".[].number",
            ]
        )
        return parse_open_numbers(output) if output is not None else None

    def known_repository(self, cwd: str | None) -> Repository | None:
        with self._lock:
            return self._repos.get(cwd) if cwd else None

    def known_state(self, repo: Repository) -> RepoState | None:
        with self._lock:
            cached = self._states.get(repo.full_name)
        return cached[1] if cached else None
