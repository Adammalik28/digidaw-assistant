"""
Tests for what Digidaw notices by itself: away / back (presence.py), message announcements (notifications.py), the
forecast and rain warning (weather.py) and the laptop's temperature (system_control.py).

Run:  PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_awareness.py
(also works with pytest). Nothing is spoken and nothing goes to the internet: the forecast is a made-up one.
"""
import datetime
import os
import sys
import time
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config               # noqa: E402
import notifications        # noqa: E402
import presence             # noqa: E402
import system_control       # noqa: E402
import weather              # noqa: E402

T = config.USER_TITLE


def test_presence():
    presence._watching, config.AWAY_MINUTES = True, 10
    t0 = 1000.0
    presence._last_active, presence._away_since = t0, 0.0
    try:
        presence._check(t0 + 300, playing=False)
        assert not presence.is_away()
        presence._check(t0 + 601, playing=False)
        assert presence.is_away()                                      # left at their last activity, t0
        presence._check(t0 + 700, playing=True)                        # music played meanwhile
        assert presence._activity(t0 + 900, "input", locked=True) is None and presence.is_away()   # the lock screen
        assert presence._activity(t0 + 1500, "input", locked=False) == (1500.0, "input", True, t0)
        assert not presence.is_away() and presence._activity(t0 + 1501, "input", locked=False) is None
        presence._check(t0 + 1501 + 601, playing=False)
        assert presence._activity(t0 + 3000, "command", locked=True)[1] == "command"   # a voice command: here for sure
        presence.hold("Gambar sudah jadi.")
        assert presence.take_held() == ["Gambar sudah jadi."] and presence.take_held() == []
    finally:
        presence._last_active, presence._away_since = time.time(), 0.0


def test_input_devices():
    listing = "\n\n".join([
        'N: Name="Power Button"\nH: Handlers=kbd event2\nB: EV=3',
        'N: Name="AT Translated Set 2 keyboard"\nH: Handlers=sysrq kbd leds event4 rfkill\nB: EV=120013',
        'N: Name="Logitech G304"\nH: Handlers=sysrq kbd leds event5 mouse0\nB: EV=12001f',
        'N: Name="USB AUDIO"\nH: Handlers=kbd event7\nB: EV=13',
        'N: Name="ELAN Touchpad"\nH: Handlers=event13 mouse2\nB: EV=1b',
        'N: Name="Lid Switch"\nH: Handlers=event0\nB: EV=21'])
    assert presence.input_devices(listing) == ["/dev/input/event4", "/dev/input/event5", "/dev/input/event13"]


def test_messages():
    config.ANNOUNCE_MESSAGES = "personal"
    rina = {"app": "Whatsie", "summary": "Rina", "body": "nanti jadi kumpul?", "at": 0, "read": False}
    group = {"app": "Whatsie", "summary": "Kelas TI", "body": "Sari: besok libur", "at": 0, "read": False}
    budi = {"app": "Telegram", "summary": "Budi", "body": "oke", "at": 0, "read": False}
    assert notifications._chat(group) == ("Kelas TI", True) and notifications._chat(rina) == ("Rina", False)
    assert notifications.announcement([rina]) == f"{T}, ada pesan WhatsApp dari Rina."
    assert notifications.announcement([rina, dict(rina)]) == f"{T}, Rina mengirim 2 pesan di WhatsApp."
    assert notifications.announcement([rina, budi, group]) == f"{T}, ada 3 pesan baru, dari Rina, Budi, dan grup Kelas TI."
    now = 10 ** 9
    assert notifications.worth_telling([rina, group], now=now) == [rina]               # groups only when wanted
    assert notifications.worth_telling([rina, budi], {"class": "whatsie", "title": "WhatsApp"}, now) == [budi]
    notifications._told["Rina"] = now - 60
    assert notifications.worth_telling([rina], now=now) == []                         # announced a minute ago
    notifications._told.clear()
    fresh = time.time()
    notifications._items.extend([dict(rina, at=fresh), dict(group, at=fresh), dict(budi, at=fresh - 3600)])
    try:
        assert notifications.unread_summary(since=fresh - 5) == "ada 2 pesan baru, dari Rina dan grup Kelas TI."
        said = notifications.read_notifications(messages_only=True, sender="Rina")
        assert "nanti jadi kumpul" in said and "libur" not in said                     # only what was offered
        assert notifications.read_notifications(messages_only=True, sender="sari").count("Dari Sari di Kelas TI") == 1
    finally:
        notifications._items.clear()
    notifications.offer_reading("Rina")
    assert notifications.take_offer() == "Rina" and notifications.take_offer() is None


def _forecast() -> dict:
    """Dry all of 1 November except 70 % and 0.5 mm between 14:00 and 15:00."""
    pct, mm, code = [0] * 24, [0.0] * 24, [2] * 24
    pct[15], mm[15], code[15] = 70, 0.5, 61
    return {"current": {"precipitation": 0, "weather_code": 2, "temperature_2m": 30.4},
            "hourly": {"time": [f"2026-11-01T{h:02d}:00" for h in range(24)], "precipitation_probability": pct,
                       "precipitation": mm, "weather_code": code},
            "daily": {"time": ["2026-10-31", "2026-11-01", "2026-11-02"], "weather_code": [2, 61, 3],
                      "temperature_2m_min": [24, 24, 23], "temperature_2m_max": [31, 32, 32],
                      "precipitation_probability_max": [10, 80, 20]}}


def test_forecast():
    assert weather.candidates("Kota Bekasi bantar gebang")[6:8] == ["bantar gebang", "bantargebang"]
    data = _forecast()
    day = datetime.datetime(2026, 11, 1)
    assert weather.rain_soon(data, day.replace(hour=13, minute=20)) == ("sekitar jam 14.00", 70)
    assert weather.rain_soon(data, day.replace(hour=13, minute=55)) == ("sebentar lagi", 70)
    assert weather.rain_soon(data, day.replace(hour=11)) is None                      # still three hours away
    assert weather._day(data, "Bantargebang", 1) == (f"Besok, Minggu, di Bantargebang: hujan ringan, suhu 24 "
                                                       f"sampai 32 derajat, peluang hujan 80 persen, paling mungkin "
                                                       f"sekitar jam 14.00, {T}.")
    assert weather._day(data, "Bantargebang", 2).endswith(f"peluang hujannya kecil (20 persen), {T}.")
    data["current"]["precipitation"] = 0.4
    assert weather.rain_soon(data, day.replace(hour=13, minute=20)) is None           # raining already: no news


def test_temperatures():
    S = namedtuple("S", "label current")
    intel = {"coretemp": [S("Package id 0", 71.0), S("Core 0", 65.0)], "acpitz": [S("", 50.0)],
             "nvme": [S("Composite", 41.8), S("Sensor 1", 50.0)]}
    assert system_control.temperatures(intel, gpu=False) == {"cpu": 71.0, "ssd": 41.8}
    amd = {"k10temp": [S("Tctl", 80.0)], "amdgpu": [S("edge", 60.0)]}
    assert system_control.temperatures(amd) == {"cpu": 80.0, "gpu": 60.0}


if __name__ == "__main__":
    ok = True
    for test in (test_presence, test_input_devices, test_messages, test_forecast, test_temperatures):
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            ok = False
            print(f"✗ {test.__name__}\n{e}")
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
