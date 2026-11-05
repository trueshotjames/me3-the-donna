"""v9 music-aware listening: calibration, the stricter gate while music plays, ducking, Spotify duck volume."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from assistant.music import MUSIC, MusicState
from assistant.voice_gate import VoiceGate

RATE = 16000


def tone(db: float, secs: float = 2.0, f: float = 220.0, seed: int = 0) -> bytes:
    """A voiced-like signal at about `db` dBFS (harmonics + a little noise)."""
    t = np.arange(int(RATE * secs)) / RATE
    rng = np.random.default_rng(seed)
    x = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 8)) + 0.05 * rng.standard_normal(len(t))
    x = x / np.sqrt(np.mean(x * x)) * 10 ** (db / 20)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


@pytest.fixture(autouse=True)
def music_off():
    MUSIC.playing, MUSIC.ducked = False, False
    yield
    MUSIC.playing, MUSIC.ducked = False, False


def test_gate_uses_calibration_and_is_stricter_with_music():
    g = VoiceGate(calibration={"user_db": -20.0, "music_db": -30.0})
    assert g.calibrated and g.user_db == -20.0
    assert g.far_db() == -30.0  # no music: the usual 10 dB gap
    MUSIC.playing = True
    assert g.far_db() == -25.0  # halfway between voice (-20) and music leak (-30)
    assert g.judge(tone(-28)).far is True and "music" in g.judge(tone(-28)).reason
    assert g.judge(tone(-21)).far is False


def test_gate_with_music_but_no_calibration_uses_music_gap():
    g = VoiceGate(seed_db=-16.0)
    MUSIC.playing = True
    assert g.far_db() == -21.0
    # King's run: lyrics at -24 dB passed the old 10 dB gap; now they are far
    assert g.judge(tone(-24)).far is True and g.judge(tone(-17)).far is False


def test_music_leak_close_to_voice_keeps_a_3_db_margin():
    g = VoiceGate(calibration={"user_db": -16.0, "music_db": -17.0})
    MUSIC.playing = True
    assert g.far_db() == -19.0


def test_calibration_summary_and_threshold_text():
    from assistant.calibrate import summarize, threshold_text

    noise = tone(-60, 3, seed=1)
    voices = [tone(-18, 4, f=180, seed=s) for s in (2, 3, 4)]
    cal = summarize(noise, voices, tone(-30, 8, f=440, seed=5), RATE)
    assert -19.5 < cal["user_db"] < -16.5 and cal["noise_db"] < -50 and -31.5 < cal["music_db"] < -28.5
    assert len(cal["print"]) == 24 and "music_similarity" in cal
    json.dumps(cal)
    g = VoiceGate(calibration=cal)
    assert g._prints >= g.enroll_turns  # voice print enrolled from the first turn
    assert "while music plays" in threshold_text(cal)
    with pytest.raises(ValueError):
        summarize(noise, [tone(-58, 4)], None, RATE)  # nothing above the noise


def test_load_calibration(tmp_path):
    from assistant.calibrate import load_calibration

    p = tmp_path / "cal.json"
    assert load_calibration(str(p)) is None
    p.write_text(json.dumps({"user_db": -18.0}))
    assert load_calibration(str(p))["user_db"] == -18.0


def test_music_state_ducks_on_near_voice_and_restores_when_idle():
    calls = []

    async def duck(on, level):
        calls.append((on, level))

    async def scenario():
        m = MusicState()
        m.attach(duck, level=15, restore_secs=0.3)
        m.near_voice()  # nothing playing: no duck
        await asyncio.sleep(0.01)
        assert calls == []
        m.update_media({"state": "playing"})
        m.on_state("listening")
        m.near_voice()
        await asyncio.sleep(0.01)
        assert calls == [(True, 15)] and m.ducked
        m.start()
        m.on_state("thinking")
        await asyncio.sleep(0.5)
        assert calls == [(True, 15)]  # not idle: stays ducked
        m.on_state("idle")
        await asyncio.sleep(0.6)
        assert calls[-1] == (False, 15) and not m.ducked
        m.on_state("speaking")  # she speaks over music: duck again
        await asyncio.sleep(0.01)
        assert calls[-1] == (True, 15)
        m.update_media({"state": "paused"})
        assert not m.playing and not m.ducked
        m._task.cancel()

    asyncio.run(scenario())


def test_spotify_duck_restores_and_louder_steps_from_real_volume(tmp_path, monkeypatch):
    from mcp_servers.tools.devices import spotify as sp
    from tests.test_real_devices import FakeSpotify

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"refresh_token": "RT"}))
    fake = FakeSpotify()
    p = sp.SpotifyPlayer("cid", device="Donna", token_file=tok, http=fake, start_poller=False)
    p.play("coldplay")
    p.set_volume(60)
    st = p.duck(True, 15)
    assert fake.player["device"]["volume_percent"] == 15 and st["ducked"] is True and st["volume"] == 60
    assert p.volume_step(10)["volume"] == 70 and fake.player["device"]["volume_percent"] == 15  # "louder" while ducked: stays ducked
    assert p.duck(False)["ducked"] is False and fake.player["device"]["volume_percent"] == 70  # comes back at the new level
    p.duck(True, 15)
    p.duck(False)
    assert fake.player["device"]["volume_percent"] == 70
