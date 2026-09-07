@echo off
cd /d "%~dp0"

echo ============================================
echo   KOL-RICH Quick Start
echo ============================================

rem ---- 1. MySQL80 ----
sc query MySQL80 | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo [INFO] Starting MySQL80 ...
    net start MySQL80
) else (
    echo [OK] MySQL80 already running
)

rem ---- 2. Backend FastAPI :8000 ----
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [INFO] Starting backend on :8000 ...
    start "KOL-RICH Backend" /min cmd /c "python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000"
) else (
    echo [OK] Backend already running on :8000
)

rem ---- 3. Frontend Vite :5173 ----
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [INFO] Starting frontend on :5173 ...
    start "KOL-RICH Frontend" /min cmd /c "cd frontend && npm run dev"
) else (
    echo [OK] Frontend already running on :5173
)

rem ---- 4. Wait for frontend to be ready (max 30s) ----
echo [INFO] Waiting for frontend :5173 ...
set /a tries=0
:wait_loop
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% geq 30 goto ready
timeout /t 1 /nobreak >nul
goto wait_loop
:ready

rem ---- 5. Open browser ----
echo [INFO] Opening browser ...
start "" http://localhost:5173/

echo ============================================
echo   Done. Browser opened at http://localhost:5173/
echo ============================================
echo.
echo   Keep this window open. Close the two
echo   minimized service windows to stop the app.
pause
