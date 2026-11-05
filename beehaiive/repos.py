"""Link a session's working folder to its GitHub repository and Projects."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .records import as_dict, as_list, dig, get_int, get_str

Runner = Callable[[Sequence[str]], str | None]

_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)
_REPO_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    "projectsV2(first:20){nodes{number title url closed}}"
    "issues(states:OPEN,first:100){totalCount nodes{number}}"
    "pullRequests(states:OPEN,first:100){totalCount nodes{number}}}}"
)
# Open issues change while a run works, so the lookup is repeated this often.
REFRESH_SECONDS = 300.0


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
    # None when GitHub listed only part of them, so nothing can be ruled closed.
    open_numbers: frozenset[int] | None


def parse_remote(url: str) -> Repository | None:
    match = _GITHUB_REMOTE.match(url.strip())
    if not match:
        return None
    return Repository(match.group("owner"), match.group("name"))


def _open_numbers(data: object) -> frozenset[int] | None:
    numbers: set[int] = set()
    for kind in ("issues", "pullRequests"):
        connection = as_dict(dig(data, "data", "repository", kind))
        nodes = as_list(connection.get("nodes") if connection else None)
        if connection is None or get_int(connection, "totalCount") > len(nodes):
            return None
        numbers.update(get_int(as_dict(node), "number") for node in nodes)
    return frozenset(numbers)


def parse_repo_state(payload: str) -> RepoState | None:
    try:
        data: object = json.loads(payload)
    except ValueError:
        return None
    if as_dict(dig(data, "data", "repository")) is None:
        return None
    links: list[ProjectLink] = []
    for item in as_list(dig(data, "data", "repository", "projectsV2", "nodes")):
        node = as_dict(item)
        url = get_str(node, "url")
        if node is None or not url or node.get("closed") is True:
            continue
        links.append(
            ProjectLink(get_int(node, "number"), get_str(node, "title") or "", url)
        )
    return RepoState(links, _open_numbers(data))


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
        output = self._run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_REPO_QUERY}",
                "-F",
                f"owner={repo.owner}",
                "-F",
                f"name={repo.name}",
            ]
        )
        state = parse_repo_state(output) if output is not None else None
        if state is None and cached is not None:
            state = cached[1]  # a failed lookup keeps the last good answer
        with self._lock:
            self._states[key] = (now, state)
        return state

    def known_repository(self, cwd: str | None) -> Repository | None:
        with self._lock:
            return self._repos.get(cwd) if cwd else None

    def known_state(self, repo: Repository) -> RepoState | None:
        with self._lock:
            cached = self._states.get(repo.full_name)
        return cached[1] if cached else None
