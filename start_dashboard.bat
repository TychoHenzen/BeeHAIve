@echo off
setlocal
cd /d "%~dp0"

if not exist "pyproject.toml" (
    echo BeeHAIve was not started from its repository directory.
    pause
    exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required. Install uv, then run this launcher again.
    pause
    exit /b 1
)

where codex >nul 2>&1
if errorlevel 1 (
    echo The Codex CLI is required for the bounded worker demo.
    pause
    exit /b 1
)

if not defined GITHUB_TOKEN if not defined GH_TOKEN (
    echo Set GITHUB_TOKEN or GH_TOKEN before starting the dashboard.
    pause
    exit /b 1
)
if not defined GITHUB_PROJECT_OWNER (
    echo Set GITHUB_PROJECT_OWNER before starting the dashboard.
    pause
    exit /b 1
)
if not defined GITHUB_PROJECT_NUMBER (
    echo Set GITHUB_PROJECT_NUMBER before starting the dashboard.
    pause
    exit /b 1
)
if not defined BEEHAIIVE_API_KEY (
    echo Set BEEHAIIVE_API_KEY before starting the dashboard.
    pause
    exit /b 1
)

if not defined GITHUB_PROJECT_OWNER_TYPE set "GITHUB_PROJECT_OWNER_TYPE=user"
if not defined BEEHAIIVE_ALLOWED_PROJECTS set "BEEHAIIVE_ALLOWED_PROJECTS=%GITHUB_PROJECT_OWNER%:%GITHUB_PROJECT_NUMBER%"
if not defined BEEHAIIVE_REVIEW_MODE set "BEEHAIIVE_REVIEW_MODE=demo"
if not defined BEEHAIIVE_AGENT_REPOSITORY set "BEEHAIIVE_AGENT_REPOSITORY=%CD%"

echo BeeHAIve dashboard: http://127.0.0.1:8000/dashboard?project=%GITHUB_PROJECT_OWNER%:%GITHUB_PROJECT_NUMBER%
echo Documentation: http://127.0.0.1:8000/docs
echo Stop the server with Ctrl+C.
uv run uvicorn main:app --reload
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
