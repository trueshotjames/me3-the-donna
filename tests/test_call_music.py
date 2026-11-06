"""Music pauses for calls and attempted calls, and comes back (mcp_servers/tools/devices/__init__.py)."""

import time

import pytest

from mcp_servers.tools.devices import Devices
from mcp_servers.tools.devices.base import DeviceError


class Media:
    name = "media"

    def __init__(self, state="playing"):
        self.state, self.log = state, []

    def tick(self):
        return False

    def status(self):
        return {"state": self.state}

    def pause(self):
        self.log.append("pause")
        self.state = "paused"

    def resume(self):
        self.log.append("resume")
        self.state = "playing"


class Phone:
    name = "phone"

    def __init__(self, fail=False):
        self.fail, self.c = fail, None

    def tick(self):
        return False

    def status(self):
        return {"call": self.c}

    def call(self, contact):
        if self.fail:
            raise DeviceError("no")
        self.c = {"state": "ringing", "started_at": 1.0}
        return {"call": self.c}


def _house(media, phone):
    return Devices(None, None, media, phone)


def _wait(cond):
    for _ in range(200):
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def _tick(d):
    d._sync_call_music()


def test_pauses_during_a_call_and_resumes_after():
    m, p = Media(), Phone()
    d = _house(m, p)
    v = d.version
    d.place_call("Love")
    assert m.state == "paused" and d.version > v  # the version bump tells the UI and Donna
    _tick(d)
    assert m.log == ["pause"]
    p.c = dict(p.c, state="connected")
    _tick(d)
    p.c = dict(p.c, state="ended")
    _tick(d)
    _wait(lambda: m.state == "playing")
    assert m.log == ["pause", "resume"]


def test_a_failed_attempt_brings_the_music_back():
    m = Media()
    d = _house(m, Phone(fail=True))
    with pytest.raises(DeviceError):
        d.place_call("Love")
    assert m.log == ["pause", "resume"] and m.state == "playing"


def test_music_that_was_not_playing_stays_off():
    m, p = Media("paused"), Phone()
    d = _house(m, p)
    d.place_call("Love")
    p.c = None
    d.release_music()
    assert m.log == [] and m.state == "paused"


def test_an_answered_call_pauses_through_tick():
    m, p = Media(), Phone()
    d = _house(m, p)
    p.c = {"state": "connected", "started_at": 2.0}
    _tick(d)
    _wait(lambda: m.state == "paused")


def test_the_users_choice_during_a_call_sticks():
    m, p = Media(), Phone()
    d = _house(m, p)
    d.place_call("Love")
    _tick(d)
    d.drop_music_hold()  # "stop the music": already paused, must not resume after the call
    p.c = None
    _tick(d)
    time.sleep(0.02)
    assert m.log == ["pause"]

    m2, p2 = Media(), Phone()
    d2 = _house(m2, p2)
    d2.place_call("Love")
    _tick(d2)
    d2.drop_music_hold()  # "play music" during the call: tick must not pause it again
    m2.state = "playing"
    _tick(d2)
    time.sleep(0.02)
    assert m2.state == "playing"


def test_tick_does_not_resume_while_dialing():
    m, p = Media(), Phone()
    d = _house(m, p)
    d._dialing = True
    d.hold_music()
    _tick(d)
    time.sleep(0.02)
    assert m.log == ["pause"]
