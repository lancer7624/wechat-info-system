@echo off
rem WeChat Assistant - open kanban page
rem via open_kanban.py: keep recorder alive (127.0.0.1:8710), fall back to static server
python "%~dp0open_kanban.py"
if errorlevel 1 (
  echo.
  echo [启动失败] 请确认 python 在 PATH 中（见 README 一、环境要求）
  pause
)
