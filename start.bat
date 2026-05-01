@echo off
REM ── Castillo Planset QC launcher ──────────────────────────────────────
REM 1. Kills anything still listening on backend (8000) or frontend (5173)
REM 2. Starts the FastAPI backend bound to 0.0.0.0 with V4 rules
REM 3. Starts the Vite dev server bound to 0.0.0.0
REM Engineers connect at: http://192.168.1.150:5173/

setlocal enabledelayedexpansion

set REPO=%~dp0
set BACKEND=%REPO%backend
set FRONTEND=%REPO%frontend

echo.
echo ── Castillo Planset QC ────────────────────────────────────────────
echo Repo: %REPO%
echo.

echo Killing any existing backend (port 8000) ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "LISTENING" ^| findstr /C:":8000 "') do (
    echo   taskkill PID %%P
    taskkill /F /PID %%P >nul 2>&1
)

echo Killing any existing frontend (port 5173) ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "LISTENING" ^| findstr /C:":5173 "') do (
    echo   taskkill PID %%P
    taskkill /F /PID %%P >nul 2>&1
)

REM Give Windows a moment to release the sockets before re-binding.
ping -n 2 127.0.0.1 >nul

echo.
echo Starting backend ...
start "Planset QC — backend (port 8000)" cmd /k "cd /d %BACKEND% && set RULES_FILE=rules_v4_draft.yaml&& python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

echo Starting frontend ...
start "Planset QC — frontend (port 5173)" cmd /k "cd /d %FRONTEND% && npm run dev"

echo.
echo ── Up and running ─────────────────────────────────────────────────
echo   This machine:   http://localhost:5173/
echo   Office LAN:     http://192.168.1.150:5173/
echo.
echo Close the two child windows to stop the servers.
echo (Or just re-run this launcher — it will kill them first.)
echo.
pause
