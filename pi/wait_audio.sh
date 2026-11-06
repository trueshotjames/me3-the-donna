#!/usr/bin/env bash
# Runs before Donna starts: wait (at most 20 s) until PipeWire has found the Jabra and made it the default output.
# At boot it needs a few seconds; started too early, her voice would begin on the Pi's headphone jack.
for _ in $(seq 1 40); do
  wpctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | grep -qi jabra && exit 0
  sleep 0.5
done
echo "wait_audio: the Jabra is not the default output; starting Donna anyway" >&2
