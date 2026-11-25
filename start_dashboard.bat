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

rem BeeHAIve reads .env itself; the launcher only needs the port to check it.
set "PORT=%BEEHAIIVE_PORT%"
if not defined PORT if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if /i "%%~A"=="BEEHAIIVE_PORT" set "PORT=%%~B"
    )
)
if not defined PORT set "PORT=8000"

set "PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "PORT_PID=%%P"
if defined PORT_PID (
    echo Port %PORT% is already occupied by process %PORT_PID%.
    pause
    exit /b 1
)

rem The server runs in the foreground as this script's child, so whatever
rem stops the launcher (Ctrl+C, closing its window, Stop in an IDE) stops the
rem server with it. Archon's runs are not in this process tree: Archon's own
rem server and its scheduled tasks run them.
echo BeeHAIve is starting at http://127.0.0.1:%PORT%/
uv run python -m beehaiive
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
