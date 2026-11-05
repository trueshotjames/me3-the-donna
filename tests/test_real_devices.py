"""Spotify and Tapo backends against fakes: no network, no keys, no bulb needed."""

from __future__ import annotations

import json
import time

import pytest

from mcp_servers.tools.devices.base import DeviceError


# ------------------------------------------------------------------ Spotify


class Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        return self._body


class FakeSpotify:
    """Just enough of the Web API: one device, a search, a player."""

    def __init__(self, devices=None):
        self.calls: list[tuple] = []
        self.devices = devices if devices is not None else [{"id": "dev1", "name": "Donna", "is_active": False, "volume_percent": 40}]
        self.active = False
        self.player = {"is_playing": False, "progress_ms": 0, "shuffle_state": False, "repeat_state": "off",
                       "device": {"name": "Donna", "volume_percent": 40}, "item": None}

    def post(self, url, data=None):
        self.calls.append(("POST", url, data.get("grant_type")))
        return Resp(200, {"access_token": "AT", "expires_in": 3600, "refresh_token": "RT2"})

    def request(self, method, url, params=None, json=None, headers=None):
        path = url.split("/v1", 1)[1]
        self.calls.append((method, path, params, json))
        if path == "/me/player" and method == "GET":
            return Resp(200, self.player) if self.active else Resp(204)
        if path == "/me/player/devices":
            return Resp(200, {"devices": self.devices})
        if path == "/me/playlists":
            return Resp(200, {"items": [{"name": "Workout", "uri": "spotify:playlist:mine"}]})
        if path == "/search":
            return Resp(200, {"artists": {"items": [{"name": "Coldplay", "uri": "spotify:artist:cp"}]},
                              "tracks": {"items": [{"name": "Yellow", "uri": "spotify:track:y", "artists": [{"name": "Coldplay"}], "album": {"uri": "spotify:album:p"}}]},
                              "playlists": {"items": [None, {"name": "Jazz Vibes", "uri": "spotify:playlist:jazz"}]}})
        if path == "/me/player/play":
            if not self.active and not (params or {}).get("device_id"):
                return Resp(404, {"error": {"status": 404, "message": "No active device", "reason": "NO_ACTIVE_DEVICE"}})
            self.active = True
            self.player.update(is_playing=True, item={"id": "y", "name": "Yellow", "duration_ms": 266000, "artists": [{"name": "Coldplay"}],
                                                      "album": {"name": "Parachutes", "images": [{"url": "https://i.scdn.co/x"}]}})
            return Resp(204)
        if path == "/me/player/pause":
            self.player["is_playing"] = False
            return Resp(204)
        if path == "/me/player/volume":
            self.player["device"]["volume_percent"] = int(params["volume_percent"])
            return Resp(204)
        return Resp(204)


@pytest.fixture
def spotify(tmp_path, monkeypatch):
    from mcp_servers.tools.devices import spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    tok = tmp_path / "spotify_token.json"
    tok.write_text(json.dumps({"refresh_token": "RT"}))
    fake = FakeSpotify()
    player = sp.SpotifyPlayer("cid", device="Donna", token_file=tok, http=fake, start_poller=False)
    return player, fake, tok


def test_spotify_needs_client_id_and_login(tmp_path):
    from mcp_servers.tools.devices.spotify import SpotifyPlayer

    with pytest.raises(DeviceError, match="SPOTIFY_CLIENT_ID"):
        SpotifyPlayer("", token_file=tmp_path / "t.json", http=FakeSpotify(), start_poller=False)
    with pytest.raises(DeviceError, match="spotify_login"):
        SpotifyPlayer("cid", token_file=tmp_path / "t.json", http=FakeSpotify(), start_poller=False)


def test_spotify_play_picks_device_and_maps_status(spotify):
    player, fake, tok = spotify
    assert player.status()["state"] == "stopped"
    st = player.play("Yellow by Coldplay")
    plays = [c for c in fake.calls if c[1] == "/me/player/play"]
    assert plays[-1][2] == {"device_id": "dev1"}  # no active device: retried on SPOTIFY_DEVICE
    assert plays[-1][3] == {"context_uri": "spotify:album:p", "offset": {"uri": "spotify:track:y"}}
    assert st["state"] == "playing" and st["track"]["title"] == "Yellow" and st["track"]["artist"] == "Coldplay"
    assert st["source"] == "Yellow by Coldplay" and st["backend"] == "spotify" and len(st["track"]["art"]) == 2
    assert json.loads(tok.read_text())["refresh_token"] == "RT2"  # rotated token saved
    assert player.tick() is True and player.tick() is False


def test_spotify_resolves_requests(spotify):
    player, fake, _ = spotify
    assert player._resolve("my workout playlist") == ({"context_uri": "spotify:playlist:mine"}, "your Workout playlist")
    assert player._resolve("some coldplay")[0] == {"context_uri": "spotify:artist:cp"}
    assert player._resolve("jazz")[0] == {"context_uri": "spotify:playlist:jazz"}  # a genre -> a playlist (null items skipped)


def test_spotify_mute_restores_volume(spotify):
    player, fake, _ = spotify
    player.play("coldplay")
    assert player.set_volume(55)["volume"] == 55
    assert player.set_mute(True)["muted"] is True and fake.player["device"]["volume_percent"] == 0
    st = player.set_mute(False)
    assert st["muted"] is False and fake.player["device"]["volume_percent"] == 55


def test_spotify_no_device_is_a_spoken_error(tmp_path, monkeypatch):
    from mcp_servers.tools.devices import spotify as sp

    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"refresh_token": "RT"}))
    player = sp.SpotifyPlayer("cid", device="Donna", token_file=tok, http=FakeSpotify(devices=[]), start_poller=False)
    with pytest.raises(DeviceError, match="no Spotify device is online"):
        player.play("coldplay")


# --------------------------------------------------------------------- Tapo


def test_tapo_color_mapping():
    from mcp_servers.tools.devices.tapo import bulbs_from_env, from_bulb, to_bulb

    assert bulbs_from_env("Living Room:192.168.1.50, bedroom:10.0.0.7") == {"living room": "192.168.1.50", "bedroom": "10.0.0.7"}
    assert to_bulb({"on": False}) == {"light_on": False}
    assert to_bulb({"on": True, "brightness": 40, "color": "#ffd9a0", "color_name": "warm white"}) == {"light_on": True, "brightness": 40, "color_temp": 2700}
    blue = to_bulb({"on": True, "brightness": 80, "color": "#5a8dff", "color_name": "blue"})
    assert blue["color_temp"] == 0 and 215 <= blue["hue"] <= 230 and blue["saturation"] > 50
    assert from_bulb(True, 60, (0, 0, 60), 2700)["color_name"] == "warm white"
    assert from_bulb(True, 60, (blue["hue"], blue["saturation"], 60), 0)["color_name"] == "blue"


class FakeLight:
    def __init__(self):
        self.states = []
        self.fail = 0

    async def set_state(self, st):
        if self.fail:
            self.fail -= 1
            raise OSError("bulb offline")
        self.states.append(st)


def test_tapo_set_applies_and_reverts_on_failure(tmp_path):
    kasa = pytest.importorskip("kasa")
    from mcp_servers.tools.devices.tapo import TapoLights

    light = FakeLight()

    class Dev:
        modules = {kasa.Module.Light: light}

    connects = []

    async def connect(host):
        connects.append(host)
        return Dev()

    saved = {}
    t = TapoLights({"living room": "192.168.1.50"}, username="u", password="p", connect=connect, start_poller=False,
                   load=lambda n: None, persist=lambda n, s: saved.update(s))
    try:
        out = t.set("all", on=True, brightness=30, color="blue")
        assert out["lights"][0]["brightness"] == 30 and light.states[-1].brightness == 30 and light.states[-1].color_temp == 0
        assert t.status()["backend"] == "tapo" and t.rooms() == ["living room"]
        light.fail = 2  # both attempts fail: refused, state unchanged
        with pytest.raises(DeviceError, match="couldn't reach the living room light"):
            t.set("living room", on=False)
        assert t.status()["lights"][0]["on"] is True
        light.fail = 1  # first attempt fails, the reconnect works
        assert t.set("living room", on=False)["lights"][0]["on"] is False and len(connects) >= 2
    finally:
        t.close()


def test_tapo_requires_credentials():
    from mcp_servers.tools.devices.tapo import TapoLights

    with pytest.raises(DeviceError, match="TAPO_USERNAME"):
        TapoLights({"lamp": "1.2.3.4"}, username="", password="", connect=lambda h: None, start_poller=False)
    with pytest.raises(DeviceError, match="TAPO_BULBS"):
        TapoLights({}, username="u", password="p", start_poller=False)


# ------------------------------------------------------- Donna's own Spotify speaker (v9)


class FakeLocal:
    def __init__(self):
        self.posts = []

    def get(self, url):
        return Resp(200, {"username": "king"} if url.endswith("/status") else {})

    def post(self, url, json=None):
        self.posts.append((url, json))
        return Resp(200)


def test_speaker_config_and_local_volume(tmp_path):
    from mcp_servers.tools.devices.speaker import Speaker, config_yaml

    y = config_yaml("Donna", "alsa", "default", 3678)
    assert "device_name: Donna" in y and "type: device_auth" in y and "port: 3678" in y
    http = FakeLocal()
    sp = Speaker(config_dir=tmp_path, http=http, binary=tmp_path / "none.exe")
    assert sp.alive() and sp.logged_in()
    sp.start()  # already answering: reused, the missing binary does not matter
    sp.set_volume(140)
    assert http.posts[-1] == ("http://127.0.0.1:3678/player/volume", {"volume": 100})


def test_spotify_plays_on_donna_and_ducks_locally(tmp_path, monkeypatch):
    from mcp_servers.tools.devices import spotify as sp
    from mcp_servers.tools.devices.speaker import Speaker

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"refresh_token": "RT"}))
    fake = FakeSpotify(devices=[{"id": "phone", "name": "King's phone", "is_active": True},
                                {"id": "dev1", "name": "Donna", "is_active": False}])
    fake.active = True  # the phone is playing
    local = FakeLocal()
    speaker = Speaker(config_dir=tmp_path, http=local)
    p = sp.SpotifyPlayer("cid", device="Donna", token_file=tok, http=fake, start_poller=False, speaker=speaker)
    p.play("coldplay")
    plays = [c for c in fake.calls if c[1] == "/me/player/play"]
    assert plays[-1][2] == {"device_id": "dev1"}  # moved to Donna even though the phone was active
    p.duck(True, 15)
    assert local.posts[-1][1] == {"volume": 15}  # local API, not the cloud
    fake.devices = [{"id": "phone", "name": "King's phone", "is_active": True}]
    with pytest.raises(DeviceError, match="not online"):
        p.play("coldplay")  # never falls back to the phone


def test_webplayer_page_token_and_state(tmp_path):
    import httpx

    from mcp_servers.tools.devices.webplayer import WebPlayer

    wp = WebPlayer(lambda: "AT-123", port=18767, profile_dir=tmp_path, name="Donna")
    from http.server import ThreadingHTTPServer
    import threading

    wp._server = ThreadingHTTPServer(("127.0.0.1", wp.port), wp._handler())
    threading.Thread(target=wp._server.serve_forever, daemon=True).start()
    try:
        page = httpx.get("http://127.0.0.1:18767/").text
        assert 'name: "Donna"' in page and "sdk.scdn.co/spotify-player.js" in page
        assert httpx.get("http://127.0.0.1:18767/token").json() == {"access_token": "AT-123"}
        httpx.post("http://127.0.0.1:18767/state", json={"kind": "ready", "data": {"device_id": "x"}})
        assert wp.ready.is_set() and wp.state["kind"] == "ready"
        with pytest.raises(DeviceError):
            wp.set_volume(10)  # volume goes through the Web API for this speaker
    finally:
        wp.stop()


def test_spotify_play_music_default_and_already_playing(spotify, monkeypatch):
    player, fake, _ = spotify
    monkeypatch.setenv("SPOTIFY_DEFAULT_PLAYLIST", "Workout")
    st = player.play("music")  # nothing playing: the default playlist
    plays = [c for c in fake.calls if c[1] == "/me/player/play"]
    assert plays[-1][3] == {"context_uri": "spotify:playlist:mine"} and st["source"] == "your Workout playlist"
    n = len(fake.calls)
    st = player.play("some music")  # already playing: nothing changes
    assert "already playing" in st["note"] and not [c for c in fake.calls[n:] if c[1] == "/me/player/play"]
    monkeypatch.setenv("SPOTIFY_DEFAULT_PLAYLIST", "https://open.spotify.com/playlist/37i9dQZF1?si=x")
    assert player._default()[0] == {"context_uri": "spotify:playlist:37i9dQZF1"}
