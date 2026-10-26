"""Measure what the voice detector sees in your room, and suggest thresholds.

    .venv\\Scripts\\python -m benchmark.vad_calibrate          # 10 s quiet, then 10 s talking
    .venv\\Scripts\\python -m benchmark.vad_calibrate --secs 15

Run it with the assistant stopped (it needs the microphone). Stay quiet for the first
part, then talk normally from where you usually sit. It prints voice confidence and
loudness (Pipecat's 0..1 scale: 0.6 is about -50 dB) for both parts, and the values
to put in .env as VAD_CONFIDENCE and VAD_MIN_VOLUME. The aim is high recall: every
real word must pass, some noise passing too is fine (it only opens the Flux gate).
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import pyaudio
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from assistant.config import load
from assistant.main import resolve_device
from assistant.vad import _scalar

RATE = 16000
FRAME = 512  # Silero's frame at 16 kHz (32 ms)


def record(vad: SileroVADAnalyzer, stream, secs: float) -> list[tuple[float, float]]:
    out = []
    end = time.time() + secs
    while time.time() < end:
        buf = stream.read(FRAME, exception_on_overflow=False)
        conf = _scalar(vad.voice_confidence(buf))
        vol = _scalar(vad._get_smoothed_volume(buf))
        vad._prev_volume = vol
        out.append((conf, vol))
    return out


def pcts(vals: list[float]) -> str:
    a = np.array(vals)
    return "  ".join(f"p{p}={np.percentile(a, p):.2f}" for p in (10, 25, 50, 75, 90, 99))


def suggest(quiet: list[tuple[float, float]], talk: list[tuple[float, float]]) -> dict:
    """Thresholds from (confidence, loudness) frames of a quiet room and of normal speech.

    High recall: thresholds at the 5th percentile of your speech, but never below the quiet
    room's 90th percentile (else the gate would sit open on room noise). Shared by the CLI
    and by the in-app calibration (Settings panel), which taps the running pipeline's VAD.
    """
    if not quiet or not talk:
        raise ValueError("need frames for both the quiet part and the talking part")
    qc, qv = [c for c, _ in quiet], [v for _, v in quiet]
    speech = [(c, v) for c, v in talk if c >= 0.5] or talk  # drop pauses between words
    sc, sv = [c for c, _ in speech], [v for _, v in speech]
    conf = max(float(np.percentile(sc, 5)) * 0.9, float(np.percentile(qc, 90)) + 0.02, 0.1)
    vol = max(float(np.percentile(sv, 5)) * 0.95, float(np.percentile(qv, 90)) + 0.02)
    conf, vol = round(min(conf, 0.7), 2), round(min(vol, 0.9), 2)
    p = lambda a, q: round(float(np.percentile(np.array(a), q)), 2)  # noqa: E731
    return {
        "confidence": conf,
        "min_volume": vol,
        "speech_pass_pct": round(100 * sum(c >= conf and v >= vol for c, v in speech) / len(speech)),
        "quiet_pass_pct": round(100 * sum(c >= conf and v >= vol for c, v in quiet) / len(quiet)),
        "speech_frames": len(speech),
        "talk_frames": len(talk),
        "quiet_frames": len(quiet),
        "quiet": {"confidence": {str(q): p(qc, q) for q in (50, 90, 99)}, "loudness": {str(q): p(qv, q) for q in (50, 90, 99)}},
        "speech": {"confidence": {str(q): p(sc, q) for q in (5, 50, 95)}, "loudness": {str(q): p(sv, q) for q in (5, 50, 95)}},
        "too_quiet": bool(np.percentile(sv, 50) <= np.percentile(qv, 90)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=10.0)
    a = ap.parse_args()
    cfg = load()
    vad = SileroVADAnalyzer(params=VADParams())
    vad.set_sample_rate(RATE)
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=FRAME,
        input_device_index=resolve_device(cfg.audio_input_device, want_input=True),
    )
    try:
        print(f"Stay QUIET for {a.secs:.0f} s ...")
        quiet = record(vad, stream, a.secs)
        print(f"Now TALK normally for {a.secs:.0f} s ...")
        talk = record(vad, stream, a.secs)
    finally:
        stream.close()
        pa.terminate()

    qc, qv = [c for c, _ in quiet], [v for _, v in quiet]
    speech = [(c, v) for c, v in talk if c >= 0.5] or talk
    sc, sv = [c for c, _ in speech], [v for _, v in speech]
    print("\nquiet   confidence:", pcts(qc))
    print("quiet   loudness:  ", pcts(qv))
    print(f"speech  confidence: {pcts(sc)}   ({len(speech)} of {len(talk)} frames)")
    print("speech  loudness:  ", pcts(sv))

    r = suggest(quiet, talk)
    print(f"\nsuggested: VAD_CONFIDENCE={r['confidence']}  VAD_MIN_VOLUME={r['min_volume']}")
    print(f"  your speech frames passing: {r['speech_pass_pct']}%   quiet-room frames passing: {r['quiet_pass_pct']}%")
    print(f"  current .env: VAD_CONFIDENCE={cfg.vad_confidence}  VAD_MIN_VOLUME={cfg.vad_min_volume}")
    if r["too_quiet"]:
        print("  warning: your speech is barely louder than the room; move closer to the mic")


if __name__ == "__main__":
    main()
