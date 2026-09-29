@echo off
rem ============================================
rem  WeChat Assistant - headless analysis entry
rem  Usage: run_analysis.bat <noon|evening|trip|daily>
rem  Agent CLI (claude / gemini / codex / ...) is picked by
rem  scripts\agent_runner.py from config.json agent section,
rem  or auto-detected. See README FAQ.
rem  Interpreter: scripts\python_path.txt (written by the setup wizard)
rem  if present, else "python" from PATH. Its folder is prepended to
rem  PATH so the agent's inner python calls resolve the same way.
rem ============================================
setlocal

set "BATCH=%~1"
if "%BATCH%"=="" set "BATCH=noon"

rem normalize to an absolute path without "..": cmd's cd /d and type
rem choke on paths that still contain ..
for %%i in ("%~dp0..") do set "SYS=%%~fi"
set "ROOT=%SYS%"
set "LOG=%SYS%\analysis.log"
set "RUNNER=%SYS%\scripts\agent_runner.py"

set "PY=python"
if exist "%~dp0python_path.txt" for /f "usebackq delims=" %%p in ("%~dp0python_path.txt") do set "PY=%%p"
if not exist "%PY%" set "PY=python"
if not "%PY%"=="python" for %%d in ("%PY%") do set "PATH=%%~dpd;%PATH%"

if not exist "%SYS%\prompts\prompt_%BATCH%.md" (
  echo [%date% %time%] [%BATCH%] prompt file missing >> "%LOG%"
  exit /b 1
)
if not exist "%RUNNER%" (
  echo [%date% %time%] [%BATCH%] agent_runner.py not found >> "%LOG%"
  exit /b 1
)

echo [%date% %time%] [%BATCH%] start >> "%LOG%"
echo [env] python=%PY% >> "%LOG%"
rem load API env from VSCode settings (same source as interactive session)
for /f "usebackq tokens=1,* delims==" %%a in (`"%PY%" "%SYS%\scripts\load_env.py"`) do set "%%a=%%b"
if defined ANTHROPIC_BASE_URL (echo [env] ANTHROPIC_BASE_URL set >> "%LOG%") else (echo [env] ANTHROPIC_BASE_URL MISSING >> "%LOG%")
if defined ANTHROPIC_AUTH_TOKEN (echo [env] ANTHROPIC_AUTH_TOKEN set >> "%LOG%") else (echo [env] ANTHROPIC_AUTH_TOKEN MISSING >> "%LOG%")
if defined ANTHROPIC_MODEL (echo [env] ANTHROPIC_MODEL set >> "%LOG%") else (echo [env] ANTHROPIC_MODEL MISSING >> "%LOG%")
cd /d "%ROOT%"
chcp 65001 >nul

"%PY%" "%RUNNER%" %BATCH% "%SYS%\prompts\prompt_%BATCH%.md" "%SYS%\run_tmp_%BATCH%.log"
if not "%ERRORLEVEL%"=="0" (
  echo [%date% %time%] [%BATCH%] retry after 60s >> "%LOG%"
  rem not timeout: with no console (scheduled task / redirected stdin)
  rem it exits immediately and never waits the 60s
  ping -n 61 127.0.0.1 >nul
  "%PY%" "%RUNNER%" %BATCH% "%SYS%\prompts\prompt_%BATCH%.md" "%SYS%\run_tmp_%BATCH%.log" --append
)

rem keep agent exit code first: type/del below reset errorlevel to 0
set "RC=%ERRORLEVEL%"
type "%SYS%\run_tmp_%BATCH%.log" >> "%LOG%" 2>nul
del "%SYS%\run_tmp_%BATCH%.log" 2>nul
echo [%date% %time%] [%BATCH%] end exit=%RC% >> "%LOG%"
if not "%RC%"=="0" "%PY%" "%SYS%\scripts\notify_fail.py" %BATCH% %RC% >> "%LOG%" 2>&1
exit /b %RC%
