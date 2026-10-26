#!/usr/bin/env bash
# Hermes Agent for the ME3 assistant on the Raspberry Pi (64-bit Pi OS) or any Linux box.
#
#   bash hermes/setup.sh            # install (if needed), copy config, start as a user service
#   bash hermes/setup.sh --no-install
#
# Same layout as setup.ps1 on Windows: our config.yaml and .env go into Hermes's home, Hermes
# itself lives in its own install with its own Python, and the assistant only talks to it over
# http://127.0.0.1:8642.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

if ! command -v hermes >/dev/null 2>&1 && [[ $INSTALL == 1 ]]; then
  echo "installing Hermes Agent (no browser toolset: saves ~1.6 GB on the Pi)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v hermes >/dev/null 2>&1 || { echo "hermes is not on PATH; open a new shell and rerun"; exit 1; }
echo "hermes: $(hermes --version 2>/dev/null | head -1)"

mkdir -p "$HERMES_HOME"
cp "$HERE/config.yaml" "$HERMES_HOME/config.yaml"
if [[ ! -f "$HERMES_HOME/.env" ]]; then
  cp "$HERE/env.example" "$HERMES_HOME/.env"
  chmod 600 "$HERMES_HOME/.env"
  echo "created $HERMES_HOME/.env: fill in OPENROUTER_API_KEY and API_SERVER_KEY, then rerun"
  exit 2
fi
for k in OPENROUTER_API_KEY API_SERVER_KEY; do
  if ! grep -Eq "^$k=.+" "$HERMES_HOME/.env"; then
    echo "$k is empty in $HERMES_HOME/.env"; exit 2
  fi
done
grep -Eq "^API_SERVER_ENABLED=true" "$HERMES_HOME/.env" || echo "API_SERVER_ENABLED=true" >> "$HERMES_HOME/.env"

# Our tools MCP server must be up for Hermes to list its tools; the assistant starts it on 8766.
echo "config copied to $HERMES_HOME/config.yaml"

# Run the gateway (API server only) as a user service so it survives reboots. `nice` keeps the
# audio pipeline ahead of Hermes when both want the CPU.
if hermes gateway status >/dev/null 2>&1; then
  hermes gateway restart
else
  hermes gateway install
  hermes gateway start
fi
sleep 3
KEY="$(grep -E '^API_SERVER_KEY=' "$HERMES_HOME/.env" | cut -d= -f2-)"
PORT="$(grep -E '^API_SERVER_PORT=' "$HERMES_HOME/.env" | cut -d= -f2- || true)"
PORT="${PORT:-8642}"
if curl -fsS -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" >/dev/null; then
  echo "Hermes API server answers on http://127.0.0.1:$PORT"
  echo "now put HERMES_API_KEY=$KEY in the assistant's .env and run: python -m assistant.main --check"
else
  echo "API server not answering yet; check: hermes gateway status / journalctl --user -u hermes-gateway"
  exit 1
fi
