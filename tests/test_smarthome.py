"""
Tests for the smart lamps (smarthome.py) and their automations (home_auto.py) with a fake lamp: nothing on the
network is touched.

Run:  PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_smarthome.py   (also works with pytest)
"""
import datetime
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import events        # noqa: E402
import smarthome     # noqa: E402
import home_auto     # noqa: E402
import presence      # noqa: E402

LAMP = {"name": "lampu", "aliases": ["lampu kamar"], "id": "dev123", "ip": "192.168.1.5", "version": 3.4}
WHITE = {"20": True, "21": "white", "22": 1000, "23": 500, "24": "000003e803e8"}


class FakeBulb:
    calls: list = []
    fail_at: set = set()           # IPs that don't answer
    opened = 0                     # connections made (each costs the slow handshake on a real lamp)
    stale = False                  # the next command hits a connection the lamp already dropped
    dps: dict = {}                 # what the lamp's status says

    def __init__(self, ip):
        self.ip, self.socket = ip, None
        FakeBulb.opened += 1

    def close(self):
        pass

    def _answer(self, *call):
        FakeBulb.calls.append((self.ip, *call))
        if FakeBulb.stale:
            FakeBulb.stale = False
            return {"Err": "901", "Error": "Network Error: Unable to Connect"}
        if self.ip in FakeBulb.fail_at:
            return {"Err": "901", "Error": "Network Error: Unable to Connect"}
        return None

    def set_multiple_values(self, dps):
        error = self._answer("set", dict(dps))
        if error:
            return error
        FakeBulb.dps.update(dps)
        return {"dps": dict(dps)}

    late_notice = None             # the next status read returns this partial notice first (as the real lamp does)

    def status(self):
        error = self._answer("status")
        if not error and FakeBulb.late_notice is not None:
            notice, FakeBulb.late_notice = FakeBulb.late_notice, None
            return {"dps": notice}
        return error or {"dps": dict(FakeBulb.dps)}

    def heartbeat(self, nowait=True):
        return self._answer("heartbeat")


def setup(fail_at=(), dps=None):
    wait_threads("smarthome-rescue")             # a search left by the previous test must not run into this one
    smarthome._rescues.clear()
    smarthome._hint_told.clear()
    smarthome.ANNOUNCE = lambda text, level="info", **kw: SAID.append(text)
    SAID.clear()
    folder = Path(tempfile.mkdtemp())
    smarthome.DEVICES_FILE = folder / "smarthome.json"
    smarthome.DEVICES_FILE.write_text(json.dumps([dict(LAMP)]))
    smarthome._bulb = lambda d: FakeBulb(d["ip"])
    smarthome._find_ip = lambda device_id: ("192.168.1.9", 3.5)
    FakeBulb.calls, FakeBulb.fail_at, FakeBulb.opened, FakeBulb.stale = [], set(fail_at), 0, False
    FakeBulb.late_notice = None
    FakeBulb.dps = dict(dps if dps is not None else WHITE)
    for kept in (smarthome._conns, smarthome._down_since, smarthome._state, smarthome._effects,
                 smarthome._last_user_change, smarthome._last_user_off, smarthome._seen, smarthome._published):
        kept.clear()
    os.environ["TUYA_KEY_dev123"] = "kunci-rahasia"


SAID: list[str] = []


def sets():
    return [c[2] for c in FakeBulb.calls if c[1] == "set"]


def test_commands():
    setup()
    assert "menyala" in smarthome.lamp_power(True) and sets()[-1] == {"20": True}
    t0 = time.time()
    assert "dimatikan" in smarthome.lamp_power(False, "lampu kamar") and sets()[-1] == {"20": False}
    assert smarthome.turned_off_by_user_since(t0 - 1)
    FakeBulb.dps = dict(WHITE)
    smarthome._state.clear()
    assert "30 persen" in smarthome.lamp_brightness(30)
    assert sets()[-1] == {"20": True, "21": "white", "22": smarthome._raw(30), "23": 500}    # warmth kept
    smarthome.lamp_color("merah")
    assert sets()[-1] == {"20": True, "21": "colour", "24": "000003e803e8"}
    smarthome.lamp_brightness(30)                                     # a red lamp stays red, only dimmer
    assert sets()[-1] == {"20": True, "21": "colour", "24": "000003e8012c"}
    assert "60 persen" in smarthome.lamp_brightness(30, relative=True)
    smarthome.lamp_color("putih hangat")
    assert sets()[-1] == {"20": True, "21": "white", "22": 1000, "23": 0}
    assert "belum saya kenal" in smarthome.lamp_color("emas")
    status = smarthome.lamp_status()
    assert "kecerahan 100 persen" in status and "hangat" in status, status
    assert events.state["lamp"]["on"] and events.state["lamp"]["brightness"] == 100     # the HUD sees it


def test_scenes():
    setup()
    assert "mode santai" in smarthome.lamp_scene("santai")
    assert sets()[-1] == {"20": True, "21": "white", "22": smarthome._raw(40), "23": 0}
    smarthome.lamp_scene("nonton")                                    # = film: dim blue
    change = sets()[-1]
    assert change["21"] == "colour" and change["24"].endswith("0064"), change     # value 100 = 10 %
    smarthome.lamp_scene("pesta")
    assert sets()[-1] == {"20": True, "21": "scene", "25": smarthome.PARTY_SCENE}
    assert len(smarthome.PARTY_SCENE) == 2 + 6 * 26                   # scene number + 6 colours of 26 hex digits
    assert "belum saya kenal" in smarthome.lamp_scene("galaksi")
    smarthome._state.clear()
    FakeBulb.late_notice = {"25": smarthome.PARTY_SCENE}         # read 1: only the scene; on/off comes with read 2
    assert "menyala" in smarthome.lamp_status() and "mati" not in smarthome.lamp_status()
    assert smarthome.scene_key("lampu untuk membaca") == "baca"
    assert "mode tidur" in smarthome.handle_action("scene", "tidur")


def test_no_key_and_new_ip():
    setup()
    os.environ.pop("TUYA_KEY_dev123")
    assert "Local Key" in smarthome.lamp_power(True) and not FakeBulb.calls
    setup(fail_at={"192.168.1.5"})                 # the router gave the lamp another IP
    t0 = time.time()
    assert "belum menjawab" in smarthome.lamp_power(True) and time.time() - t0 < 2   # said at once, not after 20 s
    wait_threads("smarthome-rescue")                         # found in the background, and the command still done
    assert FakeBulb.calls[-1][0] == "192.168.1.9" and sets()[-1] == {"20": True}
    assert SAID and "sudah tersambung lagi" in SAID[-1], SAID
    saved = json.loads(smarthome.DEVICES_FILE.read_text())[0]
    assert saved["ip"] == "192.168.1.9" and saved["version"] == 3.5          # remembered
    setup(fail_at={"192.168.1.5", "192.168.1.9"})  # heard on the Wi-Fi but takes no commands (its Wi-Fi hung)
    assert "belum menjawab" in smarthome.lamp_power(True)
    assert events.state["lamp"]["online"] is False
    wait_threads("smarthome-rescue")
    assert SAID and "matikan saklarnya" in SAID[-1], SAID
    told, t0 = len(SAID), time.time()
    assert "belum menjawab" in smarthome.lamp_power(True) and time.time() - t0 < 1     # still down: one quick try
    wait_threads("smarthome-rescue")
    assert len(SAID) == told                                 # the switch-off hint is said once, not every time
    setup(fail_at={"192.168.1.5"})
    smarthome._find_ip = lambda device_id: ("", None)     # unplugged: not heard at all
    assert "belum menjawab" in smarthome.lamp_power(True)
    wait_threads("smarthome-rescue")
    assert not SAID                                       # nothing more to say: it isn't there


def test_connection_kept_open():
    setup()
    smarthome.lamp_power(True)
    smarthome.lamp_brightness(50)
    smarthome.lamp_color("biru")
    assert FakeBulb.opened == 1, FakeBulb.opened                 # one handshake for all three
    FakeBulb.stale = True                                        # the lamp dropped it: reopened, command still done
    assert "dimatikan" in smarthome.lamp_power(False)
    assert FakeBulb.opened == 2 and sets()[-1] == {"20": False}


def wait_threads(name: str, timeout: float = 5):
    end = time.time() + timeout
    while any(t.name == name for t in threading.enumerate()) and time.time() < end:
        time.sleep(0.02)


def test_effects():
    setup()
    smarthome.flash(times=2)                                     # a reminder: the lamp blinks twice
    wait_threads("lamp-flash")
    assert sets() == [{"20": False}, {"20": True}, {"20": False}, {"20": True}], sets()
    setup(dps={**WHITE, "20": False})
    smarthome.flash()                                            # off: never blinks in the dark
    wait_threads("lamp-flash")
    assert not sets()
    setup(dps={**WHITE, "20": False})
    smarthome.sunrise(seconds=0.13)                              # an alarm: up from dim and warm
    wait_threads("lamp-sunrise")
    steps = sets()
    assert len(steps) == 13 and steps[0]["22"] < steps[6]["22"] < steps[-1]["22"] == 1000, steps
    assert steps[0]["23"] == 0 and steps[-1]["23"] == 400


def test_automations():
    setup(dps={**WHITE, "20": False})
    home_auto.STATE_FILE = Path(tempfile.mkdtemp()) / "home_auto.json"
    home_auto._place = lambda: (-5.41727, 105.42104)
    said = []
    home_auto.ANNOUNCE = lambda text, level="info", **kw: said.append(text) or True
    presence._away_since = 0.0
    home_auto.config.LAMP_AUTO = True
    day = datetime.date.today()
    rise, sunset = home_auto.sun_times(-5.41727, 105.42104, day)
    assert rise.hour in (5, 6) and sunset.hour in (17, 18)
    home_auto.check(sunset - datetime.timedelta(minutes=30))      # still light: nothing
    assert not sets()
    home_auto.check(sunset - datetime.timedelta(minutes=10))      # dusk, user here, lamp off: warm light on
    assert sets()[-1] == {"20": True, "21": "white", "22": 1000, "23": 0} and "gelap" in said[-1], (sets(), said)
    home_auto.check(sunset)                                       # once a day
    assert len(sets()) == 1
    # late evening: dimmed once (the user changed nothing for a while)
    home_auto.config.LAMP_NIGHT_HOUR, home_auto.config.LAMP_NIGHT_BRIGHTNESS = 22, 30
    home_auto.check(datetime.datetime.combine(day, datetime.time(22, 1)))
    assert sets()[-1] == {"20": True, "21": "white", "22": smarthome._raw(30), "23": 0} and "redupkan" in said[-1]
    # away long: dimmed, and the brightness it had comes back with the user — but only when the room is quiet too
    presence._away_since = time.time() - 40 * 60
    home_auto.config.LAMP_AWAY_MINUTES = 30
    presence._voice_at = time.time() - 5 * 60                    # someone talked 5 minutes ago: still in the room
    count = len(sets())
    home_auto.check(datetime.datetime.combine(day, datetime.time(22, 30)))
    assert len(sets()) == count, "dimmed while someone was talking in the room"
    presence._voice_at = time.time() - 45 * 60                   # nobody heard for 45 minutes
    home_auto._away["looked_at"] = 0.0
    home_auto.check(datetime.datetime.combine(day, datetime.time(22, 30)))
    assert sets()[-1]["22"] == smarthome._raw(home_auto.AWAY_BRIGHTNESS), sets()
    presence._away_since = 0.0
    home_auto.on_return(2400, "input", False, time.time() - 2400,
                        now=datetime.datetime.combine(day, datetime.time(22, 45)))
    assert sets()[-1]["22"] == smarthome._raw(30)
    # back after dark with the lamp off: on — but not after midnight, and not when the user turned it off
    setup(dps={**WHITE, "20": False})
    home_auto.on_return(1800, "input", False, 0, now=datetime.datetime.combine(day, datetime.time(19, 0)))
    assert sets() and sets()[-1]["20"] is True
    setup(dps={**WHITE, "20": False})
    home_auto.on_return(1800, "input", False, 0, now=datetime.datetime.combine(day, datetime.time(1, 30)))
    assert not sets()
    setup(dps={**WHITE, "20": False})
    smarthome._last_user_off["dev123"] = datetime.datetime.combine(day, datetime.time(18, 30)).timestamp()
    home_auto.on_return(1800, "input", False, 0, now=datetime.datetime.combine(day, datetime.time(19, 0)))
    assert not sets()


def test_protocol_lamp_steps():
    import protocols
    old = json.loads(json.dumps(protocols.DEFAULT_PROTOCOLS))
    for p in old.values():                                       # a protocols.json written before the lamp came
        p["steps"] = [s for s in p["steps"] if not s["tool"].startswith("lamp_")]
    assert protocols._add_lamp_steps(old)
    tidur = [s["tool"] for s in old["tidur"]["steps"]]
    assert tidur.index("lamp_power") == tidur.index("sleep_pc") - 1      # the lamp goes off before the PC sleeps
    assert old["fokus"]["steps"][-1]["tool"] == "set_timer"
    assert not protocols._add_lamp_steps(old)                    # only once
    old["kerja"]["steps"] = [s for s in old["kerja"]["steps"] if s["tool"] != "lamp_scene"]
    assert not protocols._add_lamp_steps(old)                    # a step the user removed stays removed


if __name__ == "__main__":
    ok = True
    for test in (test_commands, test_scenes, test_no_key_and_new_ip, test_connection_kept_open, test_effects,
                 test_automations, test_protocol_lamp_steps):
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            ok = False
            import traceback
            print(f"✗ {test.__name__}\n{e}")
            traceback.print_exc()
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
