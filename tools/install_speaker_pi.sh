#!/usr/bin/env bash
# Donna's own Spotify speaker on the Pi (64-bit Raspberry Pi OS): go-librespot v0.10.5, linux arm64.
# Run from the project folder:  bash tools/install_speaker_pi.sh
# Then:  python -m mcp_servers.tools.devices.speaker --login   (once; approve at spotify.com/pair from your phone)
set -euo pipefail

VER=v0.10.5
SHA=35274c1d3576090096d3459ecf76ee9c025258d19dc76f0bf724f95c525467e1
DIR="$(cd "$(dirname "$0")" && pwd)/go-librespot"
URL="https://github.com/devgianlu/go-librespot/releases/download/$VER/go-librespot_linux_arm64.tar.gz"

echo "Installing the audio decoder libraries (apt)..."
sudo apt-get install -y libogg0 libvorbis0a libvorbisenc2 libflac12 libmpg123-0 || \
  sudo apt-get install -y libogg0 libvorbis0a libvorbisenc2 libflac14 libmpg123-0

mkdir -p "$DIR"
TMP="$(mktemp -d)"
echo "Downloading go-librespot $VER..."
curl -fsSL "$URL" -o "$TMP/glr.tar.gz"
echo "$SHA  $TMP/glr.tar.gz" | sha256sum -c -
tar -xzf "$TMP/glr.tar.gz" -C "$DIR"
chmod +x "$DIR/go-librespot"
rm -rf "$TMP"

if command -v pactl >/dev/null 2>&1 && pactl info >/dev/null 2>&1; then
  BACKEND=pulseaudio   # PipeWire/PulseAudio: shares the speakerphone with Donna's voice
else
  BACKEND=alsa         # plain ALSA: "default" mixes through dmix
fi
echo
echo "Installed go-librespot $VER in $DIR"
echo "Add these to the Pi's .env (the speakerphone must be the default output):"
echo "  SPOTIFY_SPEAKER=1"
echo "  SPOTIFY_SPEAKER_BACKEND=$BACKEND"
echo "  SPOTIFY_SPEAKER_DEVICE=default"
echo "Then log the speaker in once:  python -m mcp_servers.tools.devices.speaker --login"
