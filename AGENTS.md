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
- BeeHAIve is read-only: it never writes to transcripts, git, or GitHub, and it never runs `git` or `gh` while a request holds the index lock.

## Type checking

- `uv run pyright` checks both `beehaiive/` and `tests/` under strict mode; `reportPrivateUsage` is off only for `tests/` (see `pyproject.toml`). Do not add `type: ignore` to silence it.
