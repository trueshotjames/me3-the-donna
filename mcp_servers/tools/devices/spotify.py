"""MEDIA_BACKEND=spotify: real music through the Spotify Web API (Spotify Connect; needs Premium).

Spotify does not play audio here: it tells a Spotify Connect device what to play. That device is
the Spotify desktop app on the laptop, or raspotify/librespot on the Pi (a ~20 MB Connect
receiver that plays through the Pi's sound card). SPOTIFY_DEVICE names the device to use when
nothing is playing yet ("Donna" for librespot, or the laptop's name); empty = any active device.

Secrets stay out of the code and the repo:
    SPOTIFY_CLIENT_ID      in .env (your app at developer.spotify.com; PKCE login, no client secret)
    data/spotify_token.json  written once by `python -m mcp_servers.tools.devices.spotify_login`
The token file holds a refresh token; never commit, log or share it.

Light on the Pi: one HTTPS GET of the player state every SPOTIFY_POLL_SECS while playing (30 s
when idle) on one small background thread; commands are a single request each. The tools see the
same status shape as the simulated player, so the music widget and the prompt do not change.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from mcp_servers.tools.devices.base import DeviceError, MediaPlayer

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing user-library-read playlist-read-private"
          " streaming user-read-email user-read-private")  # v9: streaming = the Web Playback SDK speaker (devices/webplayer.py)
ART = ["#1db954", "#0b3d1f"]  # the music widget paints a gradient; Spotify green
GENERIC = ("", "something", "anything", "music", "a song", "songs", "tunes", "a playlist", "playlist")  # "play music"
FAVORITES = ("favorites", "my favorites", "my music", "liked songs", "my liked songs", "my songs")


def default_token_file() -> Path:
    from mcp_servers.common import DATA_DIR

    return Path(os.getenv("SPOTIFY_TOKEN_FILE") or (DATA_DIR / "spotify_token.json"))


def save_token(path: Path, tok: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # owner only (no effect on Windows)
    except OSError:
        pass


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    for prefix in ("some ", "me some ", "me ", "a ", "the ", "my "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    for suffix in (" please", " on spotify", " music", " songs", " song", " tracks"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.strip()


class SpotifyPlayer(MediaPlayer):
    backend = "spotify"

    def __init__(self, client_id: str | None = None, *, device: str | None = None, token_file: Path | None = None,
                 poll_secs: float | None = None, http: Any = None, start_poller: bool = True, speaker: Any = None,
                 **_ignored: Any):
        self._speaker = speaker  # v9: Donna's own Connect speaker (devices/speaker.py), or None
        self._client_id = client_id if client_id is not None else os.getenv("SPOTIFY_CLIENT_ID", "")
        if not self._client_id:
            raise DeviceError("SPOTIFY_CLIENT_ID is not set in .env")
        self._token_file = token_file or default_token_file()
        if not self._token_file.is_file():
            raise DeviceError(f"no Spotify login yet ({self._token_file.name} missing); run: python -m mcp_servers.tools.devices.spotify_login")
        try:
            self._tok: dict[str, Any] = json.loads(self._token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DeviceError(f"cannot read {self._token_file.name}: {e}") from e
        if not self._tok.get("refresh_token"):
            raise DeviceError("the Spotify token file has no refresh token; run the login again")
        if http is None:
            import httpx

            http = httpx.Client(timeout=6.0)
        self._http = http
        self._device_name = (device if device is not None else os.getenv("SPOTIFY_DEVICE", "")).strip()
        self._poll = float(poll_secs if poll_secs is not None else os.getenv("SPOTIFY_POLL_SECS", "5") or 5)
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {}  # last /me/player answer, mapped to the widget shape
        self._sig: tuple = ()
        self._changed = False
        self._source = ""
        self._muted_from: int | None = None  # volume before "mute" (Spotify has no mute)
        self._duck_from: int | None = None  # volume before ducking for speech (assistant/music.py)
        self._playlists: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._stop = threading.Event()
        self._state = self._map(None)
        try:
            self._refresh_state()
        except DeviceError as e:  # offline at start-up: keep the backend, say so when a command fails
            logger.warning(f"spotify: {e}")
        self._thread = None
        if start_poller:
            self._thread = threading.Thread(target=self._poller, name="spotify-poll", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ HTTP
    def _access_token(self) -> str:
        with self._lock:
            if self._tok.get("access_token") and self._tok.get("expires_at", 0) - 60 > time.time():
                return self._tok["access_token"]
            r = self._http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self._tok["refresh_token"], "client_id": self._client_id})
            if r.status_code != 200:
                raise DeviceError("Spotify refused the saved login; run the Spotify login again")
            body = r.json()
            self._tok["access_token"] = body["access_token"]
            self._tok["expires_at"] = time.time() + int(body.get("expires_in", 3600))
            if body.get("refresh_token"):  # PKCE rotates refresh tokens
                self._tok["refresh_token"] = body["refresh_token"]
            save_token(self._token_file, self._tok)
            return self._tok["access_token"]

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None, retry: bool = True) -> Any:
        try:
            r = self._http.request(method, API + path, params=params, json=body, headers={"Authorization": f"Bearer {self._access_token()}"})
        except DeviceError:
            raise
        except Exception as e:  # noqa: BLE001 - network down, DNS, timeout
            raise DeviceError("I can't reach Spotify right now") from e
        if r.status_code == 401 and retry:
            with self._lock:
                self._tok["expires_at"] = 0
            return self._call(method, path, params=params, body=body, retry=False)
        if r.status_code == 429:
            raise DeviceError("Spotify is rate limiting us; try again in a few seconds")
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code >= 400:
            reason, msg = "", ""
            try:
                err = r.json().get("error", {})
                reason, msg = err.get("reason", ""), err.get("message", "")
            except Exception:  # noqa: BLE001
                pass
            if reason == "NO_ACTIVE_DEVICE" or r.status_code == 404:
                raise _NoDevice(msg or "no active device")
            if reason == "PREMIUM_REQUIRED":
                raise DeviceError("Spotify says this needs Premium on the logged-in account")
            if "VOLUME" in reason:
                raise DeviceError("this Spotify device does not allow volume control")
            raise DeviceError(f"Spotify refused that ({msg or r.status_code})")
        try:
            return r.json()
        except ValueError:  # player commands may answer 200 with a non-JSON body (seen live on pause)
            return None

    # ----------------------------------------------------------- devices
    def _devices(self) -> list[dict[str, Any]]:
        return (self._call("GET", "/me/player/devices") or {}).get("devices", [])

    def _target_device(self) -> str:
        devs = self._devices()
        if self._device_name:
            for d in devs:
                if d.get("name", "").lower() == self._device_name.lower():
                    return d["id"]
            if self._speaker is not None:  # never fall back to a phone when Donna is meant to be the speaker
                raise DeviceError(f"my {self._device_name} speaker is not online in Spotify yet; it may still be "
                                  "starting, or it needs its one-time login")
        if not devs:
            where = f" (\"{self._device_name}\")" if self._device_name else ""
            raise DeviceError(f"no Spotify device is online{where}; open Spotify on a phone or laptop")
        for d in devs:
            if d.get("is_active"):
                return d["id"]
        return devs[0]["id"]

    def _player(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> None:
        """A player command; with no active device, pick SPOTIFY_DEVICE (or the first one) and retry there."""
        try:
            self._call(method, path, params=params, body=body)
        except _NoDevice:
            params = dict(params or {}, device_id=self._target_device())
            try:
                self._call(method, path, params=params, body=body)
            except _NoDevice as e:
                raise DeviceError("Spotify could not start on that device; open Spotify there and try again") from e

    # ------------------------------------------------------------- state
    def _map(self, p: dict[str, Any] | None) -> dict[str, Any]:
        if not p or not p.get("item"):
            dev = (p or {}).get("device") or {}
            return {"state": "stopped", "track": None, "position_s": 0.0, "started_at": None,
                    "volume": dev.get("volume_percent") if dev.get("volume_percent") is not None else self._state.get("volume", 50),
                    "shuffle": bool((p or {}).get("shuffle_state")), "repeat": {"track": "one", "context": "all"}.get((p or {}).get("repeat_state"), "off"),
                    "device": dev.get("name"), "id": None}
        it = p["item"]
        dev = p.get("device") or {}
        pos = (p.get("progress_ms") or 0) / 1000
        playing = bool(p.get("is_playing"))
        artists = ", ".join(a.get("name", "") for a in it.get("artists", []) if a.get("name")) or (it.get("show") or {}).get("name", "")
        images = (it.get("album") or {}).get("images") or it.get("images") or []
        return {
            "state": "playing" if playing else "paused",
            "track": {"title": it.get("name", ""), "artist": artists, "album": (it.get("album") or {}).get("name", ""), "genre": "",
                      "duration_s": round((it.get("duration_ms") or 0) / 1000), "art": ART, "image": images[0]["url"] if images else None},
            "position_s": round(pos, 1),
            "started_at": time.time() - pos if playing else None,
            "volume": dev.get("volume_percent") if dev.get("volume_percent") is not None else self._state.get("volume", 50),
            "shuffle": bool(p.get("shuffle_state")),
            "repeat": {"track": "one", "context": "all"}.get(p.get("repeat_state"), "off"),
            "device": dev.get("name"),
            "id": it.get("id") or it.get("uri"),
        }

    def _refresh_state(self) -> dict[str, Any]:
        try:
            st = self._map(self._call("GET", "/me/player", params={"additional_types": "track,episode"}))
        except _NoDevice:
            st = self._map(None)
        with self._lock:
            sig = (st["state"], st["id"], st["volume"], st["shuffle"], st["repeat"], st["device"])
            if sig != self._sig:
                self._sig, self._changed = sig, True
            self._state = st
        return st

    def _after(self, delay: float = 0.35) -> dict[str, Any]:
        """Spotify applies a command asynchronously; read the state back shortly after."""
        time.sleep(delay)
        try:
            self._refresh_state()
        except DeviceError:
            pass
        return self.status()

    def _poller(self) -> None:
        while not self._stop.is_set():
            wait = self._poll if self._state.get("state") == "playing" else max(self._poll, 30.0)
            if self._stop.wait(wait):
                return
            try:
                self._refresh_state()
            except Exception as e:  # noqa: BLE001 - keep polling through a network blip
                logger.debug(f"spotify poll: {e!r}")

    def tick(self) -> bool:
        with self._lock:
            changed, self._changed = self._changed, False
        return changed

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._state)
        st.pop("id", None)
        if st.get("state") == "playing" and st.get("started_at"):
            st["position_s"] = round(time.time() - st["started_at"], 1)
        if self._duck_from is not None:  # report the volume the user chose, not the ducked one
            st["volume"] = self._duck_from
        return {"backend": self.backend, **st, "muted": self._muted_from is not None, "ducked": self._duck_from is not None, "source": self._source,
                "queue_length": 0, "queue_index": 0, "up_next": []}

    # --------------------------------------------------------- searching
    def _my_playlists(self) -> list[dict[str, Any]]:
        t, items = self._playlists
        if time.time() - t < 600:
            return items
        try:
            items = [p for p in (self._call("GET", "/me/playlists", params={"limit": 50}) or {}).get("items", []) if p]
        except DeviceError:
            items = []
        self._playlists = (time.time(), items)
        return items

    def _resolve(self, query: str) -> tuple[dict[str, Any], str]:
        """A spoken request -> (body for PUT /me/player/play, what to say we are playing)."""
        raw = (query or "").strip()
        q = _norm(raw)
        want_playlist = "playlist" in q
        q = q.replace(" playlist", "").replace("playlist ", "").strip() if want_playlist else q

        if q in FAVORITES:
            saved = (self._call("GET", "/me/tracks", params={"limit": 50}) or {}).get("items", [])
            uris = [s["track"]["uri"] for s in saved if s and s.get("track")]
            if not uris:
                raise DeviceError("your Liked Songs are empty; tell me an artist, song or playlist")
            return {"uris": uris}, "your Liked Songs"

        for p in self._my_playlists():  # "play my workout playlist"
            name = (p.get("name") or "").lower()
            if name and (name == q or (want_playlist and (q in name or name in q))):
                return {"context_uri": p["uri"]}, f"your {p['name']} playlist"

        if " by " in q:  # "Yellow by Coldplay"
            title, artist = q.split(" by ", 1)
            res = self._call("GET", "/search", params={"q": f"track:{title} artist:{artist}", "type": "track", "limit": 5}) or {}
            tracks = [t for t in res.get("tracks", {}).get("items", []) if t]
            if tracks:
                return self._track_body(tracks[0])

        res = self._call("GET", "/search", params={"q": q, "type": "artist,track,playlist", "limit": 5}) or {}
        artists = [a for a in res.get("artists", {}).get("items", []) if a]
        tracks = [t for t in res.get("tracks", {}).get("items", []) if t]
        playlists = [p for p in res.get("playlists", {}).get("items", []) if p]

        if want_playlist and playlists:
            return {"context_uri": playlists[0]["uri"]}, f"the {playlists[0]['name']} playlist"
        for a in artists:
            if a.get("name", "").lower() == q:
                return {"context_uri": a["uri"]}, a["name"]
        for t in tracks:
            if t.get("name", "").lower() == q:
                return self._track_body(t)
        if playlists and not tracks:
            return {"context_uri": playlists[0]["uri"]}, f"the {playlists[0]['name']} playlist"
        if playlists and (len(q.split()) <= 2):  # a mood or genre ("jazz", "lo-fi", "chill") -> a playlist
            return {"context_uri": playlists[0]["uri"]}, f"the {playlists[0]['name']} playlist"
        if tracks:
            return self._track_body(tracks[0])
        if artists:
            return {"context_uri": artists[0]["uri"]}, artists[0]["name"]
        raise DeviceError(f"I couldn't find {raw!r} on Spotify")

    def _default(self) -> tuple[dict[str, Any], str]:
        """SPOTIFY_DEFAULT_PLAYLIST (a name of one of your playlists, a playlist link or URI); unset = your Liked Songs."""
        want = (os.getenv("SPOTIFY_DEFAULT_PLAYLIST") or "").strip()
        if not want:
            return self._resolve("liked songs")
        if "open.spotify.com/playlist/" in want:
            want = "spotify:playlist:" + want.split("/playlist/", 1)[1].split("?")[0]
        if want.startswith("spotify:"):
            return {"context_uri": want}, "your default playlist"
        low = want.lower()
        for p in self._my_playlists():
            if (p.get("name") or "").lower() == low:
                return {"context_uri": p["uri"]}, f"your {p['name']} playlist"
        return self._resolve(want + " playlist")

    @staticmethod
    def _track_body(t: dict[str, Any]) -> tuple[dict[str, Any], str]:
        who = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
        album = (t.get("album") or {}).get("uri")
        label = f"{t['name']} by {who}" if who else t["name"]
        if album:  # play it inside its album so the music carries on afterwards
            return {"context_uri": album, "offset": {"uri": t["uri"]}}, label
        return {"uris": [t["uri"]]}, label

    # ---------------------------------------------------------- commands
    def play(self, query: str = "") -> dict[str, Any]:
        if _norm(query) in GENERIC:  # "play music" with nothing named
            if self._state.get("state") == "playing":
                return dict(self.status(), note="music is already playing; nothing changed")
            if self._state.get("state") == "paused" and self._state.get("track"):
                return self.resume()
            body, label = self._default()
        else:
            body, label = self._resolve(query)
        if self._device_name:  # v9: always play on Donna's speaker (moves the music over from a phone)
            self._player("PUT", "/me/player/play", body=body, params={"device_id": self._target_device()})
        else:
            self._player("PUT", "/me/player/play", body=body)
        self._source = label
        return self._after(0.6)

    def _on_speaker(self) -> bool:
        return self._speaker is not None and (self._state.get("device") or "").lower() == self._speaker.name.lower()

    def pause(self) -> dict[str, Any]:
        if self._state.get("state") == "playing":
            if not (self._on_speaker() and self._speaker.playing() and self._speaker.command("pause")):
                self._player("PUT", "/me/player/pause")
            with self._lock:  # Spotify reports the change a moment later; say what we did now
                self._state.update(state="paused", started_at=None)
        return self._after(0.6)

    def resume(self) -> dict[str, Any]:
        if self._state.get("state") == "stopped" and not self._state.get("track"):
            return self.play("")
        self._player("PUT", "/me/player/play")  # the cloud knows what to resume; a fresh speaker does not
        return self._after(0.6)

    def stop(self) -> dict[str, Any]:
        return self.pause()  # Spotify has pause only

    def next(self) -> dict[str, Any]:
        self._player("POST", "/me/player/next")
        return self._after(0.6)

    def previous(self) -> dict[str, Any]:
        self._player("POST", "/me/player/previous")
        return self._after(0.6)

    def duck(self, on: bool, level: int = 15) -> dict[str, Any]:
        """Music-aware listening (assistant/music.py): lower to `level` % while someone talks, then restore."""
        if on and self._duck_from is None and self._state.get("state") == "playing":
            before = int(self._state.get("volume") or 50)
            if before > level:
                self._volume_now(int(level))
                self._duck_from = before
                with self._lock:
                    self._state["volume"] = int(level)
        elif not on and self._duck_from is not None:
            before, self._duck_from = self._duck_from, None
            try:
                self._volume_now(before)
            except DeviceError:
                pass  # nothing playing any more; the next volume command sets it
            with self._lock:
                self._state["volume"] = before
        return self.status()

    def set_volume(self, level: int) -> dict[str, Any]:
        level = max(0, min(100, int(level)))
        self._muted_from = None
        if self._duck_from is not None:
            # v9 (King's run): "lower the volume" is always said while the music is ducked for his voice. Setting it
            # at once jumped the music from the ducked 15 % UP to the new level while she answered, so it sounded
            # louder. Now the new level is where the music comes back to once the conversation is idle.
            self._duck_from = level
            return self.status()
        self._player("PUT", "/me/player/volume", params={"volume_percent": level})
        with self._lock:
            self._state["volume"] = level
        return self._after(0.2)

    def volume_step(self, delta: int) -> dict[str, Any]:
        base = self._duck_from if self._duck_from is not None else int(self._state.get("volume") or 50)
        return self.set_volume(base + int(delta))  # "louder" is said while the music is ducked: step from the real volume

    def set_mute(self, muted: bool) -> dict[str, Any]:
        if muted and self._muted_from is None:
            before = self._duck_from if self._duck_from is not None else int(self._state.get("volume") or 50)
            self._duck_from = None  # "mute" is said while ducked: unmute goes back to the real volume
            self._player("PUT", "/me/player/volume", params={"volume_percent": 0})
            self._muted_from = before
        elif not muted and self._muted_from is not None:
            before, self._muted_from = self._muted_from, None
            self._player("PUT", "/me/player/volume", params={"volume_percent": before})
        return self._after(0.2)

    def set_shuffle(self, on: bool) -> dict[str, Any]:
        self._player("PUT", "/me/player/shuffle", params={"state": "true" if on else "false"})
        return self._after(0.2)

    def set_repeat(self, mode: str) -> dict[str, Any]:
        m = (mode or "off").strip().lower()
        m = {"track": "one", "song": "one", "this": "one", "on": "all", "queue": "all", "playlist": "all"}.get(m, m)
        if m not in ("off", "one", "all"):
            raise DeviceError("repeat can be off, one or all")
        self._player("PUT", "/me/player/repeat", params={"state": {"one": "track", "all": "context"}.get(m, "off")})
        return self._after(0.2)

    def _volume_now(self, level: int) -> None:
        """Ducking: through the local speaker when the music plays on it (a few ms), else the Web API."""
        if self._on_speaker():
            try:
                self._speaker.set_volume(level)
                return
            except DeviceError:
                pass
        self._player("PUT", "/me/player/volume", params={"volume_percent": int(level)})

    def close(self) -> None:
        self._stop.set()
        if self._speaker is not None:
            self._speaker.stop()


class _NoDevice(DeviceError):
    pass
