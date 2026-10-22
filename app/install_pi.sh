#!/usr/bin/env bash
# One-time setup on the Raspberry Pi: installs the pywebview GTK backend,
# creates the venv, and puts a double-clickable "Voice Assistant" icon on the Desktop.
# Run from the project root:  bash app/install_pi.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

sudo apt-get update
sudo apt-get install -y python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libportaudio2

if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv --system-site-packages "$ROOT/.venv"   # system-site-packages so pywebview sees the GTK bindings
fi
"$ROOT/.venv/bin/pip" install -r "$ROOT/app/requirements-app.txt"
[ -f "$ROOT/requirements.txt" ] && "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"

DESKTOP_FILE="$HOME/Desktop/voice-assistant.desktop"
sed -e "s#/home/pi/me3#$ROOT#g" "$ROOT/app/voice-assistant.desktop" > "$DESKTOP_FILE"
chmod +x "$DESKTOP_FILE"
gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true   # lets the Pi OS desktop launch it on double-click
mkdir -p "$HOME/.local/share/applications" && cp "$DESKTOP_FILE" "$HOME/.local/share/applications/"
echo "Done. Double-click 'Voice Assistant' on the Desktop, or run: $ROOT/.venv/bin/python app/launcher.py"
