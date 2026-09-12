# BeeHAIve

BeeHAIve is a FastAPI control plane for one allowlisted GitHub Project. The
dashboard reads the live Project state, claims one repository writer run, and
can execute one bounded local demo task.

The demo task is named **bounded repository inventory**. It asks the Codex CLI
to inspect the exact leased Git worktree and report its repository name,
current branch, and tracked-file count. The default task does not edit files.
The dashboard worker can write only inside its leased worktree. Its plain-text
result is shown on the completed PBI card.

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
# BEEHAIIVE_GITHUB_DISCOVERY_CACHE_SECONDS=600
BEEHAIIVE_ALLOWED_PROJECTS=<owner>:<number>
BEEHAIIVE_API_KEY=<operator-key>
BEEHAIIVE_REVIEW_MODE=demo
# BEEHAIIVE_AGENT_REPOSITORY=<path-to-BeeHAIve>
BEEHAIIVE_AGENT_REPOSITORY_NAME=<owner>/<repository>
# BEEHAIIVE_AGENT_TIMEOUT_SECONDS=120
# BEEHAIIVE_CODEX_EXECUTABLE=codex
# BEEHAIIVE_SCHEDULER_ENABLED=false
# BEEHAIIVE_SCHEDULER_POLL_INTERVAL_SECONDS=600
# BEEHAIIVE_SCHEDULER_MAX_CONCURRENCY=1
```

The project ID is exactly `<owner>:<number>`. The tracked launcher reads the
gitignored `.env` file before checking these values. Existing process variables
take precedence. `BEEHAIIVE_AGENT_REPOSITORY_NAME` must match the repository
selected in the Project. The timeout must be finite and no greater than 900
seconds. Project discovery is cached for 10 minutes by default, so the
dashboard's five-second polling does not repeat the full GraphQL discovery.
Set `BEEHAIIVE_GITHUB_DISCOVERY_CACHE_SECONDS` to a finite non-negative number
to change that interval. Do not commit `.env` or place its values in screenshots.
The continuous scheduler is off by default. Enable it with
`BEEHAIIVE_SCHEDULER_ENABLED=true`; it polls only `BEEHAIIVE_ALLOWED_PROJECTS`,
uses the configured dashboard worker, and shares its process-wide worker limit
with manual starts. Its status appears in the project dashboard response.

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

The dashboard worker uses `codex exec --sandbox workspace-write --ephemeral
--json` in a unique leased worktree. It disables network access and child
agents, passes a filtered environment, and rejects credential-like tracked
files. The service renews both run and worktree leases while the process runs.
On success, the service commits dirty changes with the host Git identity and
pushes the exact leased branch to the configured `origin` without force. A
clean tree is a no-op. A blocked push keeps its commit and worktree for the
dashboard retry action. Failed or cancelled worktrees with changes are kept
for recovery rather than discarded.

Set `git config user.name` and `git config user.email` in the host checkout.
Push uses host Git authentication. Credentials are not sent to the model child
or saved in delivery evidence. Check the PBI card's **Git delivery** status for
the commit SHA and push result.

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

The dashboard details, task contracts, and security boundary are documented in
[`docs/dashboard.md`](docs/dashboard.md), [`docs/task-contracts.md`](docs/task-contracts.md),
[`docs/meta-review.md`](docs/meta-review.md), and [`docs/security.md`](docs/security.md).
