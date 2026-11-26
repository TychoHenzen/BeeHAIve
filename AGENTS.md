# BeeHAIve contributor workflow

## Delivery workflow

- Work runs through Archon's bundled SDLC pack. Its settings are in `.archon/packs/sdlc.yaml` and its maintainers in `.archon/sdlc/policy/maintainers.json`. Runs read both from master on GitHub, so a change takes effect only once merged.
- An issue states the problem, why it matters, the desired outcome, the invariants, and the acceptance criteria.
- `archon-intake` triages the issue, `archon-build-queue` works it to a reviewed pull request, and `archon-merge-queue` merges it.
- Both lanes are manual: no build starts and nothing merges until a maintainer answers on `archon workflow attention`.
- Do not merge a pull request the build queue opened by hand. The merge queue's gate merges it pinned to its reviewed head.

## Transcript data

- Claude Code transcripts are an internal, undocumented format. Read every field through the `beehaiive/records.py` helpers so an unexpected shape yields "unknown", never an exception.
- The sessions page is read-only: BeeHAIve never writes to transcripts, git, or GitHub, and it never runs `git` or `gh` while a request holds the index lock.

## Archon

- The console acts on Archon: it starts, answers, cancels and resumes runs, writes binding files under Archon's `triggers/` folder, and installs Windows scheduled tasks named `Archon-<binding>`. Those are the only writes it makes.
- `/archon/api/*` forwards Archon's own API unchanged. Anything Archon's API lacks goes in `beehaiive/routes/archon_tools.py`.
- Run the Archon CLI only through `ArchonCli` with a fixed argument list, never a shell string. The Pipeline page runs archon-home's `ledger.mjs` the same way, through `PipelineReader`, and only for a repository a pbi trigger binding names; the ledger only reads. Check every value that comes from the browser (names, ids) against a pattern before it becomes a path or an argument, and refuse one that could read as an option.
- `beehaiive/guard.py` refuses any request whose Host is not this machine, any foreign Origin, and any non-GET request without `X-BeeHAIve: 1`. Keep that header in step with `web/console/api.js`, and test with `TestClient(app, base_url="http://127.0.0.1")`.
- Tests never touch the real Archon install: build the app with `tests/archon_fakes.py`.

## Type checking

- `uv run pyright` checks both `beehaiive/` and `tests/` under strict mode; `reportPrivateUsage` is off only for `tests/` (see `pyproject.toml`). Do not add `type: ignore` to silence it.
