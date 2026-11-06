"""BENCHMARK_MODE: fd-benchmark runs must never reach tools, Hermes, her memory or the speakerphone."""

from __future__ import annotations

import asyncio

import pytest


def _cfg(monkeypatch, on=True):
    from assistant.config import Config

    monkeypatch.setenv("BENCHMARK_MODE", "1" if on else "0")
    for name in ("HERMES_ENABLED", "MEMORY_ENABLED", "CUES_ENABLED", "MUSIC_DUCK", "SPEAKING_RATE"):
        monkeypatch.setenv(name, "1")
    return Config()


def test_mode_switches_off_everything_that_acts(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert cfg.benchmark_mode
    assert not (cfg.hermes_enabled or cfg.memory_enabled or cfg.cues_enabled or cfg.music_duck or cfg.speaking_rate)


def test_normal_mode_keeps_them(monkeypatch):
    cfg = _cfg(monkeypatch, on=False)
    assert not cfg.benchmark_mode
    assert cfg.hermes_enabled and cfg.memory_enabled and cfg.cues_enabled and cfg.music_duck and cfg.speaking_rate


def test_no_hermes_key_needed(monkeypatch):
    monkeypatch.setenv("HERMES_API_KEY", "")
    assert not any("HERMES" in p for p in _cfg(monkeypatch).validate())


def test_refuses_to_open_the_speakerphone(monkeypatch):
    from assistant import main

    def no_devices(cfg):
        raise AssertionError("opened an audio device")

    async def no_tools_server(cfg):
        raise AssertionError("started the tools server")

    monkeypatch.setattr(main, "make_transport", no_devices)
    monkeypatch.setattr(main, "connect_tools_server", no_tools_server)
    with pytest.raises(RuntimeError, match="speakerphone"):
        asyncio.run(main.Assistant(_cfg(monkeypatch)).build())


def test_null_bridge_never_reaches_a_tool():
    from assistant.main import NullToolsBridge

    bridge = NullToolsBridge()
    assert not bridge.connected
    assert "error" in asyncio.run(bridge.call_json("send_message", {"text": "hi"}))


def test_prompt_says_no_tools():
    from assistant.prompts import system_prompt

    with_rule = system_prompt("Donna", "King", 8, no_tools=True)
    assert "no tools and no background tasks" in with_rule
    assert "no tools and no background tasks" not in system_prompt("Donna", "King", 8)
