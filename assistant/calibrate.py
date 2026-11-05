"""Room calibration for the voice gate (v9): run it once in each room, before the demo, with Donna closed.

    python -m assistant.calibrate            # laptop or Pi; ~1 minute; uses AUDIO_INPUT_DEVICE from .env

It measures, through the same microphone and settings Donna uses:
    1. the room's noise floor (3 s of quiet),
    2. your voice: its level and a small voice print (3 short sentences),
    3. optionally, how loud music leaks through the speakerphone's echo canceller (8 s of a song you start).
and writes data/calibration.json. Donna loads it at start-up (assistant/voice_gate.py): your level replaces the
fixed -16 dB guess, your voice print is enrolled from the first turn, and while music plays a voice must be louder
than the point halfway between your voice and the music leak. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

PHRASES = [
    "Hey Donna, what's the weather like today?",
    "Set a timer for ten minutes and play some music.",
    "Turn on the living room light, please.",
]


def calibration_path(name: str | None = None) -> Path:
    from assistant.config import DATA_DIR

    p = Path(name or "calibration.json")
    return p if p.is_absolute() else DATA_DIR / p


def load_calibration(name: str | None = None) -> dict[str, Any] | None:
    """The saved calibration, or None (Donna then uses the fixed seed level and learns as before)."""
    p = calibration_path(name)
    try:
        cal = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        from loguru import logger

        music = f", music leak {cal['music_db']:.1f} dB" if isinstance(cal.get("music_db"), (int, float)) else ""
        logger.info(f"calibration from {cal.get('when', '?')}: your voice {cal.get('user_db', 0):.1f} dB, "
                    f"room noise {cal.get('noise_db', 0):.1f} dB{music}")
    except Exception:  # noqa: BLE001
        pass
    return cal


def frame_db(pcm: bytes, rate: int) -> np.ndarray:
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    n = max(1, int(rate * 0.03))
    fr = x[: len(x) // n * n].reshape(-1, n)
    return 10 * np.log10((fr * fr).mean(axis=1) + 1e-12)


def summarize(noise_pcm: bytes, voice_pcms: list[bytes], music_pcm: bytes | None, rate: int) -> dict[str, Any]:
    """The numbers saved to calibration.json (pure function; tested without a microphone)."""
    from assistant.voice_gate import speech_level_db, voice_print

    noise_db = float(np.median(frame_db(noise_pcm, rate)))
    levels, prints = [], []
    for pcm in voice_pcms:
        lv = speech_level_db(pcm, rate)
        if lv is not None and lv > noise_db + 8:
            levels.append(lv)
            vp = voice_print(pcm, rate)
            if vp is not None:
                prints.append(vp)
    if not levels:
        raise ValueError("no clear speech was heard; check AUDIO_INPUT_DEVICE and speak at your normal distance")
    user_db = float(np.median(levels))
    out: dict[str, Any] = {"user_db": round(user_db, 1), "noise_db": round(noise_db, 1), "music_db": None,
                           "voice_levels_db": [round(v, 1) for v in levels], "sample_rate": rate,
                           "when": time.strftime("%Y-%m-%d %H:%M")}
    if prints:
        p = np.mean(prints, axis=0)
        p = p / (np.linalg.norm(p) or 1)
        out["print"] = [round(float(v), 5) for v in p]
    if music_pcm:
        m = speech_level_db(music_pcm, rate)
        out["music_db"] = round(float(m if m is not None else noise_db), 1)
        if prints:
            mp = voice_print(music_pcm, rate)
            if mp is not None:
                out["music_similarity"] = round(float(np.dot(mp, np.asarray(out["print"]))), 3)
    return out


def threshold_text(cal: dict[str, Any]) -> str:
    from assistant.voice_gate import VoiceGate
    from assistant.music import MUSIC

    gate = VoiceGate(calibration=cal)
    was = MUSIC.playing
    try:
        MUSIC.playing = False
        quiet = gate.far_db()
        MUSIC.playing = True
        music = gate.far_db()
    finally:
        MUSIC.playing = was
    return (f"Donna will answer voices louder than {quiet:.1f} dB, or louder than {music:.1f} dB while music plays "
            f"(your voice: {cal['user_db']:.1f} dB).")


def _record(stream, rate: int, secs: float) -> bytes:
    chunk = int(rate * 0.032)
    frames = []
    for _ in range(int(secs * rate / chunk)):
        frames.append(stream.read(chunk, exception_on_overflow=False))
    return b"".join(frames)


def main() -> int:
    from assistant.config import load
    from assistant.main import resolve_device

    cfg = load()
    import pyaudio

    rate = cfg.audio_in_sample_rate
    pa = pyaudio.PyAudio()
    try:
        dev = resolve_device(cfg.audio_input_device, want_input=True)
        name = pa.get_device_info_by_index(dev)["name"] if dev is not None else pa.get_default_input_device_info()["name"]
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True, input_device_index=dev,
                         frames_per_buffer=int(rate * 0.032))
    except Exception as e:  # noqa: BLE001
        print(f"Could not open the microphone ({e}). Close Donna first: only one program can use it.")
        return 1
    print(f"Microphone: {name}. Sit where you will sit for the demo and speak at your normal volume.\n")
    try:
        input("Step 1 of 3, room noise: stop any music, stay quiet, and press Enter...")
        stream.read(stream.get_read_available() or 1, exception_on_overflow=False)
        noise = _record(stream, rate, 3.0)
        print(f"   room noise: {np.median(frame_db(noise, rate)):.1f} dB\n")

        voices = []
        for i, phrase in enumerate(PHRASES, start=1):
            input(f"Step 2 of 3, your voice ({i}/{len(PHRASES)}): press Enter, then say:  \"{phrase}\"")
            stream.read(stream.get_read_available() or 1, exception_on_overflow=False)
            pcm = _record(stream, rate, 4.0)
            from assistant.voice_gate import speech_level_db

            lv = speech_level_db(pcm, rate)
            print(f"   heard you at {lv:.1f} dB\n" if lv is not None else "   heard nothing; it will be skipped\n")
            voices.append(pcm)

        music = None
        ans = input("Step 3 of 3, music leak (optional): start a song in Spotify at the volume you will use, "
                    "stay quiet, and press Enter (or type s and Enter to skip)... ").strip().lower()
        if ans != "s":
            stream.read(stream.get_read_available() or 1, exception_on_overflow=False)
            music = _record(stream, rate, 8.0)
            print("   done; you can pause the music.\n")

        cal = summarize(noise, voices, music, rate)
        cal["device"] = name
    except ValueError as e:
        print(f"Calibration failed: {e}")
        return 1
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    path = calibration_path(cfg.calibration_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cal, indent=1), encoding="utf-8")
    print(f"Saved {path}")
    print(f"   your voice {cal['user_db']} dB, room noise {cal['noise_db']} dB"
          + (f", music leak {cal['music_db']} dB" if cal.get("music_db") is not None else ""))
    print("   " + threshold_text(cal))
    if cal.get("music_db") is not None and cal["music_db"] > cal["user_db"] - 6:
        print("   WARNING: the music leaks almost as loud as your voice. Lower the Spotify volume and run this again.")
    print("Start Donna again to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
