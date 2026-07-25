@echo off
setlocal
cd /d "%~dp0.."

if not exist "logs" mkdir "logs"
if not exist ".venv\Scripts\python.exe" (
  echo 仮想環境がありません。先にこのフォルダで uv sync を実行してください。
  exit /b 1
)

".venv\Scripts\python.exe" -m codex_agent.main %*
exit /b %ERRORLEVEL%
