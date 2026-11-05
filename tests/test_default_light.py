"""'Lights on' with no room named goes to the default light (LIGHT_DEFAULT_ROOM, else the first room)."""

from assistant.fast_confirm import _light
from mcp_servers.tools.devices.sim import SimLights


def test_bare_lights_go_to_default_room():
    lights = SimLights(["living room", "bedroom", "kitchen"], default_room="")
    for room in ("", "lights", "the lights", None):
        out = lights.set(room, on=True)
        assert [l["room"] for l in out["lights"]] == ["living room"]
    assert [l["room"] for l in lights.set("all the lights", on=False)["lights"]] == ["living room", "bedroom", "kitchen"]
    assert [l["room"] for l in lights.set("kitchen", on=True)["lights"]] == ["kitchen"]


def test_default_room_from_env(monkeypatch):
    monkeypatch.setenv("LIGHT_DEFAULT_ROOM", "bedroom")
    assert SimLights(["living room", "bedroom"]).set("", on=True)["lights"][0]["room"] == "bedroom"
    monkeypatch.setenv("LIGHT_DEFAULT_ROOM", "garage")  # unknown: fall back to the first room
    assert SimLights(["living room", "bedroom"]).set("", on=True)["lights"][0]["room"] == "living room"


def test_confirmation_names_the_room():
    r = {"lights": [{"room": "living room", "on": False}]}
    assert _light({"on": False}, r) == "Living room light off."
    assert _light({"room": "all", "on": False}, {"lights": [{"room": "a"}, {"room": "b"}]}) == "All lights off."
