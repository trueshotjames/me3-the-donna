"""Donna's Spotify browser profile gets a Widevine pointer on Linux (Pi), and an existing one is kept."""

import json
import sys

from mcp_servers.tools.devices import webplayer as wp


def _player(tmp_path):
    p = wp.WebPlayer.__new__(wp.WebPlayer)
    p.profile_dir = tmp_path / "profile"
    return p


def test_pointer_written_on_linux(tmp_path, monkeypatch):
    cdm = tmp_path / "WidevineCdm"
    cdm.mkdir()
    (cdm / "manifest.json").write_text("{}")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WIDEVINE_CDM_DIR", str(cdm))
    pointer = _player(tmp_path).ensure_widevine()
    assert json.loads(pointer.read_text()) == {"Path": str(cdm)}


def test_existing_pointer_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    p = _player(tmp_path)
    pointer = p.profile_dir / "WidevineCdm" / "latest-component-updated-widevine-cdm"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"Path":"/somewhere/else"}')
    assert p.ensure_widevine() == pointer
    assert "somewhere" in pointer.read_text()


def test_windows_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert _player(tmp_path).ensure_widevine() is None
