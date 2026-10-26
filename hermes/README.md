# Hermes Agent for the ME3 assistant

Hermes is the background brain. The assistant's fast model (OpenRouter) handles every turn and
calls `delegate(task)` for multi-step or open-world requests; Hermes runs the task on its own,
and the result is spoken by the fast model when it arrives. See `assistant/tasks.py`.

Same setup on the laptop and the Pi:

| | Windows laptop | Raspberry Pi 4 |
|---|---|---|
| install + configure | `powershell -ExecutionPolicy Bypass -File hermes\setup.ps1` | `bash hermes/setup.sh` |
| Hermes home | `%LOCALAPPDATA%\hermes` | `~/.hermes` |
| service | Scheduled Task at login | systemd user unit |
| config | `config.yaml` (this folder, copied) | same |
| secrets | `.env` in Hermes home, from `env.example` | same |

Steps (Windows commands are for Command Prompt, run from the project root):
1. Run the setup script once. It installs Hermes if needed. A fresh install gets `.env` from
   `env.example`; an existing install keeps Hermes's own `.env`. Either way it stops and asks for the keys.
2. Make a random API key (Pi: `openssl rand -hex 20`; Windows:)
   `powershell -Command "-join (1..40 | ForEach-Object { 'abcdefghijklmnopqrstuvwxyz0123456789'.ToCharArray() | Get-Random })"`
3. Edit Hermes's `.env` (`notepad %LOCALAPPDATA%\hermes\.env`, or `nano ~/.hermes/.env` on the Pi).
   Hermes's own file has most lines commented out: remove the `# ` in front of `OPENROUTER_API_KEY`
   and put the "me3-hermes" key after the `=`. Add the API server lines at the end if missing:

       OPENROUTER_API_KEY=sk-or-...
       API_SERVER_ENABLED=true
       API_SERVER_HOST=127.0.0.1
       API_SERVER_PORT=8642
       API_SERVER_KEY=<the random string>

4. Run the setup script again. It copies `config.yaml`, installs the login service and checks
   `http://127.0.0.1:8642/v1/models` for up to 30 s. On Windows answer `Y` to its questions and
   approve the UAC prompt. "Another gateway instance ... Exiting" is harmless.
5. Put the same `API_SERVER_KEY` value in the assistant's `.env` as `HERMES_API_KEY`, then
   `python -m assistant.main --check`.

Tested with Hermes Agent v0.20.6 (its `gateway restart` takes no `-y`).

Without Hermes (`HERMES_ENABLED=0`, or the API server down) the assistant runs as before,
just without the delegate tool.
