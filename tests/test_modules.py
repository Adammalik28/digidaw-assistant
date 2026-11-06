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
import agenda       # noqa: E402
agenda.SMALL_HOURS_UNTIL = 0                # (these run at any hour; what "besok" means after midnight: tests/test_real_use.py)
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


def test_memory_is_learned_by_asking():
    """Said in passing: the local model writes the fact, Digidaw asks, "ya" saves it, silence or "tidak" doesn't."""
    import local_llm
    import safety
    folder = Path(tempfile.mkdtemp())
    memory.SEMANTIC_FILE, memory.MEMORY_FILE = folder / "memory_semantic.json", folder / "memory.json"
    memory._get_embedding = _fake_embedding
    asked = []
    real = (local_llm.generate, local_llm.available)
    local_llm.available = lambda refresh=False: True

    def answering(fact):
        def fake(system, user, schema=None, max_tokens=200, temperature=0.2, history=None):
            asked.append(user)
            return json.dumps({"ada": bool(fact), "fakta": fact})
        local_llm.generate = fake
    try:
        # the words alone rule most out, without asking the model at all
        answering("saya x")
        for sentence in ("buka firefox", "jam berapa sekarang", "aku capek banget hari ini", "kamu suka apa", "apa hobi saya",
                         "aku mau makan", "tadi aku kerja", "password saya rahasia123", "pin kartu saya 1234",
                         "saya lagi suka lagu ini", "tolong ingetin aku kerja"):
            assert memory.learnable(sentence) == "", sentence
        assert not asked
        # the model's answer is checked too
        answering("")
        assert memory.learnable("aku suka ini") == "" and asked
        answering("saya suka kopi hitam dari aceh")
        assert memory.learnable("aku suka teh") == ""                  # invented: not what was said
        answering("saya alergi kacang")
        assert memory.learnable("aku alergi kacang") == "saya alergi kacang"
        # the whole way, with the question
        brain._calls_this_turn.clear()
        safety.cancel()
        question = brain.memory_question("aku alergi kacang")
        assert "ingat" in question and "alergi kacang" in question and safety.has_pending() and config.USER_TITLE in question
        assert "Tersimpan" in safety.confirm() and memory.all_facts() == ["saya alergi kacang"]
        assert memory.learnable("aku alergi kacang") == ""             # known already: not asked again
        answering("saya kuliah di kota")
        assert brain.memory_question("aku kuliah di kota") and safety.classify_answer("tidak") == "no"
        safety.cancel()
        assert memory.all_facts() == ["saya alergi kacang"]            # "tidak": not saved
        # a command that did something isn't a fact, and a question already waiting comes first
        brain._calls_this_turn.append(("set_timer", {}, "ok"))
        assert brain.memory_question("aku kuliah di kota") == ""
        brain._calls_this_turn.clear()
        safety.guard("hapus file", lambda: "x", always=True)
        assert brain.memory_question("aku kuliah di kota") == ""
        safety.cancel()
        # the local brain uses what it knows when it talks
        assert "alergi kacang" in brain._talk_prompt(False, "aku alergi apa ya")
        assert "alergi kacang" not in brain._talk_prompt(False, "buatkan puisi")     # only what the sentence is about: it was brought up everywhere
        # forgetting, by voice
        assert "Yang mana" in memory.remove("saya") and memory.all_facts()     # not everything at once
        memory.add("saya suka kopi")
        assert "sudah saya lupakan: saya suka kopi" in memory.forget_latest() and memory.all_facts() == ["saya alergi kacang"]
        assert "sudah saya hapus" in memory.remove("kalau kacang") and memory.all_facts() == []
    finally:
        local_llm.generate, local_llm.available = real
        safety.cancel()


def test_memory_and_favourite_routes():
    import media
    import habits
    import system_control as sc
    assert brain._route("lupakan yang tadi") == ("forget_latest", {}) and brain._route("hapus yang itu") == ("forget_latest", {})
    assert brain._route("lupakan kopi") == ("forget", {"keyword": "kopi"})
    assert brain._route("lupakan percakapan") != ("forget", {"keyword": "percakapan"})
    for said in ("apa yang kamu ingat tentang aku", "apa saja yang kamu tahu", "kamu tahu apa tentang saya"):
        assert brain._route(said) == ("recall_memories", {}), said
    for said in ("putar lagu kesukaanku", "putar lagu favorit saya", "setel yang biasa", "putar musik favoritku"):
        assert brain._route(said) == ("play_favourite", {}), said
    played = []
    real = (media.play_youtube, habits.favourites)
    media.play_youtube = lambda query, mix=False: played.append(query) or "ok"
    try:
        habits.favourites = lambda kind, n=6: ["do i wanna know", "be my baby"]
        assert media.play_favourite() == "ok" and played[0] in ("do i wanna know", "be my baby")
        habits.favourites = lambda kind, n=6: []
        assert "belum tahu" in media.play_favourite()
    finally:
        media.play_youtube, habits.favourites = real
    assert "Yang saya ingat" in sc.recall_memories() or "kosong" in sc.recall_memories()


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


def _schedule_file(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "jadwal.tsv"
    path.write_text(text, encoding="utf-8")
    config.WEEKLY_SCHEDULE_FILE = path
    return path


NO_SCHEDULE = Path(tempfile.mkdtemp()) / "tidak-ada.tsv"
config.WEEKLY_SCHEDULE_FILE = NO_SCHEDULE      # these tests don't depend on the real schedule of whoever runs them


def test_adding_events_by_sentence():
    """Real sentences: the small model lost the time and made up dates; now the sentence decides, and Digidaw asks the rest."""
    import agenda
    agenda.STATE_FILE = Path(tempfile.mkdtemp()) / "agenda.json"
    agenda._sync_google = lambda: None
    agenda._events[:] = []
    agenda._draft.clear()
    today = datetime.date.today()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    parts = agenda._event_parts("MTS cyber security besok jam jam 07")
    assert (parts["title"], parts["date"].isoformat(), parts["time"]) == ("MTS cyber security", tomorrow, "07:00"), parts
    parts = agenda._event_parts("tanggal 7 November 6 acaranya kuli")            # Google dropped "jam"
    assert (parts["title"], parts["date"].day, parts["date"].month, parts["time"]) == ("kuli", 7, 11, "06:00"), parts
    parts = agenda._event_parts("tambah acara kuliah tanggal 10 jam 13")
    assert (parts["title"], parts["date"].day, parts["time"]) == ("kuliah", 10, "13:00"), parts
    parts = agenda._event_parts("tambah jadwal dokter gigi tanggal 12 desember")
    assert (parts["title"], parts["date"].month, parts["time"]) == ("dokter gigi", 12, ""), parts
    assert agenda._event_parts("tambahkan agenda")["title"] == ""

    # the whole conversation
    assert "apa dan kapan" in agenda.handle("tambahkan agenda") and not agenda._events    # no event called "agenda"
    assert "lewat" not in (said := agenda.handle("MTS cyber security besok jam jam 07")) and "MTS" in said
    event = agenda._events[-1]
    assert (event["title"], event["date"], event["time"]) == ("MTS cyber security", tomorrow, "07:00"), event
    assert not agenda._draft
    assert agenda.handle("tambah acara") and agenda._draft
    assert "Kapan" in agenda.handle("rapat tim")
    assert "catat" in agenda.handle("besok jam 9 malam")
    assert (agenda._events[-1]["title"], agenda._events[-1]["time"]) == ("rapat tim", "21:00"), agenda._events[-1]
    assert agenda.handle("tambah agenda") and "tidak jadi" in agenda.handle("tidak jadi") and not agenda._draft
    assert agenda.handle("tambah agenda") and agenda.handle("buka firefox") is None and not agenda._draft   # moved on
    agenda.add_event("agenda", tomorrow)                                          # the online AI can't name it that either
    assert agenda._draft and len(agenda._events) == 2
    agenda._draft.clear()
    # the local brain only says it's an event; what and when come from the sentence
    assert "catat" in brain._routed_tool("add_event", "ulang bimbingan besok jam 14", "", False)
    assert agenda._events[-1]["time"] == "14:00", agenda._events[-1]
    assert "Kapan" in brain._routed_tool("add_event", "catat ulang bimbingan skripsi", "", False)
    agenda._draft.clear()
    agenda._events[:] = []


def test_weekly_schedule():
    import agenda
    import jadwal_kuliah as jk
    agenda.STATE_FILE = Path(tempfile.mkdtemp()) / "agenda.json"
    agenda._sync_google = lambda: None
    agenda._events[:] = []
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    far = today + datetime.timedelta(days=3)
    lines = [f"1\t{tomorrow.isoweekday()}\t09:00\tKalkulus (Ruang B2, 3 SKS)\tcritical",
             f"2\t{tomorrow.isoweekday()}\t13:30\tPancasila (Ruang F24, 2 SKS)\tcritical",
             f"3\t{far.isoweekday()}\t10:00\tJaringan (Ruang E1)\tcritical",
             f"4\t{tomorrow.isoweekday()}\t02:46\tuji coba\tnormal",
             "rusak", "5\t9\t10:00\thari tidak ada", "6\t1\t25:00\tjam tidak ada"]
    _schedule_file("\n".join(lines) + "\n")
    try:
        items = jk.entries()
        assert [e["name"] for e in items if e["is_class"]] and len(items) == 4, items      # bad lines are left out
        first = next(e for e in items if e["name"] == "Kalkulus")
        assert (first["room"], first["detail"], first["is_class"]) == ("Ruang B2", "Ruang B2, 3 SKS", True), first
        assert not next(e for e in items if e["name"] == "uji coba")["is_class"]
        said = jk.answer("besok kuliah apa")
        assert said.startswith("Besok kuliah Kalkulus jam 09.00 di Ruang B2, lalu Pancasila jam 13.30"), said
        assert "Kalkulus" in jk.answer("besok kuliah jam berapa") and "uji coba" not in jk.answer("besok kuliah jam berapa")
        assert "Pancasila" in jk.answer("pancasila jam berapa") and "Ruang F24" in jk.answer("kapan kuliah pancasila")
        assert "tidak ada kuliah" in jk.answer("kuliah hari ini apa") or "lagi" in jk.answer("kuliah hari ini apa")
        assert "Jaringan" in jk.answer("jadwal kuliah") and "Kalkulus" in jk.answer("jadwal kuliah")
        assert "berikutnya" in jk.answer("kuliah berikutnya")
        for not_a_question in ("aku kuliah jurusan sistem informasi", "besok ada kuliah jam 2", "ingatkan kuliah besok", "kuliah",
                               "kenapa kuliah itu mahal", "buka firefox", "jadwal minggu ini"):
            assert jk.answer(not_a_question) is None, not_a_question
        # the same questions through what the commands really use, with no events at all
        assert "Besok kuliah Kalkulus" in agenda.handle("besok kuliah apa")
        said = agenda.handle("cek jadwal")
        assert "Kalkulus" in said and "Tidak ada acara lain" in said and said.count("Tuan") == 1, said
        assert "Kalkulus" in agenda.list_events(1, start=1) and "Kalkulus" not in agenda.list_events(keyword="rapat")
        assert "Kalkulus" not in agenda.briefing_line()                                  # the morning summary: today's only
        snap = agenda.snapshot()
        assert any(r.get("weekly") and r["title"] == "Kalkulus" for r in snap), snap
        assert agenda.page_data()["weekly"] and "events" in agenda.page_data()

        _schedule_file(f"11\t{today.isoweekday()}\t23:59\tStatistika (Ruang X)\tcritical\n")
        assert "Statistika" in agenda.briefing_line() and agenda.briefing_line().startswith("Hari ini kuliah")

        # the reminder: before it starts, once, not at night, not when it's turned off
        now = datetime.datetime.now()
        soon = now + datetime.timedelta(minutes=20)
        if not 6 <= soon.hour < 21 or soon.date() != now.date():
            print("  (lewati uji pengingat: di luar jam 06-21)")
            return
        night = "03:00"
        lines = [f"7\t{today.isoweekday()}\t{soon:%H:%M}\tKalkulus (Ruang B2)\tcritical", f"8\t{today.isoweekday()}\t{night}\tmalam\tcritical",
                 f"9\t{today.isoweekday()}\t{soon:%H:%M}\tpelan\tlow",
                 f"10\t{today.isoweekday()}\t{now + datetime.timedelta(minutes=90):%H:%M}\tNanti (Ruang A)\tcritical"]
        _schedule_file("\n".join(lines) + "\n")
        config.WEEKLY_REMIND_MINUTES = 30
        due = jk.due_reminders(set())
        assert len(due) == 1 and "menit lagi kuliah Kalkulus" in due[0][1] and "Ruang B2" in due[0][1], due
        assert jk.due_reminders({due[0][0]}) == []
        assert any("Kalkulus" in text for _, text, _, _ in agenda._due_now())           # through the agenda's own loop
        assert not any("Kalkulus" in text for _, text, _, _ in agenda._due_now())       # once
        config.WEEKLY_REMIND_MINUTES = 0
        assert jk.due_reminders(set()) == []
        config.WEEKLY_REMIND_MINUTES = 30
    finally:
        config.WEEKLY_SCHEDULE_FILE = NO_SCHEDULE
        agenda._notified.clear()
    assert jk.entries() == [] and "kosong" in jk.answer("besok kuliah apa")              # no file: said so (it became an event called kuliah)
    assert jk.answer("buka firefox") is None


def test_alarm_sounds():
    import alarm_sounds
    import settings
    real = (alarm_sounds.FOLDER, alarm_sounds._probe, config.ALARM_SOUND, config.ALARM_SOUND_FOR_ALL, settings.save)
    folder = Path(tempfile.mkdtemp()) / "sounds"
    alarm_sounds.FOLDER = folder
    alarm_sounds._probe = lambda path: (b"audio" in Path(path).read_bytes(), 12.0)
    settings.save = lambda values: config.__dict__.update(values)
    try:
        added = alarm_sounds.add("../../Lagu: bangun/pagi!.mp3", b"audio one")
        assert added["id"] == "pagi.mp3" and (folder / "pagi.mp3").is_file(), added          # never outside the folder
        assert alarm_sounds.add("pagi.mp3", b"audio one")["id"] == "pagi.mp3"                # the same file again
        assert alarm_sounds.add("pagi.mp3", b"audio two")["id"] == "pagi (2).mp3"
        assert [s["id"] for s in alarm_sounds.listing()] == ["pagi (2).mp3", "pagi.mp3"]
        for name, data, why in (("virus.sh", b"audio", "Format"), ("kosong.mp3", b"", "kosong"), ("palsu.mp3", b"bukan suara", "audio")):
            try:
                alarm_sounds.add(name, data)
                raise AssertionError(name)
            except ValueError as e:
                assert why in str(e), e
        assert not [p for p in folder.iterdir() if p.name.startswith(".")]                   # no half files left
        for outside in ("../pagi.mp3", "/etc/passwd", "", "pagi.exe"):
            assert not alarm_sounds.exists(outside), outside
        config.ALARM_SOUND = "pagi.mp3"
        config.ALARM_SOUND_FOR_ALL = False
        assert alarm_sounds.custom_file("alarm").endswith("pagi.mp3") and alarm_sounds.custom_file("timer") is None
        assert alarm_sounds.sound_name("alarm") == "pagi"
        config.ALARM_SOUND_FOR_ALL = True
        assert alarm_sounds.custom_file("timer").endswith("pagi.mp3")
        (folder / "pagi.mp3").rename(folder / "hilang.mp3")
        assert alarm_sounds.custom_file("alarm") is None                                     # gone: the built-in sound rings
        (folder / "hilang.mp3").rename(folder / "pagi.mp3")
        assert alarm_sounds.remove("pagi.mp3") and config.ALARM_SOUND == "" and not alarm_sounds.remove("pagi.mp3")
    finally:
        alarm_sounds.FOLDER, alarm_sounds._probe, config.ALARM_SOUND, config.ALARM_SOUND_FOR_ALL, settings.save = real
    real_save = settings.FIELD_MAP["ALARM_SOUND"]
    assert real_save[3] == "alarm_sound"
    try:
        settings._validate("ALARM_SOUND", "../../etc/passwd")
        raise AssertionError("traversal")
    except ValueError:
        pass
    assert settings._validate("ALARM_SOUND", "") == ""


def test_alarm_never_silent_and_rises():
    import threading
    import linux_desktop as ld
    played, volumes = [], []

    class Proc:
        def __init__(self, code): self.code = code
        def wait(self): return self.code
        def poll(self): return self.code
        def kill(self): pass
    real = (ld.play_audio, ld.get_volume, ld.set_volume, config.ALARM_RISE_SECONDS)
    ld.play_audio = lambda path: played.append(path) or Proc(1 if path == "rusak.mp3" else 0)
    ld.get_volume, ld.set_volume = (lambda: (60, False)), volumes.append
    try:
        sound = ld.LoopingSound("rusak.mp3", fallback="bawaan.oga")
        time.sleep(0.7)
        sound.stop()
        assert played[0] == "rusak.mp3" and "bawaan.oga" in played, played                   # a broken file: still heard
        config.ALARM_RISE_SECONDS = 2
        alarms._stop_ring.clear()
        thread = alarms._rise({"kind": "alarm"})
        assert thread and thread.full == 60 and volumes[0] == 24, volumes                    # starts soft
        thread.join(timeout=4)
        assert volumes[-1] == 60 and volumes == sorted(volumes), volumes                     # and reaches what it should
        volumes.clear()
        assert alarms._rise({"kind": "timer"}) is None and not volumes
        config.ALARM_RISE_SECONDS = 0
        assert alarms._rise({"kind": "alarm"}) is None
    finally:
        ld.play_audio, ld.get_volume, ld.set_volume, config.ALARM_RISE_SECONDS = real
        alarms._stop_ring.clear()
    assert threading.active_count() >= 1


def test_agenda_page_and_edits():
    import agenda
    import hud_server
    today = datetime.date.today()
    agenda.STATE_FILE = Path(tempfile.mkdtemp()) / "agenda.json"
    agenda._sync_google = lambda: None
    agenda._events[:] = []
    day = lambda n: (today + datetime.timedelta(days=n)).isoformat()
    agenda.add_event("Rapat tim", day(3), "09:00")
    agenda._events.append({"id": "lama", "title": "Kemarin", "date": day(-1), "time": "", "yearly": False, "remind_days": 1})
    rows = agenda.all_events()
    assert [r["title"] for r in rows] == ["Kemarin", "Rapat tim"] and rows[0]["past"] and not rows[1]["past"], rows
    assert rows[1]["days"] == 3 and rows[1]["time"] == "09:00"
    done, _ = agenda.update_event(rows[1]["id"], "Rapat besar", day(4), "10.30", False, 7)
    changed = agenda.all_events()[1]
    assert done and (changed["title"], changed["date"], changed["time"], changed["remind_days"]) == ("Rapat besar", day(4), "10:30", 7)
    for args, why in ((("x", "A", day(1)), "tidak ada"), ((changed["id"], "", day(1)), "kosong"), ((changed["id"], "A", "besok"), "Tanggal"),
                      ((changed["id"], "A", day(-3)), "lewat"), ((changed["id"], "A", day(1), "jam 9"), "Jam")):
        done, message = agenda.update_event(*args)
        assert not done and why in message, (args, message)
    opened, closed = [], []
    real = (hud_server.open_agenda, hud_server.close_agenda)
    hud_server.open_agenda = lambda: opened.append(1) or True
    hud_server.close_agenda = lambda: closed.append(1) or True
    try:
        for said in ("buka agenda", "Tampilkan jadwal saya", "lihat agenda", "buka jadwal"):
            assert "Agenda saya buka" in agenda.handle(said), said
        assert len(opened) == 4 and "Rapat besar" in agenda.handle("buka agenda")
        assert "tutup" in agenda.handle("tutup agenda") and closed
        assert "Ada" in agenda.handle("agenda minggu ini") and len(opened) == 5              # a question is still answered aloud
    finally:
        hud_server.open_agenda, hud_server.close_agenda = real
    change = hud_server._Handler._agenda_change
    added = change({"op": "add", "title": "Bayar", "date": day(2)})
    assert added["ok"] and not change({"op": "add", "title": "Bayar", "date": day(2)})["ok"]
    assert not change({"op": "add", "title": "Dulu", "date": day(-5)})["ok"]
    target = next(r for r in agenda.all_events() if r["title"] == "Bayar")["id"]
    assert change({"op": "delete", "id": target})["ok"] and not change({"op": "delete", "id": target})["ok"]
    assert not change({"op": "delete", "id": ""})["ok"]                                      # an empty id deletes nothing
    assert brain._route("tes alarm") == ("test_alarm", {}) and brain._route("ganti suara alarm") == ("sound_settings", {})
    agenda._events[:] = []


if __name__ == "__main__":
    ok = True
    for test in (test_adding_events_by_sentence, test_weekly_schedule, test_alarm_sounds, test_alarm_never_silent_and_rises, test_agenda_page_and_edits, test_timer_and_alarm, test_alarms_survive_a_restart, test_snooze_and_stop, test_memory,
                 test_memory_is_learned_by_asking, test_memory_and_favourite_routes,
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
