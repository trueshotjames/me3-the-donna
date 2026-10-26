"""Adaptive TTS playout buffer: simulated links, virtual clock (no network, no Pipecat)."""

import asyncio
import random

import pytest

from assistant.services.playout import AdaptivePlayout

SR = 16000
CHUNK = SR * 2 * 80 // 1000  # 80 ms blocks, like the TTS server


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def arrivals(n_chunks, rate, jitter_ms=0.0, first_ms=200.0, seed=0):
    """Arrival times (s, relative) of n 80 ms chunks delivered at `rate` x real time with jitter."""
    rnd = random.Random(seed)
    t, out = first_ms / 1000, []
    for _ in range(n_chunks):
        out.append(t)
        t += 0.080 / rate + max(-0.05, rnd.gauss(0, jitter_ms / 1000))
    return out


async def play(pl, clock, times):
    """Run one sentence; return (start delay from first arrival, audible gaps in ms)."""
    t0 = clock.t

    async def source():
        for at in times:
            clock.t = max(clock.t, t0 + at)
            yield b"\0" * CHUNK

    speaker_end, start, gaps = None, None, []
    async for out in pl.pace(source(), {}):
        now = clock.t
        if start is None:
            start = now - (t0 + times[0])
        elif speaker_end is not None and now > speaker_end + 0.005:
            gaps.append((now - speaker_end) * 1000)
        speaker_end = max(speaker_end or now, now) + len(out) / (SR * 2)
    clock.t = speaker_end  # let the sentence finish playing
    return start * 1000, gaps


async def run(rate, sentences=4, jitter_ms=0.0, max_ms=1500.0):
    clock = Clock()
    pl = AdaptivePlayout(SR, max_ms=max_ms, clock=clock)
    res = []
    for i in range(sentences):
        res.append(await play(pl, clock, arrivals(40, rate, jitter_ms, seed=i)))  # 3.2 s sentences
    return pl, res


async def test_fast_link_adds_no_delay():
    pl, res = await run(rate=3.0, jitter_ms=10)
    assert all(start == 0 and not gaps for start, gaps in res)
    assert pl.target_ms == 0 and pl.totals["underruns"] == 0


async def test_slow_link_fewer_stutters_than_no_buffer():
    _, base = await run(rate=0.8, jitter_ms=30, max_ms=0)  # max 0 = buffer disabled
    _, adapt = await run(rate=0.8, jitter_ms=30)
    base_gaps = sum(len(g) for _, g in base)
    adapt_gaps = sum(len(g) for _, g in adapt)
    # The first sentences on the slow link teach the buffer; later ones play without a gap.
    assert base_gaps >= 20
    assert len(adapt[1][1]) <= 3 and all(not g for _, g in adapt[2:])
    assert all(start <= 1100 for start, _ in adapt)  # hold is capped (max_hold_ms 1000 + one chunk)
    assert adapt_gaps * 4 <= base_gaps


async def test_recovers_to_zero_when_link_improves():
    clock = Clock()
    pl = AdaptivePlayout(SR, clock=clock)
    for i in range(3):
        await play(pl, clock, arrivals(40, 0.8, 30, seed=i))
    assert pl.target_ms > 100
    for i in range(6):
        start, gaps = await play(pl, clock, arrivals(40, 3.0, 10, seed=i))
    assert pl.target_ms == 0 and start == 0 and not gaps


async def test_barge_in_drops_held_audio():
    clock = Clock()
    pl = AdaptivePlayout(SR, start_ms=1000, clock=clock)
    stall = asyncio.Event()

    async def source():
        for _ in range(3):
            clock.t += 0.2
            yield b"\0" * CHUNK
        await stall.wait()  # link stalls; the 240 ms held stays below the 1 s target

    got = []

    async def consume():
        async for out in pl.pace(source(), {}):
            got.append(out)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()  # Pipecat cancels run_tts on barge-in
    with pytest.raises(asyncio.CancelledError):
        await task
    pl.reset()
    assert got == [] and pl.play_until == 0.0
