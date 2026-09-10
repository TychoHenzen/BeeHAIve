# Live dashboard

The service serves the operator dashboard at `/dashboard`. It reads the live
state projection from `/projects/{project_id}/dashboard` and polls it every
five seconds. It does not load the old static JSX sample.

The default dashboard omits archived PBIs. Use
`/projects/{project_id}/dashboard?archived=true`, or select **Show archived
PBIs** in the dashboard, to view only PBIs whose Project status is terminal,
whose linked pull request is merged, and whose pull-request source branch is
deleted. The provider records an unknown branch state when GitHub does not
return branch evidence, so unknown or present branches stay visible in the
default view.

On Windows, copy `.env.example` to `.env`, fill in the required values, and run
the tracked `start_dashboard.bat` launcher. The launcher reads `.env` before
validating the configuration. Direct server commands still need environment
variables supplied by the process.

Start a local server from the repository root:

```text
uv run uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/dashboard?project=OWNER:NUMBER`. The project ID
must be present in `BEEHAIIVE_ALLOWED_PROJECTS`, or in the owner and number
environment variables used by the service.

Read-only state needs no API key. Operator actions require `BEEHAIIVE_API_KEY`
and the dashboard API-key field. The dashboard exposes sync, start writer, stop,
approval, and clarification actions. In documented demo mode, start writer
claims one PBI and runs the bounded repository-inventory worker. A completed
result is shown on the PBI card. Every action is recorded as `pending`,
`succeeded`, or `failed`; a response always includes the latest available run
state.

The demo worker runs `codex exec` with a read-only sandbox, an ephemeral
session, and a finite timeout. It reads a temporary credential-free copy of the
checkout configured by `BEEHAIIVE_AGENT_REPOSITORY`, and requires its exact
`BEEHAIIVE_AGENT_REPOSITORY_NAME` identity to match the claimed repository.
Stop, failure, project removal, and service shutdown terminate the process tree
and clear the run lease. Demo review mode disables review operations. It is not
a live pull-request review.

A new local database is synchronized from GitHub when the dashboard first
refreshes. Later refreshes synchronize again before reading the projection, so
the dashboard does not depend on a manual sync action for current state. Each
sync reevaluates archive evidence and restores a PBI to the default view when
any required fact changes. The project provider must be available, and the
project must be allowlisted.

The GitHub provider supplies optional live details during project sync. GitHub
sub-issues become subtasks, linked pull-request review requests and latest
reviews become readers and reviewer results, issue comments become activity,
and `bounces/N` or `escalation/<tier>` labels become escalation state.
Each PBI card keeps the raw GitHub Project status separate from the local run
status. A PBI without a local run is `idle`, terminal Project statuses appear
as terminal progress, and pull requests show merged, open, or closed state
before review decisions. An empty review decision on a merged pull request is
not rendered as `review pending`.
The provider reuses one configured client, caches discovery for 10 minutes by
default, and honors GitHub GraphQL rate-limit headers. It stops local retries
until the reset window, and serves the last successful project snapshot when a
later refresh is rate-limited. Set `BEEHAIIVE_GITHUB_DISCOVERY_CACHE_SECONDS`
to change the cache interval. See [GitHub's GraphQL rate and query limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
for the upstream limit rules.

## Reproducible smoke proof

`scripts/dashboard_smoke.py` starts a fresh service, opens the dashboard in
Edge or Chrome through the DevTools protocol, and writes a redacted JSON proof
report. It uses only the repository's existing Python dependencies and an
installed Chromium-compatible browser. The report contains the service URL,
redacted project identity, HTTP statuses, visible UI outcomes, credential
safety checks, and the documented Python and JavaScript gate results.

Run the deterministic local proof from the repository root:

```text
uv run python scripts/dashboard_smoke.py --mode fixture --report .beehaiive/dashboard-smoke.json
```

The fixture has project and repository metadata, a PBI with subtasks, readers,
reviewers, escalation, and activity, one claimable repository, and one empty
repository. The browser path verifies the no-key read, allowlist rejection,
sync, start, approval, clarification, stop, and a visible failed action. The
service databases, browser profile, and credentials stay outside source
control.

Run the live proof with credentials supplied only through the environment:

```powershell
$env:GITHUB_TOKEN = (gh auth token)
$env:GITHUB_PROJECT_OWNER = "<owner>"
$env:GITHUB_PROJECT_NUMBER = "<number>"
$env:GITHUB_PROJECT_OWNER_TYPE = "user"
$env:BEEHAIIVE_API_KEY = "<operator-key>"
uv run python scripts/dashboard_smoke.py --mode live --project "<owner>:<number>" --allow-mutations --report .beehaiive/dashboard-live-smoke.json
```

Omit `--allow-mutations` for the read-only live proof. The flag is required to
exercise sync, start, approval, clarification, and stop through the browser.
The live refresh and action wait defaults to two provider request deadlines
plus five seconds. Pass `--live-timeout <seconds>` to set a larger deadline for
projects that require more paginated provider requests.
The live harness uses an isolated local database. Project discovery uses the
configured GitHub provider, while the dashboard actions remain local state
changes. Pass `--browser` when Edge or Chrome is not discoverable.
