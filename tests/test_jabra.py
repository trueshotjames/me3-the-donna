from mcp_servers.tools.devices.jabra import parse_report


def test_parse_hook_and_mute():
    assert parse_report(bytes.fromhex("030100")) == {"hook": True, "mute": False}  # green: off hook
    assert parse_report(bytes.fromhex("030000")) == {"hook": False, "mute": False}  # red: on hook
    assert parse_report([0x03, 0x11]) == {"hook": True, "mute": True}
    assert parse_report(b"\x01\x01") is None  # another report id
    assert parse_report(b"\x03") is None


class _Dev:
    def __init__(self, fail=0):
        self.writes, self.fail = [], fail

    def open_path(self, path):
        pass

    def set_nonblocking(self, on):
        pass

    def close(self):
        pass

    def write(self, data):
        self.writes.append(list(data))

    def read(self, n):
        if self.fail:
            self.fail -= 1
            raise OSError("read error")
        return []


def test_leds_write_the_whole_report_and_a_lost_jabra_comes_back(monkeypatch):
    import time

    from mcp_servers.tools.devices import jabra

    devs = [_Dev(fail=1), _Dev()]
    fake = type("hid", (), {"device": lambda: devs.pop(0) if len(devs) > 1 else devs[0]})
    monkeypatch.setattr(jabra, "_hid", lambda: fake)
    monkeypatch.setattr(jabra, "RETRY_SECS", 0.01)
    first = devs[0]
    b = jabra.JabraButtons(lambda hook: None, path=b"x")
    b.set_leds(off_hook=True)
    assert [3, 1, 0] in first.writes  # a two-byte write reset the Jabra on the Pi
    for _ in range(100):
        if devs[0].writes:
            break
        time.sleep(0.01)
    b.close()
    assert devs[0].writes[0] == [3, 1, 0]  # the lights are put back on the reopened device
