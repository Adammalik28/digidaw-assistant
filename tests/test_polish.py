"""
Tests for the fixes of 1-3 Nov 2026, from the real log after the "Jarvis" batch went live: Digidaw not hearing its
name right after a start (echo canceller, gate model, briefing), calls dropped after "digital", its own words coming
back through the phone, messages by voice ("jawab oke", "kirim pesan ke Bisnis"), misheard lamp commands, quieter
announcements during a film or a call. Offline: no network, no sound, no real lamp or WhatsApp.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_polish.py
"""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config         # noqa: E402
import events         # noqa: E402
import presence       # noqa: E402
import notifications  # noqa: E402
import safety         # noqa: E402
import aec            # noqa: E402
import stt            # noqa: E402
import core           # noqa: E402
import brain          # noqa: E402
import whatsapp       # noqa: E402
import smarthome      # noqa: E402
import agenda         # noqa: E402
import daily          # noqa: E402
import settings       # noqa: E402
import tts            # noqa: E402

REAL_BUSY = presence.busy_reason
T = config.USER_TITLE


class _Done:
    def wait(self, timeout=None):
        return True


def _core():
    """A core that never makes a sound and records the commands it would run."""
    core.chime = lambda up=True: None
    core.media.duck = lambda: None
    core.tts.prewarm = lambda: None
    core.tts.speak = lambda text: None
    presence.busy_reason = lambda front=None: ""
    config.STREAM_REPLIES = False
    c = core.DigidawCore(voice_enabled=False)
    heard = []
    c.submit = lambda text, source: heard.append(text) or _Done()
    return c, heard


def test_aec_reads_pipewire():
    """PipeWire's pactl: modules have no index in JSON, and "client" is text where the clients' index is a number."""
    calls, state = [], {}
    state["modules"] = ('536870916\tmodule-echo-cancel\taec_method=webrtc aec_args="webrtc.noise_suppression=false" '
                        'source_name=digidaw_aec_source sink_name=digidaw_aec_sink\t\n12\tmodule-null-sink\tsink_name=x\t')
    lists = {"clients": [{"index": 7138, "properties": {"application.process.id": str(os.getpid())}},
                         {"index": 99, "properties": {"application.process.id": "1"}}],
             "sources": [{"index": 8295, "name": aec.SOURCE}], "sinks": [{"index": 8296, "name": aec.SINK}],
             "source-outputs": [{"index": 8284, "client": "7138", "source": 60}, {"index": 88, "client": "99", "source": 60}],
             "sink-inputs": [{"index": 8218, "client": "7138", "sink": 3782}]}

    def fake(*args, as_json=False, timeout=5):
        calls.append(args)
        if args[:2] == ("list", "short"):
            return state["modules"]
        if args[0] == "list":
            return lists[args[1]]
        return ""
    real = aec._pactl
    aec._pactl = fake
    try:
        assert aec._find_module() == "536870916"
        assert aec._move() == (True, True)
        assert ("move-source-output", "8284", aec.SOURCE) in calls and ("move-sink-input", "8218", aec.SINK) in calls
        assert not any(c[:2] == ("move-source-output", "88") for c in calls)      # another app's microphone stays
        state["modules"] = "7\tmodule-echo-cancel\taec_method=webrtc source_name=digidaw_aec_source sink_name=x\t"
        calls.clear()
        assert aec._current_module() is None and ("unload-module", "7") in calls     # the old noisy one goes
        calls.clear()
        aec.unload_leftover()                              # barge-in off: whatever is left is removed
        assert ("unload-module", "7") in calls
    finally:
        aec._pactl = real
    assert config.BARGE_IN is False or "BARGE_IN" in settings.load()         # off unless the user turned it on


def test_alias_after_early_chime():
    """The laptop heard "Digidaw" (chime), Google wrote "digital": the next phrase is the command, whatever it is."""
    c, heard = _core()
    assert c.wake_hint("Digidaw")
    now = time.time()
    c.on_heard("digital", now - 2, now - 1.5)
    c.on_heard("ada pesan masuk", now, now + 0.1)
    assert heard == ["ada pesan masuk"], heard


def test_alias_alone_then_request():
    c, heard = _core()
    now = time.time()
    c.on_heard("digital", now - 2, now - 1.5)             # only Google heard it, as "digital": a doubtful call
    c.on_heard("lampu mode santai", now, now + 0.1)       # …but this is clearly a request (used to be dropped)
    assert heard == ["lampu mode santai"], heard
    c.close_conversation()
    heard.clear()
    c.on_heard("Didi", now, now)                          # someone calls a person named Didi
    c.on_heard("sini makan dulu", now, now + 0.1)
    assert heard == [], heard


def test_google_alias_confirmed_by_laptop():
    assert stt.confirm_alias("Digidaw.", "digital ganti lampu ke hijau") == "Digidaw ganti lampu ke hijau"
    assert stt.confirm_alias("Digidaw, ganti lampu", "ganti lampu lagi dong") == "ganti lampu, Digidaw"
    assert stt.confirm_alias("digital", "digital ganti lampu") == "digital ganti lampu"   # the laptop didn't hear it
    assert stt.confirm_alias(None, "digital") == "digital"


def test_gate_hears_name_like_words():
    import local_stt
    assert local_stt.mentions_wake("Gigi dau, buka Firefox.") and local_stt.mentions_wake("Digidaw.")
    assert not local_stt.mentions_wake("Gak, gak, gak, gak, gak.") and not local_stt.mentions_wake("Saya yang faham.")
    assert not local_stt.mentions_wake("Jauh lebih ngomong.") and not local_stt.mentions_wake("tulang lunak")


def test_unfinished_phrase():
    assert stt.dangling("lebih") and stt.dangling("pindah ke") and stt.dangling("ubah lampu jadi")
    assert not stt.dangling("lampu lebih terang") and not stt.dangling("buka firefox")


def test_own_echo():
    """Digidaw's answer heard back (the phone read it aloud) is not a new command."""
    events.history.clear()
    events.emit("digidaw", text="Oke, lanjut lagi.")
    assert core.own_echo("Oke lanjut lagi")
    assert not core.own_echo("putar lagu Coldplay")
    events.history.clear()
    events.emit("digidaw", text="Lampu sekarang merah.")
    assert not core.own_echo("lampu merah")                         # two words: the user
    assert not core.own_echo("lampu kamar merah dong")              # not one run of its words
    assert core.own_echo("lampu sekarang merah")
    events.history[-1]["ts"] = time.time() - 60
    assert not core.own_echo("lampu sekarang merah")                # a minute later: someone saying it
    c, heard = _core()
    c.open_conversation(10)
    events.emit("digidaw", text="Oke, lanjut lagi.")
    c.on_heard("Oke lanjut lagi", time.time() - 1, time.time())
    assert heard == []


def test_busy_reason():
    real_call, real_dnd = presence.in_call, notifications._do_not_disturb
    presence.in_call, notifications._do_not_disturb = (lambda: ""), (lambda: False)
    try:
        assert REAL_BUSY({"fullscreen": 1, "title": "Firefox", "pid": 1}) == ""            # only maximized
        assert REAL_BUSY({"fullscreen": 2, "title": "YouTube — Firefox", "pid": 1}) == "layar penuh"
        assert REAL_BUSY({"fullscreen": 2, "title": "DIGIDAW HUD", "pid": 1}) == ""         # its own HUD
        presence.in_call = lambda: "Discord"
        assert REAL_BUSY({"fullscreen": 0, "title": "x", "pid": 1}) == "sedang panggilan di Discord"
    finally:
        presence.in_call, notifications._do_not_disturb = real_call, real_dnd


def test_announce_when_busy():
    c, _ = _core()
    c.voice_enabled = True
    spoken = []
    core.tts.speak = lambda text: spoken.append(text)
    presence.busy_reason = lambda front=None: "layar penuh"
    assert c.announce("Rangkuman hari ini.", "info") is False and not spoken
    assert c.announce("Hujan sebentar lagi.", "warning") is True and spoken == ["Hujan sebentar lagi."]
    presence.busy_reason = lambda front=None: ""
    assert c.announce(lambda: "", "message") is False                 # nothing left to say by then
    assert c.announce(lambda: "Ada pesan dari Rina.", "message") and spoken[-1] == "Ada pesan dari Rina."


def test_messages_named_once():
    """The welcome back named Budi Santoso: his next message isn't announced again right after it."""
    notifications._items.clear()
    notifications._told.clear()
    left = time.time() - 100
    notifications._items.append({"app": "WhatSie", "summary": "Budi Santoso", "body": "halo", "at": time.time() - 50,
                                 "read": False})
    notifications.mark_told(since=left)
    new = {"app": "WhatSie", "summary": "Budi Santoso", "body": "woi", "at": time.time(), "read": False}
    assert notifications.worth_telling([new]) == []
    assert notifications._clean("sari‎: lanjut‏") == "sari: lanjut"


def test_reading_prepared():
    prepared = []
    real = tts.prepare
    tts.prepare = lambda text, first_chunks=2: prepared.append(text)
    try:
        notifications._items.clear()
        notifications._items.append({"app": "WhatSie", "summary": "Bisnis", "body": "P", "at": time.time(), "read": False})
        notifications.prepare_reading("Bisnis")
        assert prepared == ["Dari Bisnis: P."], prepared
        assert not notifications._items[-1]["read"]                    # only worded: still unread
    finally:
        tts.prepare = real


def test_short_question():
    safety.guard('mengirim WhatsApp ke Rina: "oke"', lambda: "x", always=True)
    assert safety.question() == f'Yakin mengirim WhatsApp ke Rina: "oke", {T}?'
    safety.cancel()


def test_messages_by_voice():
    sent = []
    brain.TOOL_MAP["reply_message"] = lambda text, to="": sent.append((to, text)) or "ok"
    whatsapp.reply_message = lambda text, to="": sent.append((to, text)) or "ok"
    notifications._items.clear()
    notifications._last_chat.update(name="Bisnis", app="WhatSie", at=time.time())
    assert brain._message_route("jawab Oke") == "ok" and sent[-1] == ("", "Oke")
    assert brain._message_route("berikan jawaban oke") and sent[-1] == ("", "oke")
    assert brain._message_route("bilang ke Rina kalau saya telat") and sent[-1] == ("Rina", "saya telat")
    assert brain._message_route("Kirim pesan ke bisnis rar") and sent[-1] == ("Bisnis", "rar")
    notifications._last_chat.update(at=0)
    assert brain._message_route("jawab pertanyaan ini") is None        # no message just came in: for Digidaw
    question = brain._message_route("Kirim pesan Whatsapp")
    assert "ke siapa" in question, question
    question = brain._message_route("ke Bisnis")
    assert "isinya apa" in question and "Bisnis" in question, question
    brain._message_route("oke saya datang")
    assert sent[-1] == ("Bisnis", "oke saya datang")
    assert brain._message_route("kirim pesan ke Budi").startswith("Pesan untuk Budi")
    assert "tidak jadi" in brain._message_route("batal")
    assert not whatsapp.composing()


def test_misheard_routes():
    smarthome.has_lamps = lambda: True
    assert brain._route("jual lampu ke biru") == ("lamp_color", {"color": "biru", "name": "lampu"})
    assert brain._route("bawa lampu Keputih") == ("lamp_color", {"color": "putih", "name": "lampu"})
    assert brain._route("lampu santai") == ("lamp_scene", {"scene": "santai", "name": "lampu"})
    assert (brain._route("matikan lampu kamar tidur") or ("",))[0] != "lamp_scene"
    assert brain._route("kode pesan masuk")[0] == "read_notifications"
    assert brain._route("lagu back to friend") == ("play_youtube", {"query": "back to friend", "mix": False})
    assert brain._route("house music")[0] == "play_youtube"
    smarthome.changed_by_user_since = lambda t, name="": True           # the lamp was just changed
    assert brain._route("lebih terang") == ("lamp_brightness", {"percent": 30, "relative": True})
    smarthome.changed_by_user_since = lambda t, name="": False
    assert brain._route("lebih terang") is None


def test_lamp_question():
    """"ubah lampu kamera" (kamar merah misheard): Digidaw asks, and the answer finishes the command."""
    smarthome.has_lamps = lambda: True
    c, _ = _core()
    ran = []
    c._run_with_deadline = lambda text: (ran.append(text), ("ok", False))[1]
    c._handle("ubah lampu kamera", "hud")
    assert not ran and c._pending_lamp
    c._handle("merah", "hud")
    assert ran == ["lampu merah"], ran
    c._handle("ganti lampu", "hud")
    c._handle("matikan", "hud")
    assert ran[-1] == "matikan lampu", ran
    c._handle("lebih", "hud")                                           # cut off: "Lebih apa?"
    c._handle("terang", "hud")
    assert ran[-1] == "lebih terang", ran


def test_agenda_past_and_since():
    today = datetime.date.today()
    assert agenda._past(today, {"time": "00:00"}) is (datetime.datetime.now().time() > datetime.time(0, 0))
    assert not agenda._past(today + datetime.timedelta(days=1), {"time": "00:00"})
    assert not agenda._past(today, {})
    import tempfile
    from pathlib import Path
    daily.DIR = Path(tempfile.mkdtemp())
    with daily._lock:
        day = daily._current()
        day.update(active=3600, apps={"Firefox": 3600}, since=datetime.datetime.combine(today, datetime.time(18, 41)).timestamp())
    assert "sejak pukul 18.41" in daily.screen_time()


def test_alias_suggestions():
    assert settings.alias_sounds_like_name("digital") and settings.alias_sounds_like_name("di bidang")
    assert not any(settings.alias_sounds_like_name(w) for w in ("gak", "saya", "naikkan", "volume"))


def test_phone_quiet_for_pc_answer():
    """The PC's microphone already ran the sentence and said the answer: the phone shows it, doesn't say it."""
    import hud_server
    import mobile_server

    class Done:
        id = 1
        reply = "Oke, lanjut lagi."
        duplicate = "pc"

        def is_set(self):
            return True
    real = hud_server._handlers.get("command")
    hud_server._handlers["command"] = lambda text, source: Done()
    try:
        assert mobile_server._command("lanjutkan musik") == "Oke, lanjut lagi." and mobile_server._answered_on_pc()
        Done.duplicate = ""
        mobile_server._command("lanjutkan musik")
        assert not mobile_server._answered_on_pc()
    finally:
        hud_server._handlers["command"] = real


def test_close_menu_app():
    import system_control as sc
    import resolver
    real = resolver._static_candidates
    resolver._static_candidates = lambda: ([("kamera", "kamera", "Kamera", "guvcview")], [])
    try:
        assert sc._program_of("kamera") == "guvcview" and sc._program_of("Kamera") == "guvcview"
        assert sc._program_of("spotify") == ""
    finally:
        resolver._static_candidates = real


if __name__ == "__main__":
    ok = True
    for test in (test_aec_reads_pipewire, test_alias_after_early_chime, test_alias_alone_then_request,
                 test_google_alias_confirmed_by_laptop, test_gate_hears_name_like_words, test_unfinished_phrase, test_own_echo, test_busy_reason,
                 test_announce_when_busy, test_messages_named_once, test_reading_prepared, test_short_question,
                 test_messages_by_voice, test_misheard_routes, test_lamp_question, test_agenda_past_and_since,
                 test_alias_suggestions, test_phone_quiet_for_pc_answer, test_close_menu_app):
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
