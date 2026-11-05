@echo off
rem Windows launcher: double-click to start the MCP servers, the pipeline and the UI.
rem Uses v8\.venv if present, else the ME3 project venv two folders up, else "python" on PATH.
cd /d "%~dp0.."
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "app\launcher.py" %*
) else if exist "..\..\.venv\Scripts\pythonw.exe" (
  start "" "..\..\.venv\Scripts\pythonw.exe" "app\launcher.py" %*
) else (
  start "" pythonw "app\launcher.py" %*
)
