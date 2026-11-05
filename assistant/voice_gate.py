"""v6: a cheap near-voice gate against TV / radio / other-room false starts (VOICE_GATE=1).

fdbench's false starts that v5b still answered were TV and radio clips: well-formed requests ("Can we talk about
something else?") that no text check can tell from the user. What differs is how they sound at the microphone: the
user speaks close to the speakerphone, a TV across the room arrives much quieter (fdbench clips: user speech at
about -12 to -21 dBFS, TV/radio -32 to -41, background talk -26 to -31). So each turn's speech level is compared with
the user's usual level, learned from turns she answered; a turn more than VOICE_GATE_DB quieter is not for her.

A second cue, a tiny spectral voice print (24 log band energies, mean-normalised, cosine similarity), is computed too.
VOICE_GATE_SPEAKER=shadow only logs it (default: fdbench and the scenarios use many synthetic voices for "the user",
so enforcing it there would reject real turns); =1 enforces it once enough turns are enrolled.

Cost: numpy on at most ~10 s of 16 kHz audio once per turn, about 1-3 ms on a laptop core (Pi 4 estimate 5-15 ms),
off the critical path of audio capture. No model, no new package.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from loguru import logger

FRAME = 480  # 30 ms at 16 kHz
N_BANDS = 24


@dataclass
class Verdict:
    level_db: float | None
    user_db: float
    far: bool
    similarity: float | None
    speaker_far: bool
    reason: str | None

    def as_dict(self) -> dict:
        return {
            "level_db": None if self.level_db is None else round(self.level_db, 1),
            "user_db": round(self.user_db, 1),
            "far": self.far,
            "similarity": None if self.similarity is None else round(self.similarity, 3),
            "speaker_far": self.speaker_far,
            "reason": self.reason,
        }


def speech_level_db(pcm: bytes, sample_rate: int = 16000) -> float | None:
    """90th percentile of 30 ms frame levels among the active frames (within 25 dB of the loudest), in dBFS."""
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    n = max(1, int(sample_rate * 0.03))
    if len(x) < n * 8:  # under ~0.25 s: too little to judge
        return None
    fr = x[: len(x) // n * n].reshape(-1, n)
    db = 10 * np.log10((fr * fr).mean(axis=1) + 1e-12)
    act = db[db > db.max() - 25]
    return float(np.percentile(act, 90)) if len(act) else None


def _bands(sample_rate: int, nfft: int) -> np.ndarray:
    """Mel-spaced triangular band matrix (N_BANDS x nfft//2+1), 100 Hz - 7 kHz."""
    def mel(f):
        return 2595 * np.log10(1 + f / 700)

    def hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    edges = hz(np.linspace(mel(100), mel(min(7000, sample_rate / 2 - 1)), N_BANDS + 2))
    bins = np.fft.rfftfreq(nfft, 1 / sample_rate)
    m = np.zeros((N_BANDS, len(bins)), dtype=np.float32)
    for i in range(N_BANDS):
        lo, c, hi = edges[i], edges[i + 1], edges[i + 2]
        m[i] = np.clip(np.minimum((bins - lo) / (c - lo), (hi - bins) / (hi - c)), 0, None)
    return m


_BAND_CACHE: dict[tuple[int, int], np.ndarray] = {}


def voice_print(pcm: bytes, sample_rate: int = 16000) -> np.ndarray | None:
    """Mean log band energy of the active frames, mean-normalised and unit length; None if too short."""
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    nfft = 512
    hop = 256
    if len(x) < nfft * 10:
        return None
    key = (sample_rate, nfft)
    if key not in _BAND_CACHE:
        _BAND_CACHE[key] = _bands(sample_rate, nfft)
    idx = np.arange(0, len(x) - nfft, hop)[:, None] + np.arange(nfft)[None, :]
    frames = x[idx] * np.hanning(nfft).astype(np.float32)
    energy = (frames * frames).mean(axis=1)
    keep = energy > energy.max() * 10 ** (-2.5)  # active frames: within 25 dB of the loudest
    if keep.sum() < 8:
        return None
    spec = np.abs(np.fft.rfft(frames[keep], axis=1)) ** 2
    logb = np.log(spec @ _BAND_CACHE[key].T + 1e-10).mean(axis=0)
    v = logb - logb.mean()
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else None


class VoiceGate:
    def __init__(
        self,
        *,
        enabled: bool = True,
        gap_db: float = 10.0,
        seed_db: float = -16.0,
        speaker_mode: str = "shadow",
        speaker_min: float = 0.80,
        sample_rate: int = 16000,
        enroll_turns: int = 3,
        calibration: dict | None = None,
        music_gap_db: float = 5.0,
        music_speaker_min: float = 0.70,
    ):
        self.enabled = enabled
        self.gap_db = gap_db
        self.seed_db = seed_db
        self.speaker_mode = speaker_mode if speaker_mode in ("0", "shadow", "1") else "shadow"
        self.speaker_min = speaker_min
        self.sample_rate = sample_rate
        self.enroll_turns = enroll_turns
        self._levels: deque[float] = deque(maxlen=12)
        self._print: np.ndarray | None = None
        self._prints = 0
        # v9 (calibration, assistant/calibrate.py): the user's level and voice print measured in this room, and how
        # loud leftover music arrives at the mic; used from the first turn instead of the fixed seed
        self.music_gap_db = music_gap_db
        self.music_speaker_min = music_speaker_min
        import os

        self.music_speaker_enforce = os.getenv("MUSIC_SPEAKER_ENFORCE", "0") in ("1", "true", "yes")
        self.music_db: float | None = None
        self.calibrated = False
        cal = calibration or {}
        if isinstance(cal.get("user_db"), (int, float)):
            self.seed_db = float(cal["user_db"])
            self.calibrated = True
        if isinstance(cal.get("music_db"), (int, float)):
            self.music_db = float(cal["music_db"])
        if isinstance(cal.get("print"), list) and len(cal["print"]) == N_BANDS:
            self._print = np.asarray(cal["print"], dtype=np.float32)
            self._prints = enroll_turns
        self.stats = {"judged": 0, "far": 0, "speaker_far_shadow": 0, "learned": 0, "ms_total": 0.0, "ms_max": 0.0,
                      "music_far": 0}

    def far_db(self) -> float:
        """A voice quieter than this is not for her. While music plays: halfway between the user's level and the
        calibrated music leak (and at least 4 dB above the leak), else MUSIC_GAP_DB under the user; never closer
        than 3 dB to the user's level."""
        from assistant.music import MUSIC

        user = self.user_db
        if not MUSIC.playing:
            return user - self.gap_db
        if self.music_db is not None:
            thr = max((user + self.music_db) / 2, self.music_db + 4.0)
            return min(user - 3.0, max(thr, user - self.gap_db))
        return user - self.music_gap_db

    @property
    def user_db(self) -> float:
        """The user's usual level: median of recent answered turns, kept within 6 dB of the seed level."""
        if not self._levels:
            return self.seed_db
        med = float(np.median(self._levels))
        return min(max(med, self.seed_db - 6), self.seed_db + 6)

    def judge(self, pcm: bytes) -> Verdict:
        t0 = time.perf_counter()
        level = speech_level_db(pcm, self.sample_rate)
        user = self.user_db
        from assistant.music import MUSIC

        music = MUSIC.playing
        far_db = self.far_db()
        far = self.enabled and level is not None and level < far_db
        sim = None
        speaker_far = False
        if self.speaker_mode != "0":
            vp = voice_print(pcm, self.sample_rate)
            if vp is not None and self._print is not None and self._prints >= self.enroll_turns:
                sim = float(np.dot(vp, self._print / (np.linalg.norm(self._print) or 1)))
                speaker_far = sim < (self.music_speaker_min if music else self.speaker_min)
        reason = None
        if far:
            if music:
                self.stats["music_far"] += 1
                reason = f"music or a far voice ({level:.0f} dB; while music plays a voice needs {far_db:.0f} dB)"
            else:
                reason = f"far-away voice ({level:.0f} dB vs your usual {user:.0f} dB): TV, radio or another room"
        elif speaker_far and (self.speaker_mode == "1" or (music and self.enabled and self.music_speaker_enforce)):
            # v9 tried enforcing the voice print while music plays; King's own short commands under music then scored
            # -0.01 to 0.60 ("Next song, please" refused twice), so it is off by default (MUSIC_SPEAKER_ENFORCE=1 on).
            # The level gate and ducking the music on a near voice do the work.
            reason = f"a different voice (voice print similarity {sim:.2f})"
        ms = (time.perf_counter() - t0) * 1000
        self.stats["judged"] += 1
        self.stats["ms_total"] += ms
        self.stats["ms_max"] = max(self.stats["ms_max"], ms)
        if far:
            self.stats["far"] += 1
        if speaker_far:
            self.stats["speaker_far_shadow"] += 1
        v = Verdict(level, user, far, sim, speaker_far, reason)
        logger.info(f"voice gate: {v.as_dict()} ({ms:.1f} ms)")
        return v

    def learn(self, pcm: bytes, verdict: Verdict | None = None) -> None:
        """An answered, near turn: update the user's level and voice print."""
        level = verdict.level_db if verdict is not None else speech_level_db(pcm, self.sample_rate)
        if level is None or level < self.far_db():
            return
        self._levels.append(level)
        self.stats["learned"] += 1
        if self.speaker_mode != "0":
            vp = voice_print(pcm, self.sample_rate)
            if vp is not None:
                self._print = vp if self._print is None else 0.8 * self._print + 0.2 * vp
                self._prints += 1


# ---------------------------------------------------------------- per-turn flags shared with the LLM service
#
# The STT service decides things about the turn it is about to commit (far voice, a backchannel that interrupted her);
# the LLM service reads them when it builds the request for that transcript. Keyed by the normalised transcript so a
# flag never leaks onto a different turn.


def norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() or c == "'" else " " for c in text or "").split())


class TurnFlags:
    def __init__(self):
        self._flags: dict[str, tuple[float, dict]] = {}

    def set(self, transcript: str, **flags) -> None:
        now = time.time()
        self._flags = {k: v for k, v in self._flags.items() if now - v[0] < 60}
        key = norm(transcript)
        old = self._flags.get(key, (now, {}))[1]
        self._flags[key] = (now, {**old, **flags})

    def get(self, text: str) -> dict:
        """Flags for a user message (the turn note may already be appended: match on its start)."""
        key = norm(text)
        best = {}
        for k, (_, flags) in self._flags.items():
            if k and (key == k or key.startswith(k + " ")):
                best = flags
        return best


FLAGS = TurnFlags()
