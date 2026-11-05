#!/usr/bin/env bash
# One-time setup on the Pi: Donna as a service that starts at boot, with sound cues, no UI needed.
#
#   cd ~/Documents/MLOps/"Machine Exercise 3"/me3-the-donna && bash pi/setup_service.sh ["king james" ...]
#
# Asks for the Pi password once (sudo) for steps 1-3. Arguments: Wi-Fi connections that should not use IPv6
# (default: "king james", the phone hotspot; on it Deepgram over IPv6/NAT64 timed out while IPv4 worked).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONNS=("$@"); [ ${#CONNS[@]} -eq 0 ] && CONNS=("king james")

echo "1/6 prefer IPv4 over IPv6 for every network (phone hotspot, USB tethering, home Wi-Fi)"
if ! grep -qE '^precedence ::ffff:0:0/96 +100' /etc/gai.conf 2>/dev/null; then
  echo 'precedence ::ffff:0:0/96  100   # donna: IPv4 first (hotspot NAT64 timed out Deepgram)' | sudo tee -a /etc/gai.conf >/dev/null
fi

echo "2/6 IPv6 off on: ${CONNS[*]}"
for c in "${CONNS[@]}"; do
  if nmcli -t -f NAME con show | grep -qx "$c"; then
    sudo nmcli con modify "$c" ipv6.method disabled && echo "    $c: IPv6 off (takes effect on its next connect)"
  else
    echo "    $c: no saved connection with that name, skipped"
  fi
done

echo "3/6 start user services at boot without logging in"
sudo loginctl enable-linger "$USER"

echo "4/6 install the donna service and the donna command"
mkdir -p ~/.config/systemd/user ~/.local/bin
sed "s|@REPO@|$REPO|g" "$REPO/pi/donna.service" > ~/.config/systemd/user/donna.service
sed "s|@REPO@|$REPO|g" "$REPO/pi/donna" > ~/.local/bin/donna
chmod +x ~/.local/bin/donna
grep -q 'HOME/.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

echo "5/6 desktop icon 'Donna logs' (opens a terminal with her live output)"
mkdir -p ~/Desktop ~/.local/share/applications
cat > ~/.local/share/applications/donna-logs.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Donna logs
Comment=Donna's live output (she keeps running when you close it)
Exec=lxterminal -t "Donna logs" -e $HOME/.local/bin/donna logs
Icon=utilities-terminal
Terminal=false
EOF
cp ~/.local/share/applications/donna-logs.desktop ~/Desktop/ 2>/dev/null || true

echo "6/6 stop the old tmux/launcher copy and start the service"
tmux kill-session -t donna 2>/dev/null || true
pkill -f '[.]venv/bin/python app/launcher' 2>/dev/null || true
pkill -f '[.]venv/bin/python -m assistant[.]main' 2>/dev/null || true
pkill -f '[.]venv/bin/python -m mcp_servers[.]tools' 2>/dev/null || true
sleep 2
systemctl --user daemon-reload
systemctl --user enable donna.service
systemctl --user restart donna.service
echo
echo "Done. Listen for: one beep (starting), then two rising notes (live)."
echo "Commands: donna status | donna logs | donna stop | donna start | donna restart  (open a new terminal first)"
