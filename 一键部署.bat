@echo off
setlocal
title WeChat Info System - One-click Setup
set "WZ=%~dp0scripts\setup_wizard.py"
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3 not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/windows/
    echo Remember to check "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)
%PY% "%WZ%" %*
echo.
pause
