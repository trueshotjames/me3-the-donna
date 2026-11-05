#!/usr/bin/env bash
# One-time setup on the Raspberry Pi 4 (64-bit Raspberry Pi OS Bookworm, with the desktop).
# Installs the system packages, creates the venv with every Python package, adds the Jabra
# phone-button rule, and puts a double-clickable "Voice Assistant" icon on the Desktop.
#
#   bash app/install_pi.sh             # pipeline, UI, Tapo, Telegram messages, Jabra buttons
#   bash app/install_pi.sh --spotify   # also Chromium + Widevine, for Donna as the Spotify speaker
#
# Run from the project root. Safe to run again. Nothing here touches .env or data/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPOTIFY=0
for a in "$@"; do [ "$a" = "--spotify" ] && SPOTIFY=1; done

if [ "$(uname -m)" != "aarch64" ]; then
  echo "WARNING: this is $(uname -m), not aarch64. Use the 64-bit Raspberry Pi OS: onnxruntime (Silero VAD) has no 32-bit ARM wheel."
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is needed (Raspberry Pi OS Bookworm has 3.11)."; exit 1; }

sudo apt-get update
# PyAudio has no aarch64 wheel on PyPI and builds from source: portaudio19-dev, python3-dev, build-essential.
# python3-gi + WebKit: pywebview's window. libusb/libudev: hidapi (Jabra buttons) if pip has to build it.
# python3-gpiozero + python3-lgpio: only for LIGHT_BACKEND=gpio (seen through --system-site-packages).
sudo apt-get install -y python3-venv python3-dev build-essential \
  python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  libportaudio2 portaudio19-dev libusb-1.0-0-dev libudev-dev \
  python3-gpiozero python3-lgpio
if [ "$SPOTIFY" = 1 ]; then
  # Spotify's Web Playback SDK needs Widevine DRM; Raspberry Pi OS packages it for its Chromium.
  sudo apt-get install -y chromium libwidevinecdm0 || sudo apt-get install -y chromium-browser libwidevinecdm0
fi

if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv --system-site-packages "$ROOT/.venv"   # system-site-packages so pywebview sees the GTK bindings
fi
"$ROOT/.venv/bin/pip" install --upgrade pip
"$ROOT/.venv/bin/pip" install -r "$ROOT/app/requirements-app.txt" -r "$ROOT/requirements.txt"

# Jabra Speak phone buttons (red = hang up, green = answer): the hidraw node must be readable without root.
RULE=/etc/udev/rules.d/50-jabra.rules
if [ ! -f "$RULE" ]; then
  echo 'KERNEL=="hidraw*", ATTRS{idVendor}=="0b0e", MODE="0666"' | sudo tee "$RULE" >/dev/null
  sudo udevadm control --reload && sudo udevadm trigger || true
fi

[ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; echo "Created .env from .env.example: add the API keys."; }
mkdir -p "$ROOT/data" "$ROOT/app/logs"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/voice-assistant.desktop"
sed -e "s#/home/pi/me3#$ROOT#g" "$ROOT/app/voice-assistant.desktop" > "$DESKTOP_FILE"
chmod +x "$DESKTOP_FILE"
gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true   # lets the Pi OS desktop launch it on double-click
mkdir -p "$HOME/.local/share/applications" && cp "$DESKTOP_FILE" "$HOME/.local/share/applications/"

cat <<EOF

Done. Next:
  1. Edit $ROOT/.env (keys, devices; see README.md, "Raspberry Pi").
  2. $ROOT/.venv/bin/python -m assistant.main --list-devices   # find the speakerphone
  3. $ROOT/.venv/bin/python -m assistant.main --check          # keys and the three MCP servers
  4. Double-click 'Voice Assistant' on the Desktop, or: $ROOT/.venv/bin/python app/launcher.py
EOF
