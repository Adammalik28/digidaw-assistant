"""
Tests for the "make it really mature" work of 3 Nov 2026: data files that survive a crash, a log with times and
without other people's messages, alarms that still ring after "mode tidur", the systemd service and its heartbeat,
and router fixes from replaying the real log ("jam", "buka ho", "bukan terminal", "lagi"). Offline, no sound.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_mature.py
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config         # noqa: E402
import storage        # noqa: E402
import logfile        # noqa: E402
import notifications  # noqa: E402
import alarms         # noqa: E402
import brain          # noqa: E402
import service        # noqa: E402
import settings       # noqa: E402


def test_storage_survives_damage():
    folder = Path(tempfile.mkdtemp())
    path = folder / "alarms.json"
    storage.save_json(path, [{"id": 1}], backup=True, mode=storage.PRIVATE)
    storage.save_json(path, [{"id": 1}, {"id": 2}], backup=True, mode=storage.PRIVATE)
    assert json.loads(path.read_text()) == [{"id": 1}, {"id": 2}]
    assert json.loads((folder / "alarms.json.bak").read_text()) == [{"id": 1}]      # the previous good copy
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('[{"id": 1}, {"id"')                                              # a write cut off half-way
    assert storage.load_json(path, []) == [{"id": 1}]                                  # the .bak takes over
    storage.save_json(path, [{"id": 3}], backup=True)
    assert json.loads((folder / "alarms.json.bak").read_text()) == [{"id": 1}]         # damage never becomes the .bak
    assert storage.load_json(folder / "nothing.json", {"x": 1}) == {"x": 1}
    assert not list(folder.glob(".*.tmp"))


def test_log_has_times_and_no_messages():
    out = io.StringIO()
    log = logfile.TimestampedLog(out)
    notifications.PRIVATE_TEXTS.append("nanti aku telepon ya")
    log.write("DIGIDAW: Dari Rina: nanti aku telepon ya.\n· terdengar: 3 kata")
    log.write(" (diabaikan)\n\n")
    lines = out.getvalue().split("\n")
    assert lines[0][2] == ":" and lines[0][5] == ":" and "‹isi pesan›" in lines[0] and "telepon" not in out.getvalue()
    assert lines[1][8:].startswith(" · terdengar: 3 kata (diabaikan)"), lines[1]       # one time per line, not per write
    assert lines[2] == ""                                                                 # empty lines stay empty
    assert log.getvalue() == out.getvalue()                                               # the rest is the file's


def test_alarm_keeps_laptop_awake():
    import system_control as sc
    ran = []
    real = (sc.ld.run, alarms.next_ring)
    sc.ld.run = lambda args, **kw: ran.append(args)
    import sysadmin
    sysadmin.updating = lambda: False            # (the machine this runs on may really be updating)
    try:
        alarms.next_ring = lambda hours=24: {"kind": "alarm", "due": time.time() + 7 * 3600}
        reply = sc._do_power_linux("sleep")
        assert "tidak saya tidurkan" in reply and "alarm pukul" in reply, reply
        time.sleep(4.5)
        assert ["systemctl", "suspend"] not in ran and any("dpms" in a for a in ran), ran
        alarms.next_ring = lambda hours=24: None
        ran.clear()
        assert "sleep" in sc._do_power_linux("sleep")
        time.sleep(2.5)
        assert ["systemctl", "suspend"] in ran
    finally:
        sc.ld.run, alarms.next_ring = real


def test_alarm_loud_enough_and_next_ring():
    calls = []
    real = (alarms.ld.get_volume, alarms.ld.set_volume, alarms.ld.set_mute)
    alarms.ld.set_volume = lambda p: calls.append(("vol", p)) or True
    alarms.ld.set_mute = lambda m: calls.append(("mute", m)) or True
    try:
        alarms.ld.get_volume = lambda: (10, False)                      # "mode tidur" left it at 10 %
        assert alarms._loud_enough() == (10, False) and ("vol", config.ALARM_MIN_VOLUME) in calls
        calls.clear()
        alarms.ld.get_volume = lambda: (80, False)
        assert alarms._loud_enough() is None and not calls             # loud already: untouched
    finally:
        alarms.ld.get_volume, alarms.ld.set_volume, alarms.ld.set_mute = real
    saved = list(alarms._items)
    try:
        alarms._items[:] = [{"id": "a", "kind": "alarm", "due": time.time() + 3600},
                            {"id": "b", "kind": "alarm", "due": time.time() + 30 * 3600}]
        assert alarms.next_ring(16)["id"] == "a" and alarms.next_ring(0.5) is None
    finally:
        alarms._items[:] = saved
    alarms._awake_told["day"] = ""
    real_battery = alarms._on_battery_device
    alarms._on_battery_device = lambda: True
    try:
        assert "laptop tetap menyala" in alarms._awake_note({"due": time.time() + 8 * 3600})
        assert alarms._awake_note({"due": time.time() + 8 * 3600}) == ""          # once a day
    finally:
        alarms._on_battery_device = real_battery


def test_service_and_heartbeat():
    unit = settings.service_unit()
    for line in ("KillMode=process", "Restart=on-failure", "WatchdogSec=", "NotifyAccess=main", "start_digidaw.sh"):
        assert line in unit, line
    folder = tempfile.mkdtemp()
    address = os.path.join(folder, "notify")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(address)
    receiver.settimeout(2)
    saved = {k: os.environ.get(k) for k in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")}
    os.environ.update(NOTIFY_SOCKET=address, WATCHDOG_USEC="150000", WATCHDOG_PID=str(os.getpid()))
    healthy, stop = {"ok": True}, threading.Event()
    try:
        threading.Thread(target=service.heartbeat, args=(lambda max_silence: healthy["ok"], stop), daemon=True).start()
        assert receiver.recv(64) == b"READY=1" and receiver.recv(64) == b"WATCHDOG=1"
        healthy["ok"] = False                                    # Digidaw hangs: no more heartbeats
        time.sleep(0.2)
        receiver.settimeout(0.3)
        while True:
            try:
                receiver.recv(64)
            except socket.timeout:
                break
        try:
            assert receiver.recv(64) is None, "a heartbeat while hanging"
        except socket.timeout:
            pass
    finally:
        stop.set()
        receiver.close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert service.notify("READY=1") is (bool(os.environ.get("NOTIFY_SOCKET")))


def test_core_alive():
    import core
    c = core.DigidawCore(voice_enabled=False)
    assert c.alive()                                             # not listening yet: counts as alive
    c._loop_tick = time.time() - 200
    assert not c.alive(max_silence=60)
    c._loop_tick = time.time()
    assert c.alive(max_silence=60)


def test_router_fixes_from_the_log():
    assert brain._route("jam") == ("get_time", {}) and brain._route("waktu") == ("get_time", {})
    assert brain._route("buka jam") == ("open_application", {"name": "jam"})
    assert brain._route("buka ho") == ("show_hud", {}) and brain._route("buka hood") == ("show_hud", {})
    assert brain._route("atur kecerahan di 10%") == ("set_brightness", {"level": 10})
    assert brain._route("bukan itu") is None                                  # "no", an answer: not "buka itu"
    brain._last_fast.update(tool="", kwargs={}, at=0)
    assert brain._route("lagi") is None                                       # nothing to repeat
    brain._last_fast.update(tool="volume_up", kwargs={"amount": 10}, at=time.time())
    assert brain._route("lagi") == ("volume_up", {"amount": 10})
    assert brain._route("dikit lagi") == ("volume_up", {"amount": 5})
    assert brain._route("dikit dong") == ("volume_up", {"amount": 5})
    brain._last_fast.update(tool="open_application", kwargs={"name": "firefox"}, at=time.time())
    assert brain._route("lagi") is None                                       # opening Firefox again: no
    brain._last_fast.update(tool="volume_up", kwargs={"amount": 10}, at=time.time() - 600)
    assert brain._route("lagi") is None                                       # long ago


def test_repeat_and_corrections_keep_separate_memories():
    """"lagi" (the last fast command) and the correction memory ("bukan, maksudnya …") must not share one variable:
    a fast command then broke the local AI's tool choice and corrections with a KeyError."""
    brain._last_action.clear()
    brain._last_fast.update(tool="volume_up", kwargs={"amount": 10}, at=time.time())
    assert brain._local_tools("kecilkan suara") is not None           # (read the correction memory: must not crash)
    assert not brain._last_action                                     # a fast command doesn't touch that memory
    brain._last_action.update(text="buka firefox", calls=[("open_application", {"name": "firefox"}, "ok")], at=time.time())
    brain._last_action.clear()                                        # a correction used it up
    assert brain._route("lagi") == ("volume_up", {"amount": 10})      # "lagi" still works


def test_metrics_report():
    import metrics
    metrics.DIR = Path(tempfile.mkdtemp())
    metrics._day.clear()
    for _ in range(9):
        metrics.count("ting")
        metrics.count("ting_berhasil")
    metrics.count("ting")
    metrics.count("ting_tanpa_perintah")
    metrics.count("perintah_suara", 9)
    metrics.count("rute_cepat", 7)
    metrics.count("rute_gemini", 2)
    for value in (0.4, 0.5, 0.7):
        metrics.timing("dikenali", value)
        metrics.timing("suara_mulai", 1.2)
    report = metrics.performance_report(1)
    assert "dipanggil 10 kali" in report and "90 persen" in report and "7 lewat jalur cepat" in report, report
    assert metrics.summary(1)["call_rate"] == 90 and metrics.summary(1)["voice"] == 1.2
    metrics.save()
    saved = json.loads((metrics.DIR / f"{metrics._day['day']}.json").read_text())
    assert saved["counts"]["ting"] == 10 and (metrics.DIR / f"{metrics._day['day']}.json").stat().st_mode & 0o777 == 0o600
    assert brain._route("laporan kinerja minggu ini") == ("performance_report", {"days": 7})


if __name__ == "__main__":
    ok = True
    for test in (test_storage_survives_damage, test_log_has_times_and_no_messages, test_alarm_keeps_laptop_awake,
                 test_alarm_loud_enough_and_next_ring, test_service_and_heartbeat, test_core_alive,
                 test_router_fixes_from_the_log, test_repeat_and_corrections_keep_separate_memories,
                 test_metrics_report):
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as e:
            ok = False
            import traceback
            print(f"✗ {test.__name__}: {e!r}")
            traceback.print_exc()
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
