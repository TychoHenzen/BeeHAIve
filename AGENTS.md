# BeeHAIve contributor workflow

## Delivery workflow

- Ideas enter the linked GitHub Project through `/add-backlog-idea` as Backlog issues.
- `/refine-backlog-item` researches an issue and moves the refined PBI to Todo.
- `/next-ticket` implements one issue branch and pushes it.
- `/submit-draft-pr` creates or updates the draft pull request.
- Review is read-only until the user explicitly accepts the findings.
- `/complete-pr` alone owns making a pull request ready, merging it, confirming the linked issue, and deleting the branch.

## Transcript data

- Claude Code transcripts are an internal, undocumented format. Read every field through the `beehaiive/records.py` helpers so an unexpected shape yields "unknown", never an exception.
- The sessions page is read-only: BeeHAIve never writes to transcripts, git, or GitHub, and it never runs `git` or `gh` while a request holds the index lock.

## Archon

- The console acts on Archon: it starts, answers, cancels and resumes runs, writes binding files under Archon's `triggers/` folder, and installs Windows scheduled tasks named `Archon-<binding>`. Those are the only writes it makes.
- `/archon/api/*` forwards Archon's own API unchanged. Anything Archon's API lacks goes in `beehaiive/routes/archon_tools.py`.
- Run the Archon CLI only through `ArchonCli` with a fixed argument list, never a shell string. Check every value that comes from the browser (names, ids) against a pattern before it becomes a path or an argument, and refuse one that could read as an option.
- `beehaiive/guard.py` refuses any request whose Host is not this machine, any foreign Origin, and any non-GET request without `X-BeeHAIve: 1`. Keep that header in step with `web/console/api.js`, and test with `TestClient(app, base_url="http://127.0.0.1")`.
- Tests never touch the real Archon install: build the app with `tests/archon_fakes.py`.

## Type checking

- `uv run pyright` checks both `beehaiive/` and `tests/` under strict mode; `reportPrivateUsage` is off only for `tests/` (see `pyproject.toml`). Do not add `type: ignore` to silence it.
