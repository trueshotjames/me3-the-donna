@echo off
rem Windows launcher: double-click to start the MCP servers, the pipeline and the UI.
rem Uses the project venv if one exists (.venv\Scripts\python.exe), else "python" on PATH.
cd /d "%~dp0.."
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "app\launcher.py" %*
) else (
  start "" pythonw "app\launcher.py" %*
)
