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
        if /i "%%~A"=="BEEHAIIVE_PROJECT_OWNER" if not defined BEEHAIIVE_PROJECT_OWNER set "BEEHAIIVE_PROJECT_OWNER=%%~B"
        if /i "%%~A"=="BEEHAIIVE_PROJECT_OWNER_TYPE" if not defined BEEHAIIVE_PROJECT_OWNER_TYPE set "BEEHAIIVE_PROJECT_OWNER_TYPE=%%~B"
        if /i "%%~A"=="BEEHAIIVE_PROJECT_NUMBER" if not defined BEEHAIIVE_PROJECT_NUMBER set "BEEHAIIVE_PROJECT_NUMBER=%%~B"
        if /i "%%~A"=="BEEHAIIVE_DB" if not defined BEEHAIIVE_DB set "BEEHAIIVE_DB=%%~B"
        if /i "%%~A"=="BEEHAIIVE_CODEX" if not defined BEEHAIIVE_CODEX set "BEEHAIIVE_CODEX=%%~B"
        if /i "%%~A"=="BEEHAIIVE_CODEX_ARGS" if not defined BEEHAIIVE_CODEX_ARGS set "BEEHAIIVE_CODEX_ARGS=%%~B"
        if /i "%%~A"=="BEEHAIIVE_SKILLS_DIRS" if not defined BEEHAIIVE_SKILLS_DIRS set "BEEHAIIVE_SKILLS_DIRS=%%~B"
        if /i "%%~A"=="BEEHAIIVE_PROJECT_REFRESH_SECONDS" if not defined BEEHAIIVE_PROJECT_REFRESH_SECONDS set "BEEHAIIVE_PROJECT_REFRESH_SECONDS=%%~B"
        if /i "%%~A"=="BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS" if not defined BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS set "BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS=%%~B"
    )
)

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required. Install uv, then run this launcher again.
    pause
    exit /b 1
)

if not defined BEEHAIIVE_CODEX set "BEEHAIIVE_CODEX=codex"
where "%BEEHAIIVE_CODEX%" >nul 2>&1
if errorlevel 1 (
    echo The configured Codex executable was not found: %BEEHAIIVE_CODEX%
    pause
    exit /b 1
)

where gh >nul 2>&1
if errorlevel 1 (
    echo GitHub CLI is required. Install gh, then run this launcher again.
    pause
    exit /b 1
)
gh auth status >nul 2>&1
if errorlevel 1 (
    echo gh is not authenticated. Run gh auth login, then run this launcher again.
    pause
    exit /b 1
)

if not defined BEEHAIIVE_PROJECT_OWNER (
    echo Set BEEHAIIVE_PROJECT_OWNER before starting BeeHAIve.
    pause
    exit /b 1
)
if not defined BEEHAIIVE_PROJECT_NUMBER (
    echo Set BEEHAIIVE_PROJECT_NUMBER before starting BeeHAIve.
    pause
    exit /b 1
)
if not defined BEEHAIIVE_PROJECT_OWNER_TYPE set "BEEHAIIVE_PROJECT_OWNER_TYPE=user"
set "PORT=8000"
set "PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "PORT_PID=%%P"
if defined PORT_PID (
    echo Port %PORT% is already occupied by process %PORT_PID%.
    pause
    exit /b 1
)

set "PYTHONPATH=%CD%"
set "SERVER_PID="
for /f "tokens=*" %%P in ('powershell -NoProfile -Command "$p = Start-Process -FilePath uv -ArgumentList @('run','python','-m','beehaiive') -WorkingDirectory '%CD%' -NoNewWindow -PassThru; $p.Id"') do set "SERVER_PID=%%P"
if not defined SERVER_PID (
    echo BeeHAIve could not start.
    pause
    exit /b 1
)
echo BeeHAIve is running at http://127.0.0.1:%PORT%/ (PID %SERVER_PID%).

:wait_for_server
timeout /t 2 /nobreak >nul
tasklist /FI "PID eq %SERVER_PID%" | findstr /R /C:"%SERVER_PID%" >nul
if not errorlevel 1 goto wait_for_server
set "EXIT_CODE=0"

:cleanup
if defined SERVER_PID taskkill /PID %SERVER_PID% /T /F >nul 2>&1
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
