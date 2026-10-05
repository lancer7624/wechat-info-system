@echo off
setlocal
title WeChat Bot Serve (inbound + reply)
cd /d "%~dp0"

rem interpreter: scripts\python_path.txt first (written by setup wizard), else python on PATH
set "PY=python"
if exist "scripts\python_path.txt" for /f "usebackq delims=" %%p in ("scripts\python_path.txt") do set "PY=%%p"
if not exist "%PY%" set "PY=python"

if not exist "config.json" (
  echo [ERROR] config.json not found - run setup first, or copy config.example.json.
  pause
  exit /b 1
)

echo.
echo Starting WeChat bot bridge (long-poll in, reply out)...
echo Press Ctrl+C to stop. First message must come from YOU in WeChat.
echo.
"%PY%" -u "scripts\wx_serve.py"
echo.
echo [stopped]
pause
