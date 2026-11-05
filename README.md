# BeeHAIve

<p align="center">
  <img src="docs/assets/beehaive-logo.png" alt="BeeHAIve logo" width="360">
</p>

BeeHAIve is a local, read-only dashboard for the Claude Code sessions you run
from the desktop app or the CLI. It shows every recently active session as an
agent, with the subagents it dispatched nested under it. For each one it shows
what it is doing now, every step it took and how long that step ran, and every
problem it hit: failed tool calls, API errors, hook errors and failed
subagents.

A session that runs dod-guard's `/goal-sdlc` gets more: the stage in flight
(for example `next-ticket #405`), links to the issue and any pull requests,
and links to the GitHub repository and its open GitHub Projects.

BeeHAIve never starts, stops or steers an agent, and never writes to git or
GitHub. You keep working in Claude Code; BeeHAIve only reads what Claude Code
writes to disk.

## Setup

Use Python 3.13 and `uv`. The GitHub CLI (`gh`), signed in, is optional: it is
only used to find the Projects linked to a goal-sdlc session's repository.

```powershell
git clone https://github.com/TychoHenzen/BeeHAIve.git
Set-Location BeeHAIve
uv sync
.\start_dashboard.bat
```

Open `http://127.0.0.1:8000/`. `uv run python -m beehaiive` starts the same
server without the launcher. The launcher refuses a port that is already in
use and stops the server process tree when it exits.

Every setting is optional. Put any of them in `.env` (see `.env.example`) or in
the environment, which wins over `.env`:

- `BEEHAIIVE_CLAUDE_PROJECTS`: the transcript folder, by default
  `~/.claude/projects`.
- `BEEHAIIVE_LOOKBACK_HOURS`: how far back to show sessions, by default 24.
- `BEEHAIIVE_PORT`: by default 8000.

## How it works

Claude Code writes each session to
`~/.claude/projects/<project>/<session-id>.jsonl`, and each subagent to a
`subagents/agent-<id>.jsonl` file beside it with a `.meta.json` file that
names the tool call that launched it. A background thread reads only the bytes
added since its last pass, every two seconds, and skips record types the
dashboard does not use. The first pass over a day of heavy use takes some
seconds; the page says so while it runs.

A thread counts as working when it wrote something in the last two minutes,
when a tool call it started less than fifteen minutes ago has not returned, or
when one of its subagents is working. A subagent that stopped without its
launch call returning is shown as stalled.

The transcript format is internal to Claude Code and undocumented. Every field
is read defensively, so a format change shows up as missing detail or as a
count of records that could not be read, not as a crash. Codex sessions are
not read.

## Local gate

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q --cov=beehaiive --cov-report=term-missing --cov-fail-under=90
node --test tests/*.test.mjs
uv run pip-audit --progress-spinner off
```
