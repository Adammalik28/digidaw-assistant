"""
Tests for the fixes of 1-3 Nov 2026, from the real log after the "Jarvis" batch went live: Digidaw not hearing its
name right after a start (echo canceller, gate model, briefing), calls dropped after "digital", its own words coming
back through the phone, messages by voice ("jawab oke", "kirim pesan ke Bisnis"), misheard lamp commands, quieter
announcements during a film or a call. Offline: no network, no sound, no real lamp or WhatsApp.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_polish.py
"""
import datetime
import os
import re
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
agenda.SMALL_HOURS_UNTIL = 0                # (these run at any hour; what "besok" means after midnight: tests/test_real_use.py)
import daily          # noqa: E402
import settings       # noqa: E402
import tts            # noqa: E402

REAL_BUSY = presence.busy_reason
T = config.USER_TITLE


class _Done:
    def wait(self, timeout=None):
        return True


config.WEEKLY_SCHEDULE_FILE = config.BASE_DIR / "tests" / "tidak-ada-jadwal.tsv"     # (no real schedule in these tests)


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


def test_aec_removes_the_old_modules():
    """Earlier versions loaded PipeWire echo-cancel modules; a copy left behind is removed. Other people's modules stay."""
    lines = ("7\tmodule-echo-cancel\taec_method=webrtc source_name=digidaw_aec_source sink_name=x\t\n"
             "9\tmodule-echo-cancel\tsource_name=digidaw_bargein_source sink_name=y\t\n"
             "12\tmodule-echo-cancel\tsource_name=someone_else sink_name=z\t\n13\tmodule-null-sink\tsink_name=x\t")
    calls = []

    class Done:
        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake(args, **k):
        calls.append(args)
        return Done(lines if args[1] == "list" else "")
    real = aec.subprocess.run
    aec.subprocess.run = fake
    try:
        aec.unload_leftover()
    finally:
        aec.subprocess.run = real
    gone = [c[2] for c in calls if c[1] == "unload-module"]
    assert gone == ["7", "9"], gone


def test_call_google_did_not_hear():
    """The laptop heard "Digidaw" (chime), Google didn't: the next phrase still works if it sounds like a command, so
    the user doesn't have to call again. Noise and chatter don't, and the phrase itself isn't a command."""
    c, heard = _core()
    assert c.wake_hint("Digidaw")
    c.wake_unconfirmed()
    now = time.time()
    c.on_heard("buka", now - 2, now - 1)                                # the phrase Whisper took for the name
    assert not heard and time.time() < c.armed_until
    assert c._ting_undone
    c.on_heard("telusuri Doraemon", now + 1, now + 1.1)                 # a known command, not only a command word
    assert heard == ["telusuri Doraemon"] and not c._ting_undone, heard

    heard.clear()
    c.close_conversation()
    assert c.wake_hint("Digidaw")
    c.wake_unconfirmed()
    now = time.time()
    c.on_heard("tulang lunak itu", now + 1, now + 1.1)                  # a song, or a command badly heard: forgiven once
    assert not heard and time.time() < c.armed_until, heard
    c.on_heard("kok bisa begitu", now + 2, now + 2.1)                   # still not a command: the wait ends
    assert not heard and c.armed_until == 0.0, heard

    c.close_conversation()
    assert c.wake_hint("Digidaw")
    c.wake_unconfirmed()
    assert c.wake_hint("Digidaw")                                       # called again: answered again
    c.wake_unconfirmed()
    c.on_heard("gigi buka Firefox", time.time() + 1, time.time() + 1.1)  # the name misheard, then the command
    assert heard == ["buka Firefox"], heard


def test_what_the_microphone_really_wrote():
    """Sentences from the log of 4-5 Nov. Each of these was misunderstood, or took Gemini seconds, or did nothing."""
    import golden
    expected = {
        "hari apa ini": "get_date", "sekarang hari apa": "get_date", "tindakan discord ke workspace 3": "move_to_workspace",
        "indah workspace 3": "switch_workspace", "pindah ke space 2": "switch_workspace", "tour Van ke 5%": "fan_speed",
        "atur-atur friend ke 20%": "fan_speed", "Buka folder": "open_folder", "Buka folder download": "open_folder",
        "buka screenshot": "open_folder", "tutup folder": "close_application", "turunkan kecerahan ke 20%": "set_brightness",
        "tingkatkan kecerahan lampu": "lamp_brightness", "Jalan lampu 100%": "lamp_brightness", "balap warna merah": "lamp_color",
        "matikan daya": "shutdown_pc", "buka screen": "open_script_manager", "muka terminal": "open_application",
        "jadikan lagu ini alarm": "song_as_alarm", "jadikan Be My Baby sebagai alarm": "song_as_alarm",
    }
    wrong = {said: golden.decide(said) for said, tool in expected.items() if (golden.decide(said) or [None])[0] != tool}
    assert not wrong, wrong
    assert golden.decide("putar musik I was never that")[1]["query"] == "i was never that"      # a title
    assert golden.decide("putar musik santai")[1]["query"] == "musik santai"                    # a kind of music
    assert golden.decide("putar musik tradisional")[1]["query"] == "musik tradisional"
    assert golden.decide("jadikan Be My Baby sebagai alarm")[1]["title"] == "be my baby"
    assert golden.decide("pindah rumah") is None and golden.decide("pindah kerja") is None      # not a window


def test_a_call_heard_as_something_else():
    """Alone these are the name (it came out like this when the user called loudly); in a sentence they stay words."""
    import core as core_module
    detect = core_module.detect_wake
    for call in ("Dido Dido Dido", "gigi dong", "Gadgetaw.", "Hijau Daun", "bidang", "biji daun", "halo gigi dong"):
        assert detect(call) == (True, "", True), call
    for words in ("putar Hijau Daun", "gigi saya sakit", "hijau", "buka Firefox", "bidang apa itu"):
        assert not detect(words)[0], words
    import local_stt
    assert local_stt.mentions_wake("Dido Dido Dido") and local_stt.mentions_wake("gigi dong")   # these reach Google now


def test_workspace_without_a_number_is_asked():
    c, heard = _core()
    said = []
    real = core.tts.speak
    core.tts.speak = said.append
    try:
        for sentence in ("tindakan WhatsApp ke workspace", "pindah ke space", "pindah ke workspace"):
            c._pending_verb = None
            c._handle(sentence, "voice")
            assert c._pending_verb and not heard, (sentence, heard)
            assert said[-1].startswith("Workspace berapa"), said
        ran = []                                            # (the brain itself would really move the window)
        c._run_with_deadline = lambda text: (ran.append(text), ("ok", False))[1]
        c._pending_verb = ("tindakan whatsapp ke workspace", time.time())
        c._handle("3", "voice")
        assert ran == ["tindakan whatsapp ke workspace 3"], ran
    finally:
        core.tts.speak = real


def test_opening_what_is_already_open():
    """"buka Firefox" three times was three windows, "pindah ke WhatsApp" with WhatSie open "opened" it and nothing moved."""
    import system_control as sc
    import window_manager as wm
    windows = [{"hwnd": "0x1", "title": "WhatSie: WhatsApp", "pid": 11, "process": "whatsie", "class": "com.ktechpit.whatsie", "workspace": "1"},
               {"hwnd": "0x2", "title": "Mozilla Firefox", "pid": 12, "process": "firefox", "class": "firefox", "workspace": "2"},
               {"hwnd": "0x3", "title": "adam@adam", "pid": 13, "process": "kitty", "class": "kitty", "workspace": "2"},
               {"hwnd": "0x4", "title": "song", "pid": 14, "process": "chrome", "class": "DigidawPlayer", "workspace": "3"}]
    focused, launched = [], []
    real = (wm._windows, wm._force_foreground, sc._linux_launch, sc.bring_forward)
    wm._windows, wm._force_foreground = (lambda: list(windows)), focused.append
    sc._linux_launch, sc.bring_forward = (lambda program: launched.append(program) or program), (lambda *a, **k: None)
    try:
        assert "WhatsApp" in sc.open_application("whatsapp") and focused == ["0x1"] and not launched
        focused.clear()
        sc.open_application("firefox")
        assert focused == ["0x2"] and not launched
        focused.clear()
        sc.open_application("terminal")                     # another terminal is what's asked for
        assert not focused and launched, (focused, launched)
        launched.clear()
        assert wm.find_app("chrome") is None                # the music player isn't "Chrome"
        assert wm._find("terminal")["hwnd"] == "0x3"        # "pindah ke terminal" with kitty open
        assert wm._find("wasit loss") is None
        windows.append({"hwnd": "0x5", "title": "WasIstLos", "pid": 15, "process": "wasistlos", "class": "wasistlos", "workspace": "1"})
        assert wm._find("wasit loss")["hwnd"] == "0x5"      # as Google writes it
    finally:
        wm._windows, wm._force_foreground, sc._linux_launch, sc.bring_forward = real


def test_a_folder_wherever_it_is():
    import system_control as sc
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(f"{home}/Documents/kampus/Skripsi Akhir")
        os.makedirs(f"{home}/Documents/kampus/.tersembunyi/rahasia")
        os.makedirs(f"{home}/Laporan")
        os.makedirs(f"{home}/a/b/c/d/e/dalam")
        real = Path.home
        Path.home = classmethod(lambda cls: Path(home))
        try:
            assert sc._find_folder("skripsi").endswith("Skripsi Akhir")     # contains it
            assert sc._find_folder("laporan") == f"{home}/Laporan"          # the same name
            assert sc._find_folder("rahasia") is None                       # not in hidden folders
            assert sc._find_folder("dalam") is None                         # not forever deep
            assert sc._find_folder("ab") is None
        finally:
            Path.home = real
    assert sc._resolve_folder("sc") == str(config.SCRIPTS_DIR.expanduser())


def test_a_new_request_is_not_the_message():
    import brain
    import whatsapp
    sent = []
    real = whatsapp.reply_message
    whatsapp.reply_message = lambda text, to="": sent.append((to, text)) or "yakin?"
    try:
        whatsapp.stop_composing()
        assert "siapa" in brain._message_route("kirim pesan ke WhatsApp")         # an app isn't a person
        whatsapp.stop_composing()
        assert "Adam" in brain._message_route("kirim pesan ke Adam")
        assert "Bisnis" in brain._message_route("kirim pesan ke Bisnis") and not sent   # starts over, not the text of the first
        assert "Isi pesannya" in brain._message_route("ya") and not sent          # answering, not dictating
        brain._message_route("saya otw")
        assert sent == [("Bisnis", "saya otw")], sent
        whatsapp.compose("Adam")
        assert brain._message_route("buka terminal") is None and not whatsapp.composing()     # just that
    finally:
        whatsapp.reply_message = real
        whatsapp.stop_composing()


def test_routine_words_as_they_come_out():
    import protocols
    import routines
    ran = []
    real = (protocols.load, routines._run, routines._start)
    protocols.load = lambda: {"gabut": {"steps": []}, "jam terbang": {"steps": []}, "kerja": {"steps": []}, "santai": {"steps": []}}
    routines._run = lambda key: ran.append(key) or "jalan"
    routines._start = lambda rest: ran.append(("baru", rest)) or "ok"
    try:
        for said, key in (("mode gabut", "gabut"), ("model ga", "gabut"), ("mode gabot", "gabut"), ("modul jam terbang", "jam terbang")):
            ran.clear()
            assert routines.handle(said), said
            assert ran == [key], (said, ran)
        for said in ("buat rutini", "tambah rutini", "gambar rutinitas baru"):
            ran.clear()
            assert routines.handle(said) == "ok" and ran[0][0] == "baru", said
        for said in ("mode malam", "model lampu santai", "mode hemat"):
            ran.clear()
            assert routines.handle(said) is None and not ran, (said, ran)
    finally:
        protocols.load, routines._run, routines._start = real


def test_agenda_month_is_not_a_title():
    import agenda
    assert agenda._event_parts("tambahkan agenda di November")["title"] == ""
    assert agenda._event_parts("rapat tim besok jam 9")["title"].lower().startswith("rapat")


def test_a_site_by_its_name():
    import requests
    import site_finder
    real = requests.get

    class Answer:
        def __init__(self, location):
            self.headers = {"Location": location}
    try:
        requests.get = lambda *a, **k: Answer("https://www.google.com/url?q=https://lms.example.ac.id/")
        assert site_finder.first_result("lms contoh") == "https://lms.example.ac.id/"
        requests.get = lambda *a, **k: Answer("https://www.google.com/sorry/index")    # no result: not Google's own page
        assert site_finder.first_result("lms contoh") is None
        requests.get = lambda *a, **k: Answer("https://www.tiktok.com/@someone/video/7383131686160174341")
        assert site_finder.first_result("namasitus") and not site_finder._named_result("namasitus", "namasitus")   # some video
        requests.get = lambda *a, **k: Answer("https://namasitus.co.id/")
        assert site_finder._named_result("namasitus", "namasitus") == "https://namasitus.co.id/"
    finally:
        requests.get = real


def test_ai_check_kept_for_a_while():
    """Fourteen restarts in an evening shouldn't spend the daily quota of the free AI on checks."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py"), encoding="utf-8").read()
    assert "_ai_checked_lately()" in src and "AI_CHECK_HOURS" in src
    start = src.index("ARGS = _parse_args()")
    assert src.index("===== Digidaw start") > start          # a copy that only brings the HUD forward says nothing


def test_long_phrase_only_its_end_is_read():
    import speech_recognition as sr
    import stt
    audio = sr.AudioData(b"\x01\x00" * 16000 * 20, 16000, 2)
    tail = stt.SpeechToText._tail(audio, 20.0) if hasattr(stt, "SpeechToText") else None
    if tail is not None:
        assert len(tail.get_raw_data()) == int(stt.GATE_TAIL_SECONDS * 16000) * 2
        assert stt.SpeechToText._tail(audio, 5.0) is audio


def test_alarm_that_fell_during_a_restart_still_rings():
    import alarms
    now = time.time()
    saved = [{"id": "a", "kind": "timer", "label": "", "due": now - 20, "repeat": None, "time": "01:00"},        # during the restart
             {"id": "b", "kind": "alarm", "label": "", "due": now - 300, "repeat": None, "time": "07:00"},       # the PC came on late
             {"id": "c", "kind": "alarm", "label": "kuliah", "due": now - 7200, "repeat": None, "time": "05:00"},  # too long ago: told
             {"id": "d", "kind": "timer", "label": "", "due": now - 7200, "repeat": None, "time": "05:00"},      # nobody needs that one
             {"id": "e", "kind": "alarm", "label": "", "due": now - 86400 * 3, "repeat": list(range(7)), "time": "06:00"},   # days ago: next time
             {"id": "f", "kind": "alarm", "label": "", "due": now + 600, "repeat": None, "time": "09:00"}]
    told = []
    real = (alarms._load, alarms._save, alarms._loop, alarms._say, alarms.threading.Timer, list(alarms._items))

    class AtOnce:
        def __init__(self, seconds, fn, args=()):
            self.fn, self.args = fn, args

        def start(self):
            self.fn(*self.args)
    alarms._load, alarms._save, alarms._loop = (lambda: [dict(i) for i in saved]), (lambda: None), (lambda: None)
    alarms._say, alarms.threading.Timer = (lambda text, level="": told.append(text)), AtOnce
    alarms._items.clear()
    try:
        alarms.start()
        items = {i["id"]: i for i in alarms._items}
        assert set(items) == {"a", "b", "e", "f"}, items
        assert now < items["a"]["due"] < now + 60 and now < items["b"]["due"] < now + 60      # ring in a moment
        assert items["e"]["due"] > now + 60 and items["f"]["due"] == saved[-1]["due"]
        assert len(told) == 1 and "kuliah" in told[0] and "06" not in told[0], told           # (the one from days ago isn't "just missed")
    finally:
        alarms._load, alarms._save, alarms._loop, alarms._say, alarms.threading.Timer = real[:5]
        alarms._items[:] = real[5]


def test_the_phrase_that_chimed_is_not_the_command():
    """01:01:47: the laptop heard "Digidaw", it chimed, Google's first guess for that same phrase was "lagi", and that was answered as a chat."""
    c, heard = _core()
    now = time.time()
    c.arm()
    c._early_armed_at = now
    c.on_heard("lagi", now - 1.0, now - 0.2)
    assert not heard and c._awaiting_command, heard
    c.on_heard("buka firefox", now + 1, now + 2)
    assert heard == ["buka firefox"], heard


def test_timer_rings_with_the_chosen_sound():
    """Reported 5 Nov: a song became the alarm, the Control Panel showed it, the timer still rang the old sound."""
    import alarm_sounds
    import importlib
    import shutil
    import tempfile
    from pathlib import Path
    assert importlib.reload  # (config defaults are read below, without what this PC saved)
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.py"), encoding="utf-8").read()
    assert "ALARM_SOUND_FOR_ALL = True" in src
    folder = Path(tempfile.mkdtemp())
    (folder / "lagu.mp3").write_bytes(b"x")
    real = (alarm_sounds.FOLDER, config.ALARM_SOUND, config.ALARM_SOUND_FOR_ALL)
    alarm_sounds.FOLDER, config.ALARM_SOUND, config.ALARM_SOUND_FOR_ALL = folder, "lagu.mp3", True
    try:
        for kind in ("alarm", "timer", "reminder"):
            assert alarm_sounds.custom_file(kind) == str(folder / "lagu.mp3"), kind
        config.ALARM_SOUND_FOR_ALL = False                          # the switch still does what it says
        assert alarm_sounds.custom_file("alarm") and alarm_sounds.custom_file("timer") is None
    finally:
        alarm_sounds.FOLDER, config.ALARM_SOUND, config.ALARM_SOUND_FOR_ALL = real
        shutil.rmtree(folder, ignore_errors=True)


def test_song_found_however_it_plays():
    """Twice "belum ada lagu" while Arctic Monkeys played: the page wasn't readable at that moment."""
    import alarm_sounds as a
    import media
    real = (media._alive, media._player_target, media.snapshot, a._song_called, dict(media._last_video))
    media._alive = lambda: True
    media._player_target = lambda: {"url": "https://www.youtube.com/watch?v=abc&list=RDabc", "title": "(3) Arctic Monkeys - 505 (Official Video) - YouTube"}
    try:
        assert a._song_playing() == ("https://www.youtube.com/watch?v=abc", "Arctic Monkeys - 505")
        media._player_target = lambda: None                         # the page doesn't answer: what the desktop says is playing
        media.snapshot = lambda: {"title": "Do I Wanna Know", "artist": "Arctic Monkeys", "app": "Firefox", "playing": True}
        asked = []
        a._song_called = lambda title: asked.append(title) or ("https://www.youtube.com/watch?v=xyz", title)
        assert a._song_playing()[0].endswith("xyz") and asked == ["Arctic Monkeys Do I Wanna Know"], asked
        media.snapshot = lambda: {"title": "Pesan baru", "artist": "", "app": "WhatsApp", "playing": True}       # not a song
        media._last_video.update(id="last", title="Lantas", at=time.time())
        assert a._song_playing() == ("https://www.youtube.com/watch?v=last", "Lantas")             # what Digidaw itself started
        media._last_video.update(at=0.0)
        assert a._song_playing() is None
    finally:
        media._alive, media._player_target, media.snapshot, a._song_called = real[:4]
        media._last_video.update(real[4])
    assert a._clean_title("sound effect absurd cocok buat alarm🗿 part2 |#NAMA") == "sound effect absurd cocok buat alarm part2"
    assert a._clean_title("🗿") == "Lagu alarm"


def test_small_model_must_fit_the_sentence():
    """Its choice is forced, so nonsense gets a tool too. All of these really happened or came out in the exam."""
    import local_router as lr
    for said, tool in (("hapus teks", "close_tab"), ("maju 30 detik", "set_timer"), ("to", "lamp_color"), ("dingin", "lamp_scene"), ("dredown", "volume_down"),
                       ("mode gelap", "power_mode"), ("rekam layar", "take_screenshot"), ("simpan", "write_note"),
                       ("batal", "cancel_watch"), ("ulangi lagu ini", "play_favourite"), ("apa perintah terakhir", "list_timers")):
        assert not lr.believable(tool, said), (said, tool)
    for said, tool in (("tutup firefox", "close_application"), ("set timer 5 menit", "set_timer"), ("lampu warna merah", "lamp_color"), ("merah", "lamp_color"),
                       ("catat ide baru", "write_note"), ("atur power ke hemat", "power_mode"), ("mode performa", "power_mode"), ("full screen Firefox", "window_action"),
                       ("pindah ke discord", "focus_window"), ("putar lagu santai", "play_youtube"), ("berapa suhu", "get_temperature")):
        assert lr.believable(tool, said), (said, tool)
    assert lr.believable("fan_speed", "ke 20%", recent="fan_speed")       # a follow-up of what was just done
    assert not lr.believable("fan_speed", "ke 20%", recent="set_volume")


def test_everyday_requests_go_where_they_should():
    """Sentences anyone says to an assistant. Before: a guess by the small model, or seconds of the big one."""
    import golden
    expected = {
        "volume berapa": "get_volume", "musik berhenti": "media_control", "ulangi lagu ini": "media_control", "putar dari awal": "media_control",
        "mundur 10 detik": "media_control", "maju 30 detik": "media_control", "ini lagu siapa": "now_playing", "simpan lagu ini": "like_song",
        "putar lagu terakhir": "media_control", "stopwatch": "stopwatch", "hentikan stopwatch": "stopwatch", "catat beli susu": "write_note",
        "baca catatan": "read_last_note", "catatan saya apa saja": "list_notes", "buka catatan": "open_last_note", "hapus catatan terakhir": "delete_last_note",
        "tambahkan ke catatan beli gula": "append_to_last_note", "sisa penyimpanan": "disk_space", "ip saya berapa": "get_network_info", "wifi apa": "get_network_info",
        "tes kecepatan internet": "internet_speed", "internet lambat gak": "internet_speed", "bluetooth nyala gak": "bluetooth", "kecerahan berapa": "get_brightness",
        "naikkan kecerahan": "brightness_step", "layar lebih redup": "brightness_step", "mode gelap": "dark_mode", "kosongkan sampah": "empty_recycle_bin",
        "update sistem": "system_update", "cek update": "check_updates", "aplikasi apa yang berat": "heavy_apps", "kenapa laptop lambat": "heavy_apps",
        "tutup aplikasi yang berat": "close_heaviest", "berapa lama laptop menyala": "uptime", "tutup ini": "window_action", "maximize": "window_action",
        "jendela apa yang aktif": "active_window_info", "aplikasi apa yang terbuka": "list_windows", "rekam layar": "screen_record", "salin": "edit_action",
        "tempel": "edit_action", "undo": "edit_action", "simpan": "edit_action", "pilih semua": "edit_action", "hapus teks": "edit_action", "ctrl s": "press_keys",
        "baca clipboard": "get_clipboard", "kamu bisa apa": "what_i_can_do", "bantuan": "what_i_can_do", "hari apa besok": "get_date", "tanggal berapa besok": "get_date",
        "sekarang bulan apa": "get_date", "berapa hari lagi tanggal 10 desember": "days_until", "ingat nama kucing saya mochi": "remember",
        "tadi kamu bilang apa": "repeat_reply", "ulangi": "repeat_reply", "apa perintah terakhir": "last_command", "batal": "say", "buka firefox baru": "open_application",
    }
    wrong = {said: golden.decide(said) for said, tool in expected.items() if (golden.decide(said) or [None])[0] != tool}
    assert not wrong, wrong
    # and what they must not take over
    for said, tool in (("salin link ini", "copy_current_link"), ("simpan link ini", "save_current_link"), ("buka tab baru", "navigate"), ("tutup firefox", "close_application"),
                       ("matikan wifi", "wifi_power"), ("kipas 50 persen", "fan_speed"), ("lanjutkan musik", "media_control")):
        assert golden.decide(said)[0] == tool, (said, golden.decide(said))
    for said in ("ingat gak nama kucing saya", "berapa hari lagi lebaran", "wifi", "tambah lampu"):
        assert golden.decide(said) is None, (said, golden.decide(said))
    assert golden.decide("catat Beli Susu")[1]["content"] == "Beli Susu"     # keeps its capitals
    assert golden.decide("maju 2 menit")[1]["seconds"] == 120


def test_it_means_what_was_just_there():
    import golden
    import brain
    brain._recent_app.update(opened="firefox", closed="", at=time.time())
    assert golden.decide("tutup itu") == ("close_application", {"name": "firefox"})
    assert golden.decide("buka lagi") == ("open_application", {"name": "firefox"})
    brain._recent_app.update(closed="discord")
    assert golden.decide("buka lagi")[1]["name"] == "discord"
    assert golden.decide("pindahkan itu ke workspace 3")[1]["name"] == ""
    brain._recent_app.update(at=0.0)
    assert golden.decide("tutup itu")[0] == "window_action" and golden.decide("buka lagi") is None


def test_extending_a_timer():
    import alarms
    now = time.time()
    real = (alarms._save, list(alarms._items))
    alarms._save = lambda: None
    alarms._items[:] = [{"id": "a", "kind": "alarm", "label": "", "due": now + 10, "repeat": None, "time": "07:00"},
                        {"id": "b", "kind": "timer", "label": "", "due": now + 60, "repeat": None, "time": "01:00"}]
    try:
        assert "sisa 6 menit" in alarms.extend_timer(5)
        item = next(i for i in alarms._items if i["id"] == "b")
        assert now + 355 < item["due"] < now + 365                       # the timer, not the alarm that comes first
        alarms._items[:] = []
        assert "Tidak ada timer" in alarms.extend_timer(5)
    finally:
        alarms._save = real[0]
        alarms._items[:] = real[1]
    assert brain._alarm_route("tambah 5 menit") == ("extend_timer", {"minutes": 5})
    assert brain._alarm_route("tambah timernya 30 detik") == ("extend_timer", {"minutes": 0.5})
    assert brain._alarm_route("sisa timer berapa") == ("list_timers", {}) and brain._alarm_route("timer tinggal berapa") == ("list_timers", {})
    assert brain._alarm_route("batalkan timer")[0] == "cancel_timers"
    assert brain._alarm_route("set timer jam berapa") is None            # not a question about what's running


def test_agenda_questions_stay_questions():
    import jadwal_kuliah
    added = []
    listed = []
    real = (agenda.add_event, agenda.list_events)
    agenda.add_event = lambda *a, **k: added.append(a) or "ok"
    agenda.list_events = lambda days=30, start=0, keyword="": listed.append((days, start)) or "daftar"
    today = datetime.date.today().weekday()
    try:
        answer = agenda.handle("ada kuliah hari ini")                    # with an empty schedule this became an event called "kuliah"
        assert not added and "kosong" in answer, (added, answer)
        for said, day in (("besok ada apa", (1, 1)), ("hari ini ada apa", (0, 0)), ("ada apa besok", (1, 1)), ("lusa ngapain aja", (2, 2)),
                          ("minggu ini ada apa", (7, 0)), ("jadwal saya hari senin", ((0 - today) % 7, (0 - today) % 7))):
            listed.clear()
            assert agenda.handle(said) == "daftar", said
            assert listed == [day], (said, listed)
        agenda.handle("besok saya ada meeting jam 2")                     # telling: still added
        assert added and added[0][0] == "meeting", added
    finally:
        agenda.add_event, agenda.list_events = real
        agenda._draft.clear()
    assert jadwal_kuliah.answer("buka firefox") is None


def test_everyday_tools_read_the_machine():
    import everyday
    import system_control as sc
    ld = everyday.ld
    real = (ld.get_volume, ld.out, ld.active_window, sc.press_keys, sc.set_brightness)
    pressed, set_to = [], []
    try:
        ld.get_volume = lambda: (35, False)
        assert "35 persen" in everyday.get_volume()
        ld.get_volume = lambda: (35, True)
        assert "dibisukan" in everyday.get_volume()
        ld.out = lambda args, timeout=5: "intel_backlight,backlight,8256,43%,19200"
        assert "43 persen" in everyday.get_brightness()
        sc.set_brightness = lambda level: set_to.append(level) or "ok"
        everyday.brightness_step(15)
        everyday.brightness_step(-90)                                    # never down to a black screen
        assert set_to == [58, 5], set_to
        sc.press_keys = lambda keys: pressed.append(keys) or "ok"
        ld.active_window = lambda: {"class": "kitty"}
        everyday.edit_action("copy")
        everyday.edit_action("clear")
        assert pressed == ["ctrl+shift+c", "ctrl+u"], pressed            # ctrl+c would stop what's running
        assert "tidak ada di terminal" in everyday.edit_action("save")
        pressed.clear()
        ld.active_window = lambda: {"class": "firefox"}
        everyday.edit_action("copy")
        everyday.edit_action("clear")
        assert pressed == ["ctrl+c", "ctrl+a", "backspace"], pressed
    finally:
        ld.get_volume, ld.out, ld.active_window, sc.press_keys, sc.set_brightness = real
    everyday._watch.update(started=0.0, kept=0.0)
    assert "belum" in everyday.stopwatch("stop") and "mulai" in everyday.stopwatch("start")
    everyday._watch.update(started=time.time() - 125)
    assert "2 menit 5 detik" in everyday.stopwatch("read") and "berhenti di 2 menit 5 detik" in everyday.stopwatch("stop")
    everyday._watch.update(started=0.0, kept=0.0)
    assert "disk utama" in everyday.disk_space().lower() and " GB" in everyday.disk_space()
    assert "35 hari lagi" in everyday.days_until(f"tanggal {(datetime.date.today() + datetime.timedelta(days=35)).day} {['januari','februari','maret','april','mei','juni','juli','agustus','september','oktober','november','desember'][(datetime.date.today() + datetime.timedelta(days=35)).month - 1]}")


def test_fan_goes_back_to_automatic_when_hot():
    """Reported question 5 Nov: safe to leave it running with the fan set low? It doesn't follow the heat when set by hand."""
    import pc_control
    real = pc_control.ld.out
    try:
        pc_control.ld.out = lambda args, timeout=5: "Auto Control Enabled     : false"
        assert pc_control.fan_is_manual() is (bool(pc_control.shutil.which("nbfc")))
        pc_control.ld.out = lambda args, timeout=5: "Auto Control Enabled     : true"
        assert not pc_control.fan_is_manual()
    finally:
        pc_control.ld.out = real
    src = open(os.path.join(os.path.dirname(__file__), "..", "monitor.py"), encoding="utf-8").read()
    assert "FAN_SAFE_CELSIUS" in src and "fan_speed(auto=True)" in src


def test_name_spotter_never_acts_alone():
    """A phrase the detector hears the name in only goes on to Google; and until it has learned the user's voice it only watches."""
    import name_spotter as ns
    import numpy as np
    from pathlib import Path
    import tempfile
    spot = ns.spotter
    real = (spot.network, spot.personal, list(spot.scores))
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "m.npz"
        np.savez(path, threshold=np.array(0.8), w0=np.ones((1536, 2), dtype=np.float32) * 0.01, b0=np.zeros(2, dtype=np.float32),
                 w1=np.ones((2, 1), dtype=np.float32), b1=np.zeros(1, dtype=np.float32))
        net = ns._Network(path)
    assert net.threshold == 0.8 and net.score(np.ones((16, 96), dtype=np.float32)) > 0.99 and net.score(-np.ones((16, 96), dtype=np.float32)) == 0.5
    try:
        spot.network = net
        spot.personal = False
        assert not spot.sure(0.99)                                         # only watching: no effect at all
        assert stt.SpeechToText._with_spot("buka firefox", 0.99, 1.5) == "buka firefox"
        spot.personal = True
        assert spot.sure(0.9) and not spot.sure(0.7)
        with_spot = stt.SpeechToText._with_spot
        alias = config.WAKE_ALIASES[0]
        assert with_spot("", 0.9, 1.0) == alias                            # Google heard nothing: a call, the doubtful kind
        assert with_spot("kok bisa", 0.9, 1.0) == alias                    # two words of nothing
        assert with_spot("buka firefox", 0.9, 2.5) == f"{alias} buka firefox"       # the name misheard, the command right
        assert with_spot("saya tidak tahu kenapa dia pergi ke sana", 0.9, 4.0) == "saya tidak tahu kenapa dia pergi ke sana"    # talk stays talk
        assert with_spot("Digidaw buka firefox", 0.9, 2.5) == "Digidaw buka firefox"
        assert with_spot("buka firefox", 0.5, 2.5) == "buka firefox"       # not sure enough
        assert core.detect_wake(alias)[0] and core.detect_wake(alias)[2]   # loose: the next phrase has to look like a command
        spot.scores.clear()
        now = time.time()
        spot.scores.extend([(now - 3, 0.99), (now - 2.9, 0.1), (now - 1, 0.95), (now - 0.9, 0.92), (now + 5, 0.99), (now + 5.1, 0.99)])
        assert spot.score_during(now - 3.2, now - 2.8) == 0.1              # one click alone counts for nothing
        assert spot.score_during(now - 1.2, now - 0.8) == 0.92
        assert spot.score_during(now - 0.5, now) == 0.0
    finally:
        spot.network, spot.personal = real[0], real[1]
        spot.scores.clear()
        spot.scores.extend(real[2])
    assert brain._route("latih panggilan")[0] == "train_my_call" and brain._route("hapus latihan panggilan")[0] == "forget_my_call"
    missing = ns.Spotter()
    real_paths = (ns.PERSONAL_MODEL, ns.GENERAL_MODEL)
    ns.PERSONAL_MODEL, ns.GENERAL_MODEL = Path("/tidak/ada.npz"), Path("/tidak/ada.npz")
    try:
        assert missing.start() is False and missing.started is False       # no model: Digidaw simply works as before
        missing.feed(b"\0\0", now)
    finally:
        ns.PERSONAL_MODEL, ns.GENERAL_MODEL = real_paths


def test_lesson_saying_the_name():
    """The whole lesson, without a microphone or training: takes, then the result said, and hearing goes back to normal."""
    import alarms
    import json
    import name_spotter as ns
    import numpy as np
    import subprocess
    import tempfile
    from pathlib import Path
    said, recorded = [], []
    real = (ns._record, core.chime, tts.speak, tts.is_speaking, ns.MODEL_DIR, ns.TAKES_FILE, subprocess.run, ns.spotter.load, alarms.ANNOUNCE, ns.time.sleep)

    class Done:
        stdout, stderr = json.dumps({"held": 3, "of": 3, "wrong": 0, "others": 4, "threshold": 0.7}), ""
    with tempfile.TemporaryDirectory() as folder:
        ns.MODEL_DIR, ns.TAKES_FILE = Path(folder), Path(folder) / "my_voice.npz"
        ns._record = lambda seconds: recorded.append(seconds) or np.zeros(int(seconds * 16000), dtype=np.int16)
        core.chime, tts.speak, tts.is_speaking = (lambda up=True: None), said.append, (lambda: False)
        subprocess.run = lambda *a, **k: Done()
        ns.spotter.load = lambda: True
        alarms.ANNOUNCE = lambda text, level="": said.append(text)
        ns.time.sleep = lambda s: None
        try:
            ns._training["busy"] = True
            ns._lesson()
            assert len(recorded) == 1 + ns.CALL_TAKES + len(ns.OTHER_SENTENCES), recorded
            kept = np.load(ns.TAKES_FILE)
            assert sum(k.startswith("call") for k in kept.files) == ns.CALL_TAKES and "room" in kept.files
            assert "Dari 3" in said[-1] and "3 saya kenali" in said[-1], said[-1]
            assert not ns._training["busy"] and stt.stt_engine._recording is None
            ns._training["busy"] = True
            ns._record = lambda seconds: np.zeros(0, dtype=np.int16)    # the microphone gives nothing: said so, nothing saved
            ns.TAKES_FILE.unlink()
            ns._lesson()
            assert "terhenti" in said[-1] and not ns.TAKES_FILE.exists(), said[-1]
        finally:
            ns._record, core.chime, tts.speak, tts.is_speaking, ns.MODEL_DIR, ns.TAKES_FILE, subprocess.run, ns.spotter.load, alarms.ANNOUNCE, ns.time.sleep = real
            ns._training["busy"] = False


def test_a_process_that_never_read_the_file_cannot_replace_it():
    """5 Nov: the brain exam added one event from its own process, and the user's three were gone."""
    import json
    import tempfile
    from pathlib import Path
    folder = Path(tempfile.mkdtemp())
    real = (agenda.STATE_FILE, list(agenda._events), dict(agenda._loaded))
    agenda.STATE_FILE = folder / "agenda.json"
    agenda.STATE_FILE.write_text(json.dumps({"events": [{"id": "x", "title": "lama", "date": "2099-01-02", "time": "", "yearly": False, "remind_days": 1, "created": 1}], "notified": []}))
    agenda._events.clear()
    agenda._loaded["file"] = None
    try:
        agenda.add_event("rapat", "2099-01-03")
        saved = [e["title"] for e in json.loads(agenda.STATE_FILE.read_text())["events"]]
        assert sorted(saved) == ["lama", "rapat"], saved
        assert "dihapus" in agenda.delete_event("lama") and "dihapus" in agenda.delete_event("rapat")
        agenda.add_event("praktikum", (datetime.date.today() + datetime.timedelta(days=1)).isoformat())
        assert "dihapus" in agenda.delete_event("besok") and not agenda._events     # by its day
    finally:
        agenda.STATE_FILE = real[0]
        agenda._events[:] = real[1]
        agenda._loaded.update(real[2])


def test_small_model_is_taught_from_real_use():
    """Its weights can't be trained here: what really reached it is shown as examples. The exams themselves are tests/teach_eval.py and tests/talk_exam.py (they need Ollama)."""
    import local_router as lr
    import teaching
    assert teaching.similar("tutup semua tuner")[0] == ("tutup semua tuner", "close_application")
    assert teaching.similar("rawr")[0] == ("rawr", lr.ASK) and teaching.similar("zzzzzz qqqq") == []
    assert all("rawr" != s for s, _ in teaching.similar("rawr", leave_out="rawr"))
    assert all(teaching.part(s) == "ajar" for s, _ in teaching.similar("buka game dvd", only="ajar", limit=8))
    words = open(os.path.join(os.path.dirname(__file__), "private_words.txt"), encoding="utf-8").read().split("\n") if os.path.exists(os.path.join(os.path.dirname(__file__), "private_words.txt")) else []
    said = " ".join(s for s, _ in teaching.TAUGHT).lower()
    assert not [w for w in words if len(w.strip()) > 4 and re.search(rf"\b{re.escape(w.strip().lower())}\b", said)]      # this file goes into git
    # what is done with its choice
    assert lr.settle(lr.CHAT, "rawr") == lr.ASK and lr.settle(lr.CHAT, "selamat pagi") == lr.CHAT and lr.settle(lr.CHAT, "aku lagi sedih sekali") == lr.CHAT
    assert lr.settle("close_tab", "hapus teks") == lr.ASK                    # short and doesn't fit: asked
    assert lr.settle("close_tab", "tolong hapus teks ini ya") == lr.AI       # longer: the online AI reads it
    assert lr.settle("close_tab", "tolong hapus teks ini ya", online=False) == lr.ASK
    assert lr.settle("reply_message", "yang dari whatsapp") == lr.AI         # never starts a message on its own word
    assert lr.settle("close_application", "tutup firefox") == "close_application"


def test_small_talk_is_right_for_the_time_of_day():
    import everyday
    import golden
    import brain as b
    real = everyday._part_of_day
    try:
        everyday._part_of_day = lambda: "pagi"
        assert "Selamat pagi" in everyday.small_talk("part", "pagi")
        said = everyday.small_talk("part", "malam")
        assert "Selamat pagi" in said and "masih pagi" in said, said         # it was: "Waduh, sekarang masih pagi. Aduh, mungkin kamu salah tahu waktu."
        everyday._part_of_day = lambda: "malam"
        assert "sudah malam" in everyday.small_talk("part", "pagi")
    finally:
        everyday._part_of_day = real
    assert "Sama-sama" in everyday.small_talk("thanks").replace("Iya, sama-sama", "Sama-sama")
    for _ in range(20):                # (it picks at random)
        praise = everyday.small_talk("praise").lower()
        assert "terima kasih" in praise or "makasih" in praise, praise
    for said, kind in (("halo", "hello"), ("selamat pagi digidaw", "part"), ("apa kabar", "how"), ("wah keren", "praise"), ("kamu hebat banget", "praise")):
        assert golden.decide(said)[1]["kind"] == kind, (said, golden.decide(said))
    assert b.LEAKED_THINKING_RE.search("Kita lanjutkan percakapan dengan mengingat konteks waktu") and not b.LEAKED_THINKING_RE.search("Ibu kota Australia adalah Canberra.")
    assert b.MUSIC_TALK_RE.search("kamu suka musik apa") and not b.MUSIC_TALK_RE.search("aku capek")
    facts = ["saya alergi makanan manis"]
    assert b._facts_about("aku lapar", facts) == [] and b._facts_about("makanan apa yang aman", facts) == facts and b._facts_about("kamu kenal aku gak", facts) == facts


def test_rumble_is_not_speech():
    """A low rumble mustn't pass for speech: phrases never ended, and a call came out seconds late."""
    import array
    import math
    import stt

    def tone(hz, seconds=1.0, level=8000):
        return array.array("h", (int(level * math.sin(2 * math.pi * hz * i / 16000)) for i in range(int(16000 * seconds)))).tobytes()

    def rms(data):
        samples = array.array("h", data)
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    def filtered(hz):
        highpass, data = stt._HighPass(200, 16000), tone(hz)
        out = b"".join(highpass(data[i:i + 960]) for i in range(0, len(data), 960))   # in frames, like the microphone
        assert len(out) == len(data)
        return rms(out[3200:])                                                        # (after the filter settled)

    assert filtered(30) < 60                                                          # 8000 -> almost nothing
    assert filtered(50) < 300
    assert 5000 < filtered(1000) < 6200                                               # the voice stays
    assert 5000 < filtered(400) < 6200
    loud = stt._HighPass(200, 16000)(array.array("h", [32767] * 480).tobytes())       # no overflow on a full-scale frame
    assert len(loud) == 960


def test_sentence_begun_inside_the_wait():
    """The 8 s after the chime count from when the user starts talking, not from when the text comes back."""
    import core as core_module
    import stt
    c = core_module.DigidawCore.__new__(core_module.DigidawCore)
    now = time.time()
    c.armed_until, c._stretched_for = now - 0.1, None
    assert not c._in_wait() and c._in_wait(now - 1) and not c._in_wait(now + 1)
    engine = stt.stt_engine
    real = engine.phrase_in_progress_since
    try:
        engine.phrase_in_progress_since = now - 2                  # still talking, begun inside
        assert c._stretch_wait(now) and c.armed_until > now
        c.armed_until = now - 0.1                                  # the same sentence holds it for as long as it lasts
        assert c._stretch_wait(now)
        c.armed_until = now - 0.1
        engine.phrase_in_progress_since = now - 0.05               # a new sentence, begun after the end: too late
        assert not c._stretch_wait(now)
        engine.phrase_in_progress_since = None
        assert not c._stretch_wait(now)
    finally:
        engine.phrase_in_progress_since = real
    c.armed_until = 0.0                                            # Esc closed it: what was being said no longer counts
    assert not c._in_wait(now - 1)


def test_lamp_answer_is_a_lamp_word():
    import brain
    import core as core_module
    real = dict(brain._last_fast)
    brain._last_fast.update(tool="lamp_color", at=time.time())
    try:
        check = core_module.DigidawCore._lamp_answer
        assert check("merah") and check("Hijau.") and check("mati") and check("santai")
        assert not check("to") and not check("tolol")
        brain._last_fast.update(tool="open_application")
        assert not check("merah")
    finally:
        brain._last_fast.update(real)


def test_ignored_words_only_when_asked():
    import config
    import core as core_module
    said = core_module.DigidawCore._overheard
    real = config.LOG_OVERHEARD
    try:
        config.LOG_OVERHEARD = False
        assert said("halo apa kabar", 1.0, 2.5) == "3 kata"
        config.LOG_OVERHEARD = True
        assert said("halo apa kabar", 1.0, 2.5) == '"halo apa kabar" [1.5 dtk]'
    finally:
        config.LOG_OVERHEARD = real


def test_lone_word_after_an_answer():
    """After an answer the conversation stays open: a lone word in it was mostly the name misheard ("hijau" turned the lamp green)."""
    import brain
    c, heard = _core()
    brain._dialog.clear()
    brain._last_fast.update(tool="", at=0.0)
    c.open_conversation(10)
    now = time.time()
    for scrap in ("dredown", "kalau", "cover"):
        c.on_heard(scrap, now, now + 0.1)
    assert not heard and c.in_followup(), heard
    c.on_heard("firefox", now + 1, now + 1.1)                           # a known name still works
    assert heard == ["firefox"], heard
    heard.clear()
    c.open_conversation(10)
    c.on_heard("tutup firefox", time.time(), time.time() + 0.1)         # and of course a whole command
    assert heard == ["tutup firefox"], heard
    heard.clear()
    c.open_conversation(10)
    armed = c.armed_until
    c.on_heard("hijau daun", time.time(), time.time() + 0.1)            # the name misheard: a call, not a question to Gemini
    assert not heard and c.armed_until > armed, heard
    # ... unless Digidaw just asked something, or that word was what the lamp was asked to become
    c.close_conversation()
    c.open_conversation(10)
    brain._remember_turn("tambah agenda", "Acaranya apa dan kapan, Tuan?")
    c.on_heard("kuliah", time.time(), time.time() + 0.1)
    c.on_heard("dredown", time.time(), time.time() + 0.1)
    assert heard == ["kuliah", "dredown"], heard
    heard.clear()
    brain._dialog.clear()
    c.close_conversation()
    c.open_conversation(10)
    brain._last_fast.update(tool="lamp_color", at=time.time())
    c.on_heard("hijau", time.time(), time.time() + 0.1)
    assert heard == ["hijau"], heard
    brain._last_fast.update(tool="", at=0.0)


def test_finishing_a_routine_by_voice():
    """"sudah" ends a conversation everywhere, but while a routine is taught it finishes the routine."""
    import routines
    c, heard = _core()
    routines._teaching.update(name="kuliah", steps=["buka firefox"], at=time.time())
    try:
        c.open_conversation(25)
        c.on_heard("sudah", time.time(), time.time() + 0.1)
        assert heard == ["sudah"], heard
    finally:
        routines._teaching.clear()
    heard.clear()
    c.open_conversation(10)
    c.on_heard("sudah", time.time(), time.time() + 0.1)                 # no routine being taught: as always
    assert not heard, heard


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
    assert brain._route("lebih terang") == ("brightness_step", {"change": 15})     # no lamp just changed: the screen


def test_lamp_question():
    """"ganti lampu": Digidaw asks, and the answer finishes the command. "ubah lampu kamera" ("ke merah" misheard, the
    log shows the answer each time) doesn't need the question."""
    smarthome.has_lamps = lambda: True
    c, _ = _core()
    ran = []
    c._run_with_deadline = lambda text: (ran.append(text), ("ok", False))[1]
    c._handle("ubah lampu kamera", "hud")
    assert ran == ["ubah lampu kamera"] and not c._pending_lamp, ran
    ran.clear()
    c._handle("ubah lampu", "hud")
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
    for test in (test_small_model_is_taught_from_real_use, test_small_talk_is_right_for_the_time_of_day, test_a_process_that_never_read_the_file_cannot_replace_it, test_lesson_saying_the_name, test_name_spotter_never_acts_alone, test_timer_rings_with_the_chosen_sound, test_song_found_however_it_plays, test_small_model_must_fit_the_sentence, test_everyday_requests_go_where_they_should, test_it_means_what_was_just_there, test_extending_a_timer, test_agenda_questions_stay_questions, test_everyday_tools_read_the_machine, test_fan_goes_back_to_automatic_when_hot, test_the_phrase_that_chimed_is_not_the_command, test_alarm_that_fell_during_a_restart_still_rings, test_what_the_microphone_really_wrote, test_a_call_heard_as_something_else, test_workspace_without_a_number_is_asked, test_opening_what_is_already_open, test_a_folder_wherever_it_is, test_a_new_request_is_not_the_message, test_routine_words_as_they_come_out, test_agenda_month_is_not_a_title, test_a_site_by_its_name, test_ai_check_kept_for_a_while, test_long_phrase_only_its_end_is_read, test_rumble_is_not_speech, test_sentence_begun_inside_the_wait, test_lamp_answer_is_a_lamp_word, test_ignored_words_only_when_asked, test_finishing_a_routine_by_voice, test_lone_word_after_an_answer, test_call_google_did_not_hear, test_aec_removes_the_old_modules, test_alias_after_early_chime, test_alias_alone_then_request,
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
