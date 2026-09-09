# Live dashboard

The service serves the operator dashboard at `/dashboard`. It reads the live
state projection from `/projects/{project_id}/dashboard` and polls it every
five seconds. It does not load the old static JSX sample.

Start a local server from the repository root:

```text
uv run uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/dashboard?project=OWNER:NUMBER`. The project ID
must be present in `BEEHAIIVE_ALLOWED_PROJECTS`, or in the owner and number
environment variables used by the service.

Read-only state needs no API key. Operator actions require `BEEHAIIVE_API_KEY`
and the dashboard API-key field. The first release exposes start or sync,
stop, approval, and clarification actions. Every action is recorded as
`pending`, `succeeded`, or `failed`; a response always includes the latest
available run state.

A new local database is synchronized from GitHub when the dashboard first
refreshes. Later refreshes synchronize again before reading the projection, so
the dashboard does not depend on a manual sync action for current state. The
project provider must be available, and the project must be allowlisted.

The GitHub provider supplies optional live details during project sync. GitHub
sub-issues become subtasks, linked pull-request review requests and latest
reviews become readers and reviewer results, issue comments become activity,
and `bounces/N` or `escalation/<tier>` labels become escalation state.
