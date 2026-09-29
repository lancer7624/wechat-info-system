@echo off
rem ============================================
rem  WeChat Assistant - headless analysis entry
rem  Usage: run_analysis.bat <noon|evening|trip|daily>
rem  Agent CLI (claude / gemini / codex / ...) is picked by
rem  scripts\agent_runner.py from config.json agent section,
rem  or auto-detected. See README FAQ.
rem ============================================
setlocal

set "BATCH=%~1"
if "%BATCH%"=="" set "BATCH=noon"

set "SYS=%~dp0.."
set "ROOT=%~dp0.."
set "LOG=%SYS%\analysis.log"
set "RUNNER=%SYS%\scripts\agent_runner.py"

if not exist "%SYS%\prompts\prompt_%BATCH%.md" (
  echo [%date% %time%] [%BATCH%] prompt file missing >> "%LOG%"
  exit /b 1
)
if not exist "%RUNNER%" (
  echo [%date% %time%] [%BATCH%] agent_runner.py not found >> "%LOG%"
  exit /b 1
)

echo [%date% %time%] [%BATCH%] start >> "%LOG%"
rem load API env from VSCode settings (same source as interactive session)
for /f "usebackq tokens=1,* delims==" %%a in (`python "%SYS%\scripts\load_env.py"`) do set "%%a=%%b"
if defined ANTHROPIC_BASE_URL (echo [env] ANTHROPIC_BASE_URL set >> "%LOG%") else (echo [env] ANTHROPIC_BASE_URL MISSING >> "%LOG%")
if defined ANTHROPIC_AUTH_TOKEN (echo [env] ANTHROPIC_AUTH_TOKEN set >> "%LOG%") else (echo [env] ANTHROPIC_AUTH_TOKEN MISSING >> "%LOG%")
if defined ANTHROPIC_MODEL (echo [env] ANTHROPIC_MODEL set >> "%LOG%") else (echo [env] ANTHROPIC_MODEL MISSING >> "%LOG%")
cd /d "%ROOT%"
chcp 65001 >nul

python "%RUNNER%" %BATCH% "%SYS%\prompts\prompt_%BATCH%.md" "%SYS%\run_tmp_%BATCH%.log"
if not "%ERRORLEVEL%"=="0" (
  echo [%date% %time%] [%BATCH%] retry after 60s >> "%LOG%"
  timeout /t 60 /nobreak >nul
  python "%RUNNER%" %BATCH% "%SYS%\prompts\prompt_%BATCH%.md" "%SYS%\run_tmp_%BATCH%.log" --append
)

rem keep agent exit code first: type/del below reset errorlevel to 0
set "RC=%ERRORLEVEL%"
type "%SYS%\run_tmp_%BATCH%.log" >> "%LOG%" 2>nul
del "%SYS%\run_tmp_%BATCH%.log" 2>nul
echo [%date% %time%] [%BATCH%] end exit=%RC% >> "%LOG%"
if not "%RC%"=="0" python "%SYS%\scripts\notify_fail.py" %BATCH% %RC% >> "%LOG%" 2>&1
exit /b %RC%
