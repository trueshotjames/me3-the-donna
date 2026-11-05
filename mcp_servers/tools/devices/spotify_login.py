"""One-time Spotify login for MEDIA_BACKEND=spotify (Authorization Code with PKCE: no client secret).

    python -m mcp_servers.tools.devices.spotify_login              # laptop: opens the browser
    python -m mcp_servers.tools.devices.spotify_login --no-browser # Pi without a screen: paste the URL back

Needs SPOTIFY_CLIENT_ID in .env, and SPOTIFY_REDIRECT_URI (default http://127.0.0.1:8888/callback)
added exactly as written to your app's Redirect URIs at developer.spotify.com.
Writes data/spotify_token.json (owner-only). Nothing secret is printed.
Instead of logging in on the Pi, you can also copy that file from the laptop to the Pi's data/ folder.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from mcp_servers.common import env, setup
from mcp_servers.tools.devices.spotify import SCOPES, TOKEN_URL, default_token_file, save_token

AUTH_URL = "https://accounts.spotify.com/authorize"


def main() -> int:
    setup("spotify-login")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-browser", action="store_true", help="print the login link and paste the address you land on")
    args = ap.parse_args()

    client_id = env("SPOTIFY_CLIENT_ID")
    if not client_id:
        print("Set SPOTIFY_CLIENT_ID in .env first (developer.spotify.com > your app > Settings).")
        return 1
    redirect = env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })

    if args.no_browser:
        print("1. Open this link on any device and log in to Spotify:\n\n" + url + "\n")
        print("2. The browser then fails to load a 127.0.0.1 page. That is expected.")
        landed = input("3. Copy the full address from the address bar and paste it here: ").strip()
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(landed).query)
    else:
        parsed = urllib.parse.urlparse(redirect)
        got: dict[str, list[str]] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                if u.path != parsed.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                got.update(urllib.parse.parse_qs(u.query))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Spotify is connected. You can close this tab.</h2>")

            def log_message(self, *a):  # keep the console quiet
                pass

        srv = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 8888), Handler)
        srv.timeout = 1
        print("Opening Spotify in your browser. If nothing opens, visit:\n\n" + url + "\n")
        webbrowser.open(url)
        deadline = time.time() + 300
        while not got and time.time() < deadline:
            srv.handle_request()
        srv.server_close()
        qs = got

    if qs.get("state", [""])[0] != state:
        print("The login answer did not match this attempt (state mismatch). Run the login again.")
        return 1
    if "error" in qs or "code" not in qs:
        print(f"Spotify login was not completed: {qs.get('error', ['no code'])[0]}")
        return 1
    r = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": redirect,
                                    "client_id": client_id, "code_verifier": verifier}, timeout=10)
    if r.status_code != 200:
        print(f"Spotify did not accept the login (HTTP {r.status_code}). Check the Redirect URI in your app settings.")
        return 1
    body = r.json()
    tok = {"access_token": body["access_token"], "refresh_token": body["refresh_token"],
           "expires_at": time.time() + int(body.get("expires_in", 3600)), "scope": body.get("scope", "")}
    path = default_token_file()
    save_token(path, tok)
    me = httpx.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10)
    product = me.json().get("product", "?") if me.status_code == 200 else "?"
    print(f"Saved the Spotify login to {path} (account type: {product}). Set MEDIA_BACKEND=spotify in .env and restart.")
    if product not in ("premium", "?"):
        print("Note: playback control needs Spotify Premium on this account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
