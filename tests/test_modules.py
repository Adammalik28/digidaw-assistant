"""
Tests for modules that had none (3 Nov 2026): alarms (they must ring, also after a restart), long-term memory,
the local AI's "I did it" without doing anything, and the phone's push subscriptions. Offline: no sound, no
network (the memory's embeddings are faked).

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_modules.py
"""
import datetime
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config       # noqa: E402
import alarms       # noqa: E402
import memory       # noqa: E402
import brain        # noqa: E402
import mobile_push  # noqa: E402


def _fresh_alarms():
    alarms.STATE_FILE = Path(tempfile.mkdtemp()) / "alarms.json"
    with alarms._lock:
        alarms._items.clear()
    alarms._ringing = None


def test_timer_and_alarm():
    _fresh_alarms()
    reply = alarms.set_timer(10)
    assert "10 menit" in reply, reply
    timer = alarms._items[-1]
    assert timer["kind"] == "timer" and 590 < timer["due"] - time.time() <= 600
    reply = alarms.set_alarm("06:30")
    alarm = next(i for i in alarms._items if i["kind"] == "alarm")
    due = datetime.datetime.fromtimestamp(alarm["due"])
    assert (due.hour, due.minute) == (6, 30) and 0 < alarm["due"] - time.time() <= 24 * 3600, reply
    alarms.set_alarm("07:00", repeat="hari kerja")
    weekdays = next(i for i in alarms._items if i.get("repeat"))
    assert weekdays["repeat"] == [0, 1, 2, 3, 4], weekdays
    assert datetime.datetime.fromtimestamp(weekdays["due"]).weekday() < 5
    listed = alarms.list_timers()
    assert "06.30" in listed and "07.00" in listed, listed
    saved = json.loads(alarms.STATE_FILE.read_text())
    assert len(saved) == 3 and alarms.STATE_FILE.stat().st_mode & 0o777 == 0o600       # kept, for this user only
    assert "1 dibatalkan" in alarms.cancel_timers("06.30")
    assert not any(i.get("time") == "06:30" for i in alarms._items)
    assert "Tidak ada" in alarms.cancel_timers("rapat")


def test_alarms_survive_a_restart():
    """Saved alarms come back; one that rang while Digidaw was off is reported, a repeating one moves on."""
    _fresh_alarms()
    now = time.time()
    past = datetime.datetime.fromtimestamp(now - 3600)
    alarms.STATE_FILE.write_text(json.dumps([
        {"id": "a", "kind": "alarm", "label": "", "due": now - 3600, "time": past.strftime("%H:%M"), "repeat": None},
        {"id": "b", "kind": "alarm", "label": "olahraga", "due": now + 7200, "time": "23:59", "repeat": None},
        {"id": "c", "kind": "alarm", "label": "", "due": now - 600, "time": "06:00", "repeat": [0, 1, 2, 3, 4, 5, 6]},
    ]))
    said, fired = [], []
    real = (alarms._say, alarms._fire, alarms.threading.Timer)

    class InstantTimer:
        def __init__(self, delay, fn, args=()):
            self.fn, self.args = fn, args

        def start(self):
            self.fn(*self.args)
    alarms._say = lambda text, level="reminder": said.append(text)
    alarms._fire = lambda item: fired.append(item)
    alarms.threading.Timer = InstantTimer
    try:
        alarms.start()
        ids = sorted(i["id"] for i in alarms._items)
        assert ids == ["b", "c"], ids                                   # the missed one-off is gone, reported
        repeating = next(i for i in alarms._items if i["id"] == "c")
        assert repeating["due"] > now                                   # tomorrow's 06.00, not ringing now
        assert said and "terlewat" in said[0], said
    finally:
        alarms._say, alarms._fire, alarms.threading.Timer = real


def test_snooze_and_stop():
    _fresh_alarms()
    assert "Tidak ada alarm yang sedang berbunyi" in alarms.stop_alarm()
    item = {"id": "x", "kind": "alarm", "label": "", "due": time.time(), "time": "06:00", "repeat": None}
    alarms._ringing = item
    reply = alarms.snooze_alarm(5)
    assert "5 menit" in reply, reply
    snoozed = alarms._items[-1]
    assert 290 < snoozed["due"] - time.time() <= 300, snoozed
    alarms._ringing = item
    alarms._stop_ring.clear()
    assert "dimatikan" in alarms.stop_alarm() and alarms._stop_ring.is_set()
    alarms._ringing = None


def _fake_embedding(text: str) -> list[float]:
    """A stable stand-in for Gemini's embeddings: the same words give the same vector."""
    vector = [0.0] * memory.EMBED_DIMS
    for word in text.lower().split():
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % memory.EMBED_DIMS] += 1.0
    return vector


def test_memory():
    folder = Path(tempfile.mkdtemp())
    memory.SEMANTIC_FILE, memory.MEMORY_FILE = folder / "memory_semantic.json", folder / "memory.json"
    memory._get_embedding = _fake_embedding
    assert "Tersimpan" in memory.add("saya suka kopi hitam tanpa gula")
    assert "sudah saya ingat" in memory.add("Saya suka kopi hitam tanpa gula.")      # the same fact twice
    assert "Tersimpan" in memory.add("ulang tahun ibu tanggal 20 Oktober")
    assert memory.all_facts() == ["saya suka kopi hitam tanpa gula", "ulang tahun ibu tanggal 20 Oktober"]
    assert memory.SEMANTIC_FILE.stat().st_mode & 0o777 == 0o600
    memory.SEMANTIC_FILE.write_text('[{"fact": "rusak')                             # damaged on disk
    assert memory.all_facts() == ["saya suka kopi hitam tanpa gula"]                # the .bak (one write older)
    assert "sudah saya hapus" in memory.remove("kopi")
    assert memory.all_facts() == []


def test_local_ai_claims():
    """The local AI saying it did something without doing it is caught (then Gemini acts)."""
    for claim in ("Membuka Firefox untuk Anda.", "Baik, saya sudah mematikan lampunya.", "Sudah saya kirim, Tuan."):
        assert brain.ACTION_CLAIM_RE.search(claim) or brain.DONE_CLAIM_RE.search(claim), claim
    for talk in ("Langit biru karena hamburan cahaya.", "Saya tahu jawabannya: 12.", "Saya senang membantu."):
        assert not (brain.ACTION_CLAIM_RE.search(talk) or brain.DONE_CLAIM_RE.search(talk)), talk
    for order in ("jawab oke", "balas pesannya nanti", "kirim pesan ke rina", "berikan jawaban oke", "nyalakan lampu"):
        assert brain.ACTION_VERB_RE.match(order), order                 # an order: "Oke." alone isn't an answer
    assert not brain.ACTION_VERB_RE.match("kenapa langit biru")


def test_push_subscriptions():
    mobile_push.SUBS_FILE = Path(tempfile.mkdtemp()) / "push_subscriptions.json"
    mobile_push._subs.clear()
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "key", "auth": "secret"}}
    assert mobile_push.subscribe(good, "sesi-1") and mobile_push.subscribed("sesi-1")
    assert not mobile_push.subscribe({"endpoint": "http://evil.example/x", "keys": good["keys"]}, "sesi-1")
    assert not mobile_push.subscribe({"endpoint": "https://x.example/y", "keys": {}}, "sesi-1")
    assert mobile_push.SUBS_FILE.stat().st_mode & 0o777 == 0o600
    mobile_push._subs.clear()
    mobile_push._load()
    assert len(mobile_push._subs) == 1                                  # kept across a restart
    mobile_push.unsubscribe(session="sesi-1")
    assert not mobile_push.subscribed("sesi-1") and json.loads(mobile_push.SUBS_FILE.read_text()) == []


if __name__ == "__main__":
    ok = True
    for test in (test_timer_and_alarm, test_alarms_survive_a_restart, test_snooze_and_stop, test_memory,
                 test_local_ai_claims, test_push_subscriptions):
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
