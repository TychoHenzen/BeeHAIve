# Live dashboard

The service serves the operator dashboard at `/dashboard`. It reads the live
state projection from `/projects/{project_id}/dashboard` and polls it every
five seconds. It does not load the old static JSX sample.

The default dashboard omits archived PBIs. Use
`/projects/{project_id}/dashboard?archived=true`, or select **Archived** in the
Queue filters, to view only PBIs whose Project status is terminal and whose
durable run is not active, waiting for an operator, or failed. Recent delivery
evidence remains visible on Mission control even when terminal PBIs are not in
the active queue.
Merged pull requests and source-branch state remain visible as evidence in the
inspector. A terminal item does not stay in the main queue merely because
provider branch evidence is unavailable.

Mission control is one board rather than one page per project. Its reading
order is **Needs you**, **Queue**, **Agents**, and **Recent deliveries**. Queue
rows show only the next decision and the evidence needed to choose it. Open a
row's inspector for lifecycle, session, contract, checks, and delivery detail.
Queue, Agents, Deliveries, Workflow graphs, Projects, Evidence log, and Settings
are separate hash-routed views. Project is a filter in the board header. The
project ID is selected in Settings. Workflow graphs expose a dropdown of
stored workflow definitions on both the graph page and Settings, then retain
the selected workflow in the dashboard URL. The graph page reads structured
nodes, transitions, safety, review, and activation state without requiring
operator-entered JSON. The guided demo uses placeholder
systems so the complete
Plan, Refine, Implement, Pull request, Review, and Ship journey can be
exercised without GitHub or Codex changes.

The live Queue view groups work into Refinement, Implementation, Publish,
Review, Repair, Completion, Blocked, and Completed queues. Queue assignment is
computed by the server from Project Status and current run, branch, pull
request, check, review, and handoff evidence. The browser displays that
assignment; it does not invent a second lifecycle. Select Archived to inspect
the Completed queue.

Quick idea entry accepts bounded text and an allowlisted Project selection. It
sends one authenticated capture action to the add-backlog-idea skill context.
The action log shows pending, succeeded, or failed state and the created issue
returns to the selected Project's Backlog. The read-only dashboard config
endpoint at /dashboard/config supplies the Project IDs for that selector.
The top Project switcher uses the same allowlist; selecting another Project
refreshes its queue and keeps actions scoped to that Project.

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

Read-only state needs no API key. Dashboard actions and Settings writes use the
configured server-side `BEEHAIIVE_API_KEY`; the browser never asks for or stores it.
Direct API clients still send it as `X-API-Key`. The dashboard exposes a
focused work queue with sync, start or claim writer, stop, advance, approval,
clarification, operator-question answers, retry, and commit-and-push delivery
actions. The API accepts explicit
`synchronize`, `claim`, `retry`, and `deliver` action names through the same
authenticated boundary. Workflow graphs have their own structured page with
definition, transition, safety, review, activation, and rollback readback.
Settings also exposes the scheduler switch, poll interval, and autonomous
worker capacity. Applying those values updates the running server when a
workflow-backed agent worker exists. Put the same values in `.env` to retain
them after a restart.
Production lifecycle buttons call the server autonomous-run endpoint. The
guided demo is the only browser-local placeholder and is labeled in its
buttons and status message. Recent activity includes the action reason,
selected PBI, run stage, scheduler settings, and bounded failure details.
The active run inspector also shows whether the Codex process is starting,
alive, exited, or timed out, with its PID, elapsed time, last-output age, and
configured timeout.
In documented demo mode, start work claims one PBI and runs the bounded
repository-inventory worker. A completed result is shown in the work-item row
and run inspector. Every action is recorded as `pending`, `succeeded`, or `failed`; a
response always includes the latest available run state. Graph traces use the
existing 100-event and 4,000-character limits, a 64,000-byte aggregate cap,
status filters, expandable details, and redacted text. Review,
refinement, and host APIs remain authenticated owning-service boundaries until
their own dashboard slices add controls.

The dashboard worker runs `codex exec` with a writable sandbox rooted at one
unique Git worktree. It disables network access and child agents, filters the
child environment, and requires the configured repository identity to match the
claimed repository. Both the dashboard run lease and worktree lease are renewed
while the process runs. On success, dirty changes are committed with host Git
`user.name` and `user.email`, then pushed to the configured `origin` on the exact
leased branch without force. A clean worktree is a no-op.

The service records the commit SHA and push result. A blocked push keeps the
local commit and retained worktree. The work-item inspector exposes a retry action that
pushes the same SHA. Pushes use host Git authentication. Credentials are not
sent to the model child or stored in delivery evidence. Failed or cancelled
workers with uncommitted changes keep their worktrees for recovery. Clean
unsuccessful worktrees are removed. Demo review mode disables review operations.
It is not a live pull-request review.

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
For each active linked pull request, the provider also records the observed head
SHA and paginated `CheckRun` and `StatusContext` evidence. The dashboard shows
requiredness, status or state, conclusion, source URL, and a verdict of
`pending`, `passing`, `blocking`, or `unproven`. A missing rollup, provider or
rate-limit error, unavailable requiredness, or head mismatch stays unproven.
A blocking required check for the observed head fails the matching local
implementation run through the existing durable failure and routing path.
Each work-item row keeps the raw GitHub Project status separate from the local
run status. A PBI without a local run is `idle`, terminal Project statuses appear
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
safety checks, and the documented Python and JavaScript gate results. The
coverage check uses the repository's 90 percent CI threshold.

Run the deterministic local proof from the repository root:

```text
uv run python scripts/dashboard_smoke.py --mode fixture --report .beehaiive/dashboard-smoke.json
```

The fixture has project and repository metadata, a PBI with subtasks, readers,
reviewers, escalation, and activity, one claimable repository, and one empty
repository. The browser path verifies the no-key read, allowlist rejection,
sync, start, approval, clarification, stop, and a visible failed action. The service
databases, browser profile, and credentials stay outside source control.

The browser-level lifecycle suite uses Python Playwright against the guided
placeholder systems. It proves the Plan, Refine, Build, Pull request, Review,
and Ship journey, operator-question recovery, and server-owned dashboard
authorization. Run it with `uv run pytest -m e2e tests/e2e/test_dashboard_lifecycle.py -q`.

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
