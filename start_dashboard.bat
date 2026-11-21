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
