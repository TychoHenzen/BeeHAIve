# BeeHAIve

BeeHAIve is a FastAPI control plane for one allowlisted GitHub Project. The
dashboard reads the live Project state, claims one repository writer run, and
can execute one bounded local demo task.

The demo task is named **bounded repository inventory**. It asks the Codex CLI
to inspect a temporary credential-free copy of the configured checkout and
report its repository name, current branch, and tracked-file count. It does not
edit files, create files, access the network, read credentials, or start
another agent. Its plain-text result is shown on the completed PBI card.

This delivery does not claim autonomous swarms, multi-device coordination,
production pull-request review adapters, or automatic pull-request creation.

## Clean Windows setup

Prerequisites are Windows, Git, Python 3.13, `uv`, GitHub CLI, Node.js, the
Codex CLI, and Chromium. GitHub access must include the selected Project and
its linked issue metadata.

Install Python 3.13 and `uv`, then check out the repository:

```powershell
git clone https://github.com/TychoHenzen/BeeHAIve.git
Set-Location BeeHAIve
uv sync
gh auth status
Copy-Item .env.example .env
```

Edit `.env` with the selected project and credentials:

```dotenv
GITHUB_TOKEN=<fine-grained-token>
GITHUB_PROJECT_OWNER=<owner>
GITHUB_PROJECT_NUMBER=<number>
GITHUB_PROJECT_OWNER_TYPE=user
BEEHAIIVE_ALLOWED_PROJECTS=<owner>:<number>
BEEHAIIVE_API_KEY=<operator-key>
BEEHAIIVE_REVIEW_MODE=demo
# BEEHAIIVE_AGENT_REPOSITORY=<path-to-BeeHAIve>
BEEHAIIVE_AGENT_REPOSITORY_NAME=<owner>/<repository>
# BEEHAIIVE_AGENT_TIMEOUT_SECONDS=120
# BEEHAIIVE_CODEX_EXECUTABLE=codex
```

The project ID is exactly `<owner>:<number>`. The tracked launcher reads the
gitignored `.env` file before checking these values. Existing process variables
take precedence. `BEEHAIIVE_AGENT_REPOSITORY_NAME` must match the repository
selected in the Project. The timeout must be finite and no greater than 900
seconds. Do not commit `.env` or place its values in screenshots.

Start the service from the repository root so the launcher loads `.env`:

```powershell
.\start_dashboard.bat
```

The launcher checks `uv`, the configured Codex executable, GitHub configuration,
and the API key. It starts `uv run uvicorn main:app --reload` and keeps the
server output visible. Direct Uvicorn startup is supported only when the same
variables are already present in the process environment.

Open `http://127.0.0.1:8000/dashboard?project=<owner>:<number>` and
`http://127.0.0.1:8000/docs`. Viewing the dashboard does not need an API key.
Before clicking an action such as **Start writer**, enter the value of
`BEEHAIIVE_API_KEY` from `.env` in the dashboard API-key field.

## Run the live demo

1. Confirm the dashboard shows the expected project and linked repositories.
2. Enter the API key, then select the repository whose checkout is running BeeHAIve.
3. Select **Start writer**. This claims the first claimable PBI and starts the
   bounded repository-inventory worker.
4. Watch the repository writer and PBI status change to active.
5. Wait for the completed PBI card. Its **Result** field contains the agent's
   plain-text inventory result, and the completed-run count increases.

The worker uses `codex exec --sandbox read-only --ephemeral --json` with a
finite timeout. It receives an explicit model selected by the routing tier and
a temporary copy that excludes local environment and credential files. The
service renews the run lease while the process runs. **Stop**, failure, sync
removal, and service shutdown terminate the process tree, mark the run failed
with a reason, and clear the lease.

If the demo fails, read the PBI **Failure** field and the action log. Check the
GitHub token, exact project allowlist, `codex` availability, and the local
checkout path. Restarting the service does not create a second active writer.
Stop the run before removing local `.beehaiive` state.

## Evidence and verification

Redacted, versioned browser evidence is kept here:

- [Configured dashboard](docs/screenshots/dashboard-configured.png)
- [Active demo](docs/screenshots/active-demo.png)
- [Completed result](docs/screenshots/completed-demo.png)
- [Stopped or failed run](docs/screenshots/stopped-demo.png)

These captures come from the live browser proof and are redacted before they
are written. They contain no project identifiers, repository names, or
credentials. Regenerate them with the configured `.env`:

```powershell
uv run python -m scripts.dashboard_screenshots
```

Use `BEEHAIIVE_SCREENSHOT_MODE=fixture` only for local supplementary evidence.
The committed captures must come from the live mode.

Run the deterministic fixture proof without GitHub mutations:

```powershell
uv run python scripts/dashboard_smoke.py --mode fixture --report .beehaiive/dashboard-smoke.json
```

Run the live browser proof with the environment configuration above:

```powershell
uv run python scripts/dashboard_smoke.py --mode live --project "$env:GITHUB_PROJECT_OWNER`:$env:GITHUB_PROJECT_NUMBER" --allow-mutations --live-timeout 180 --report .beehaiive/dashboard-live-smoke.json
```

The live smoke exercises the real GitHub Project discovery path. Its local
dashboard actions remain bounded local state changes. The report is redacted
and `.beehaiive` is ignored by Git.

For the full local gate, run:

```powershell
uv run pytest -q
uv run node --test tests/*.test.mjs
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --cov=beehaiive --cov=main --cov-report=term-missing --cov-fail-under=100
```

The dashboard details and security boundary are documented in
[`docs/dashboard.md`](docs/dashboard.md) and [`docs/security.md`](docs/security.md).
