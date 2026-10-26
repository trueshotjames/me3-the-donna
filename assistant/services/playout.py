"""Adaptive playout (jitter) buffer for streamed TTS audio.

The speaker drains audio in real time. When chunks arrive faster than real time (good link) every
chunk is released at once, so the buffer adds no delay. When a chunk arrives after the speaker has
already run dry (an underrun, heard as a stutter), the target buffer grows by the gap; later audio
is then held until enough is queued to cover the next stall: one short pause instead of many
stutters. The target shrinks again once sentences arrive comfortably faster than real time.

The model of the speaker is a single clock, ``play_until``: when the audio released so far will
have finished playing. It is shared across sentences (audio still queued from the previous
sentence counts as cushion) and reset on barge-in, when the transport drops its queue.
"""

from __future__ import annotations

import time
from typing import AsyncIterator, Callable

from loguru import logger


class AdaptivePlayout:
    def __init__(
        self,
        sample_rate: int,
        *,
        start_ms: float = 0.0,
        max_ms: float = 1500.0,
        max_hold_ms: float = 1000.0,
        tolerance_ms: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bytes_per_ms = sample_rate * 2 / 1000  # 16-bit mono
        self.target_ms = float(start_ms)
        self.max_ms = float(max_ms)
        self.max_hold_s = max_hold_ms / 1000  # never keep the speaker waiting longer than this
        self.tolerance_s = tolerance_ms / 1000
        self.clock = clock
        self.play_until = 0.0
        self.totals = {"underruns": 0, "underrun_ms": 0.0, "hold_ms": 0.0, "sentences": 0}

    def reset(self) -> None:
        """Barge-in: the transport dropped its queue, so nothing is playing any more."""
        self.play_until = 0.0

    def _ms(self, n_bytes: int) -> float:
        return n_bytes / self.bytes_per_ms

    async def pace(
        self, source: AsyncIterator[bytes], rec: dict | None = None, on_hold: Callable[[], None] | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the source's audio, holding some back only while the link is slower than real time."""
        rec = rec if rec is not None else {}
        held = bytearray()
        playing = False  # this sentence has released audio and is meant to be audible
        underruns, underrun_ms, hold_ms = 0, 0.0, 0.0
        t_first = t_last = t_hold = None
        received = 0
        rec["jb_target_ms"] = round(self.target_ms)
        async for chunk in source:
            now = self.clock()
            t_first = now if t_first is None else t_first
            t_last = now
            received += len(chunk)
            if not playing and t_hold is None and self.play_until and now > self.play_until:
                # The speaker went quiet between sentences (next sentence's audio was late).
                rec["jb_join_gap_ms"] = round((now - self.play_until) * 1000)
            if playing and not held and now > self.play_until + self.tolerance_s:
                gap_s = now - self.play_until
                underruns += 1
                underrun_ms += gap_s * 1000
                self.target_ms = min(self.max_ms, self.target_ms + gap_s * 1000 + 100)
                playing = False  # rebuffer up to the new target
            held += chunk
            cushion_ms = max(0.0, self.play_until - now) * 1000 + self._ms(len(held))
            # Once audible, feed the speaker at once; hold only at sentence start or after an underrun.
            waited_s = (now - t_hold) if t_hold is not None else 0.0
            if playing or cushion_ms >= self.target_ms or waited_s >= self.max_hold_s:
                if t_hold is not None:
                    hold_ms += (now - t_hold) * 1000
                    t_hold = None
                self.play_until = max(self.play_until, now) + self._ms(len(held)) / 1000
                playing = True
                out = bytes(held)
                held.clear()
                yield out
            else:
                if t_hold is None:
                    t_hold = now
                if on_hold:
                    on_hold()
        if held:  # end of sentence: play whatever is left
            now = self.clock()
            if t_hold is not None:
                hold_ms += (now - t_hold) * 1000
            self.play_until = max(self.play_until, now) + self._ms(len(held)) / 1000
            yield bytes(held)

        # Shrink only when this sentence arrived comfortably faster than real time.
        audio_ms = self._ms(received)
        span_ms = ((t_last - t_first) * 1000) if t_first is not None else 0.0
        rate = audio_ms / span_ms if span_ms > 50 else None  # None: all audio came at once
        if underruns == 0 and audio_ms > 0 and (rate is None or rate >= 2.0):
            self.target_ms = 0.0  # clearly fast again: back to no added delay at once
        elif underruns == 0 and audio_ms > 0 and rate >= 1.3:
            self.target_ms = 0.0 if self.target_ms < 40 else self.target_ms * 0.5
        elif rate is not None and rate < 0.95:
            # Slower than real time: aim for the hold that would have played this sentence without a
            # gap; after a clean sentence, ease down towards it instead of jumping.
            needed = audio_ms * (1 / rate - 1)
            floor = self.target_ms if underruns else self.target_ms * 0.85
            self.target_ms = min(self.max_ms, max(needed, floor))
        self.totals["sentences"] += 1
        self.totals["underruns"] += underruns
        self.totals["underrun_ms"] += underrun_ms
        self.totals["hold_ms"] += hold_ms
        rec.update(
            jb_underruns=underruns,
            jb_underrun_ms=round(underrun_ms),
            jb_hold_ms=round(hold_ms),
            jb_arrival_rate=round(rate, 2) if rate is not None else None,
            jb_target_after_ms=round(self.target_ms),
        )
        if underruns or hold_ms > 0 or rec["jb_target_ms"] != rec["jb_target_after_ms"]:
            logger.info(
                f"TTS playout: {underruns} underrun(s) ({underrun_ms:.0f} ms), held {hold_ms:.0f} ms, "
                f"arrival {rate if rate is not None else 'burst'}x real time, "
                f"buffer {rec['jb_target_ms']} -> {rec['jb_target_after_ms']} ms"
            )
