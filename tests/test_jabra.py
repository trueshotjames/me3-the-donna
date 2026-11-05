from mcp_servers.tools.devices.jabra import parse_report


def test_parse_hook_and_mute():
    assert parse_report(bytes.fromhex("030100")) == {"hook": True, "mute": False}  # green: off hook
    assert parse_report(bytes.fromhex("030000")) == {"hook": False, "mute": False}  # red: on hook
    assert parse_report([0x03, 0x11]) == {"hook": True, "mute": True}
    assert parse_report(b"\x01\x01") is None  # another report id
    assert parse_report(b"\x03") is None
