@echo off
rem ============================================================
rem   DaV Fund Quant AI Analysis - Quick Start
rem   1. Verify .project_marker
rem   2. Verify port holder is from this project (via PowerShell Get-CimInstance)
rem   3. Skip if port already used by this project; abort if by other
rem ============================================================

cd /d "%~dp0"
setlocal EnableDelayedExpansion

echo === DaV Fund Quant AI Analysis ===
echo Path: %~dp0
echo ====================================

set "PROJECT_ROOT=%~dp0"
if not exist "%PROJECT_ROOT%\.project_marker" (
    echo [ERROR] .project_marker missing. Run from KOL-RICH root.
    echo         Current: %PROJECT_ROOT%
    pause
    exit /b 1
)

rem ---- 1. MySQL80 ----
echo [STEP 1] Checking MySQL80 ...
sc query MySQL80 | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo [INFO] Starting MySQL80 ...
    net start MySQL80
) else (
    echo [OK] MySQL80 running
)

rem ---- Helper: get CommandLine for a PID via PowerShell ----
rem   Returns the CommandLine text in %1 (or empty if not found / error)
set "GET_CMDLINE_POWERSHELL=powershell -NoProfile -ExecutionPolicy Bypass -Command \"(Get-CimInstance Win32_Process -Filter 'ProcessId=%1' -ErrorAction SilentlyContinue ^| Select-Object -First 1 -ExpandProperty CommandLine)\""

rem ---- 2. Backend :8000 ----
echo [STEP 2] Checking backend :8000 ...
set "BACKEND_OK=0"
set "PID_8000="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    set "PID_8000=%%P"
    for /f "delims=" %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Get-CimInstance Win32_Process -Filter 'ProcessId=%%P' -ErrorAction SilentlyContinue ^| Select-Object -First 1 -ExpandProperty CommandLine) } catch { '' }"') do (
        set "CMDLINE=%%C"
    )
    echo DEBUG_LINE: %CMDLINE%
    call set "CMDLINE_PATH=%%CMDLINE:*%PROJECT_ROOT%=KOL%%"
    if /i "!CMDLINE_PATH!"=="KOL" (
        set "BACKEND_OK=1"
        echo [OK] :8000 used by this project ^(PID %%P^)
    ) else (
        echo [WARN] :8000 used by other ^(PID %%P^): !CMDLINE!
    )
)

if "%BACKEND_OK%"=="0" (
    if defined PID_8000 (
        echo [ERROR] Port 8000 occupied by another project. Stop PID !PID_8000! first.
        pause
        exit /b 1
    ) else (
        echo [INFO] Starting backend :8000 ...
        start "KOL-RICH Backend" /min cmd /c "cd /d %PROJECT_ROOT% && python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000"
    )
)

rem ---- 3. Frontend :5173 ----
echo [STEP 3] Checking frontend :5173 ...
set "FRONTEND_OK=0"
set "PID_5173="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    set "PID_5173=%%P"
    for /f "delims=" %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Get-CimInstance Win32_Process -Filter 'ProcessId=%%P' -ErrorAction SilentlyContinue ^| Select-Object -First 1 -ExpandProperty CommandLine) } catch { '' }"') do (
        set "CMDLINE=%%C"
    )
    call set "CMDLINE_PATH=%%CMDLINE:*%PROJECT_ROOT%=KOL%%"
    if /i "!CMDLINE_PATH!"=="KOL" (
        set "FRONTEND_OK=1"
        echo [OK] :5173 used by this project ^(PID %%P^)
    ) else (
        echo [WARN] :5173 used by other ^(PID %%P^): !CMDLINE!
    )
)

if "%FRONTEND_OK%"=="0" (
    if defined PID_5173 (
        echo [ERROR] Port 5173 occupied by another project. Stop PID !PID_5173! first.
        pause
        exit /b 1
    ) else (
        echo [INFO] Starting frontend :5173 ...
        start "KOL-RICH Frontend" /min cmd /c "cd /d %PROJECT_ROOT%\frontend && npm run dev"
    )
)

rem ---- 4. Wait for both ports ----
echo [STEP 4] Waiting for ports (up to 30s) ...
set /a tries=0
:wait_loop
set /a ready=0
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul && set /a ready+=1
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul && set /a ready+=1
if !ready! geq 2 goto ready
set /a tries+=1
if %tries% geq 30 goto ready
timeout /t 1 /nobreak >nul
goto wait_loop
:ready

rem ---- 5. Open browser ----
echo [STEP 5] Opening browser ...
start "" "http://localhost:5173/"

echo ====================================
echo   Started. Background windows:
echo     - KOL-RICH Backend
echo     - KOL-RICH Frontend
echo   Close those two windows to stop.
echo ====================================
echo.
pause