@echo off
setlocal EnableDelayedExpansion

rem ============================================================
rem   大V基金交易量化AI分析系统 启动脚本
rem
rem   防呆设计：
rem   1. 校验 .project_marker：缺失即拒绝启动（避免误启动别的项目）
rem   2. 端口检测时核对占用进程的 ImagePath，必须包含本目录
rem   3. 进程已运行（端口被本项目占用）→ 跳过；端口被别的项目占用 → 报错并提示
rem   4. 后端前端启动时显式设置窗口标题
rem ============================================================

cd /d "%~dp0"

echo ============================================
echo   大V基金交易量化AI分析系统
echo   %~dp0
echo ============================================

rem ---- 0. 项目根目录校验 ----
set "PROJECT_ROOT=%~dp0"
if not exist "%PROJECT_ROOT%\.project_marker" (
    echo [ERROR] 未检测到 .project_marker 文件，当前目录不是「%PROJECT_NAME%」项目根目录。
    echo         请在 E:\PM\PM\KOL-RICH\ 下运行 启动系统.bat
    echo         当前路径：%PROJECT_ROOT%
    pause
    exit /b 1
)

rem ---- 1. MySQL80 ----
echo [STEP 1] 检查 MySQL80 ...
sc query MySQL80 | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo [INFO] 启动 MySQL80 ...
    net start MySQL80
) else (
    echo [OK]   MySQL80 已在运行
)

rem ---- 2. Backend :8000 ----
echo [STEP 2] 检查后端 :8000 ...
set "BACKEND_OK=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    set "PID_8000=%%P"
    set "CMDLINE="
    for /f "tokens=*" %%I in ('wmic process where "ProcessId=%%P" get CommandLine /format:list 2^>nul ^| findstr /i "CommandLine="') do (
        set "CMDLINE=%%I"
    )
    call set "CMDLINE_PATH=%%CMDLINE:*%PROJECT_ROOT%=KOL%%"
    if /i "!CMDLINE_PATH!"=="KOL" (
        set "BACKEND_OK=1"
        echo [OK]   :8000 已被本项目占用（PID %%P）
    ) else (
        echo [WARN] :8000 被其它进程占用（PID %%P）。命令：%CMDLINE%
    )
)

if "%BACKEND_OK%"=="0" (
    if defined PID_8000 (
        echo [ERROR] 端口 8000 被其它项目占用，本项目后端无法启动。
        echo         请先停止占用 :8000 的进程（PID !PID_8000!），或修改 .project_marker 中的 backend_port。
        pause
        exit /b 1
    ) else (
        echo [INFO] 启动后端 :8000 ...
        start "KOL-RICH Backend" /min cmd /c "cd /d %PROJECT_ROOT% ^&^& python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000"
    )
)

rem ---- 3. Frontend :5173 ----
echo [STEP 3] 检查前端 :5173 ...
set "FRONTEND_OK=0"
set "PID_5173="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    set "PID_5173=%%P"
    set "CMDLINE="
    for /f "tokens=*" %%I in ('wmic process where "ProcessId=%%P" get CommandLine /format:list 2^>nul ^| findstr /i "CommandLine="') do (
        set "CMDLINE=%%I"
    )
    call set "CMDLINE_PATH=%%CMDLINE:*%PROJECT_ROOT%=KOL%%"
    if /i "!CMDLINE_PATH!"=="KOL" (
        set "FRONTEND_OK=1"
        echo [OK]   :5173 已被本项目占用（PID %%P）
    ) else (
        echo [WARN] :5173 被其它进程占用（PID %%P）。命令：%CMDLINE%
    )
)

if "%FRONTEND_OK%"=="0" (
    if defined PID_5173 (
        echo [ERROR] 端口 5173 被其它项目占用，本项目前端无法启动。
        echo         请先停止占用 :5173 的进程（PID !PID_5173!），或修改 .project_marker 中的 frontend_port。
        pause
        exit /b 1
    ) else (
        echo [INFO] 启动前端 :5173 ...
        start "KOL-RICH Frontend" /min cmd /c "cd /d %PROJECT_ROOT%\frontend ^&^& npm run dev"
    )
)

rem ---- 4. Wait for both ports ----
echo [STEP 4] 等待端口就绪（最久 30s）...
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
echo [STEP 5] 打开浏览器 ...
start "" "http://localhost:5173/"

echo ============================================
echo   已启动。后台窗口标题：KOL-RICH Backend / KOL-RICH Frontend
echo   关闭那两个最小化窗口即可停止服务。
echo ============================================
echo.
pause