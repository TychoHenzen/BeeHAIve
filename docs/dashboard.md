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
operator-entered JSON.
Production startup seeds the active `automation-swarm` workflow from the same
six autonomous lifecycle skills; older stored definitions remain selectable.

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
`start_dashboard.bat` refuses to start when port 8000 is already occupied rather
than leaving another reload worker behind.

Use the URL served by FastAPI rather than opening `docs/dashboard.html` or a
standalone design export directly. The static files do not provide
`/dashboard/settings`; a `Not Found` save response means the browser is pointed
at the wrong or stale server origin.

Read-only state needs no API key. Dashboard actions and Settings writes use the
configured server-side `BEEHAIIVE_API_KEY`; the browser never asks for or stores it.
Direct API clients still send it as `X-API-Key`. The dashboard exposes a
focused work queue with lifecycle, block-resolution, stop, operator-question,
retry, and delivery evidence. Workflow graphs have their own structured page
with definition, transition, safety, review, activation, and rollback readback.
Settings also exposes validated project and workflow selection plus the
scheduler switch, poll interval, and autonomous worker capacity. Applying
those values updates the running server when a workflow-backed agent worker
exists and persists them in the server state store. Credentials and checkout
paths remain environment-owned.
The **Run full lifecycle** action calls the server autonomous-run endpoint for
the selected claimable PBI. Recent activity includes the action reason,
selected PBI, run stage, scheduler settings, and bounded failure details.
The active run inspector also shows whether the Codex process is starting,
alive, exited, or timed out, with its PID, elapsed time, last-output age, and
configured timeout.
Every action is recorded as `pending`, `succeeded`, or `failed`; a response
always includes the latest available run state. Graph traces use the existing
100-event and 4,000-character limits, a 64,000-byte aggregate cap, status
filters, expandable details, and redacted text. Review and refinement APIs
remain authenticated owning-service boundaries. When a stable worker host is
configured, the read-only dashboard state also exposes its bounded host facts,
capabilities, advertised slots, heartbeat, and derived `active`, `stale`, or
`unknown` liveness.

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

## Verification

Use the configured service and a real allowlisted Project for dashboard proof.
Open `http://127.0.0.1:8000/dashboard?project=OWNER:NUMBER` in Edge or another
Chromium-compatible browser. Record the served URL, visible PBI identifiers,
HTTP statuses, dashboard network outcomes, and console errors. The proof must
show live Project data and no synthetic identifiers.

For the local gate, run:

```powershell
uv run pytest -q
node --test tests/*.test.mjs
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --cov=beehaiive --cov=main --cov-report=term-missing --cov-fail-under=90
```
