@echo off
setlocal
cd /d "%~dp0"

if not exist "pyproject.toml" (
    echo BeeHAIve was not started from its repository directory.
    pause
    exit /b 1
)

if exist ".env" (
    echo Loading local configuration from .env.
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if /i "%%~A"=="GITHUB_TOKEN" if not defined GITHUB_TOKEN if not defined GH_TOKEN set "GITHUB_TOKEN=%%~B"
        if /i "%%~A"=="GH_TOKEN" if not defined GITHUB_TOKEN if not defined GH_TOKEN set "GH_TOKEN=%%~B"
        if /i "%%~A"=="GITHUB_PROJECT_OWNER" if not defined GITHUB_PROJECT_OWNER set "GITHUB_PROJECT_OWNER=%%~B"
        if /i "%%~A"=="GITHUB_PROJECT_NUMBER" if not defined GITHUB_PROJECT_NUMBER set "GITHUB_PROJECT_NUMBER=%%~B"
        if /i "%%~A"=="GITHUB_PROJECT_OWNER_TYPE" if not defined GITHUB_PROJECT_OWNER_TYPE set "GITHUB_PROJECT_OWNER_TYPE=%%~B"
        if /i "%%~A"=="BEEHAIIVE_ALLOWED_PROJECTS" if not defined BEEHAIIVE_ALLOWED_PROJECTS set "BEEHAIIVE_ALLOWED_PROJECTS=%%~B"
        if /i "%%~A"=="BEEHAIIVE_API_KEY" if not defined BEEHAIIVE_API_KEY set "BEEHAIIVE_API_KEY=%%~B"
        if /i "%%~A"=="BEEHAIIVE_REVIEW_MODE" if not defined BEEHAIIVE_REVIEW_MODE set "BEEHAIIVE_REVIEW_MODE=%%~B"
        if /i "%%~A"=="BEEHAIIVE_AGENT_REPOSITORY" if not defined BEEHAIIVE_AGENT_REPOSITORY set "BEEHAIIVE_AGENT_REPOSITORY=%%~B"
        if /i "%%~A"=="BEEHAIIVE_AGENT_REPOSITORY_NAME" if not defined BEEHAIIVE_AGENT_REPOSITORY_NAME set "BEEHAIIVE_AGENT_REPOSITORY_NAME=%%~B"
        if /i "%%~A"=="BEEHAIIVE_AGENT_TIMEOUT_SECONDS" if not defined BEEHAIIVE_AGENT_TIMEOUT_SECONDS set "BEEHAIIVE_AGENT_TIMEOUT_SECONDS=%%~B"
        if /i "%%~A"=="BEEHAIIVE_CODEX_EXECUTABLE" if not defined BEEHAIIVE_CODEX_EXECUTABLE set "BEEHAIIVE_CODEX_EXECUTABLE=%%~B"
        if /i "%%~A"=="BEEHAIIVE_CODEX_MODEL" if not defined BEEHAIIVE_CODEX_MODEL set "BEEHAIIVE_CODEX_MODEL=%%~B"
    )
)

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required. Install uv, then run this launcher again.
    pause
    exit /b 1
)

if not defined BEEHAIIVE_CODEX_EXECUTABLE set "BEEHAIIVE_CODEX_EXECUTABLE=codex"
where "%BEEHAIIVE_CODEX_EXECUTABLE%" >nul 2>&1
if errorlevel 1 (
    echo The configured Codex CLI executable was not found: %BEEHAIIVE_CODEX_EXECUTABLE%
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
if not defined BEEHAIIVE_AGENT_REPOSITORY_NAME (
    echo Set BEEHAIIVE_AGENT_REPOSITORY_NAME to the exact linked repository owner/name.
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
