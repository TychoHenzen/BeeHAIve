# BeeHAIve contributor workflow

## Delivery workflow

- Work runs through Archon's bundled SDLC pack. Its settings are in `.archon/packs/sdlc.yaml` and its maintainers in `.archon/sdlc/policy/maintainers.json`. Runs read both from master on GitHub, so a change takes effect only once merged.
- An issue states the problem, why it matters, the desired outcome, the invariants, and the acceptance criteria.
- `archon-intake` triages the issue, `archon-build-queue` works it to a reviewed pull request, and `archon-merge-queue` merges it.
- Both lanes are manual: no build starts and nothing merges until a maintainer answers on `archon workflow attention`.
- Do not merge a pull request the build queue opened by hand. The merge queue's gate merges it pinned to its reviewed head.

## Transcript data

- Claude Code transcripts are an internal, undocumented format. Read every field through the `beehaiive/records.py` helpers so an unexpected shape yields "unknown", never an exception.
- BeeHAIve is read-only: it never writes to transcripts, git, or GitHub, and it never runs `git` or `gh` while a request holds the index lock.

## Type checking

- `uv run pyright` checks both `beehaiive/` and `tests/` under strict mode; `reportPrivateUsage` is off only for `tests/` (see `pyproject.toml`). Do not add `type: ignore` to silence it.
