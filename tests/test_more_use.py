"""What the audit of 6 Nov changed: prices and world time without an AI, messages that survive a restart, "oke" left unanswered, the lamp's question, rarely used sentences.
No sound, no network, nothing opened; files are temporary."""
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import brain
import core
import metrics
import notifications
import quick_facts as q
import safety
import screen_agent
import system_control
import tts
import window_manager_linux as wm


class Answer:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _sources(**urls):
    q._cache.clear()

    def get(url, timeout=None):
        assert timeout, "never without a time limit"
        for key, data in urls.items():
            if key.replace("_", "-") in url:
                if isinstance(data, Exception):
                    raise data
                return Answer(data)
        raise q.requests.ConnectionError("tidak terjangkau")

    q.requests.get = get


def test_prices_said_as_a_person_says_them():
    _sources(coingecko={"bitcoin": {"idr": 1371657839, "usd": 75617}}, er_api={"result": "success", "rates": {"IDR": 18080.0, "USD": 1, "JPY": 157.0}}, gold_api={"price": 4045.0})
    assert q.coin_price("BTC").startswith("Bitcoin sekarang sekitar 1,37 miliar rupiah, atau 75.617 dolar")
    assert "1,81 juta rupiah" in q.exchange_rate("dolar", 100)
    assert "18.080 rupiah" in q.exchange_rate("usd")
    assert "115 rupiah" in q.exchange_rate("yen")
    said = q.gold_price()
    assert "2,35 juta rupiah per gram" in said and "Antam" in said, said


def test_no_guessing_when_a_source_is_away():
    _sources()
    for said in (q.coin_price("bitcoin"), q.exchange_rate("euro"), q.gold_price()):
        assert "tidak bisa diambil" in said, said
    _sources(er_api={"result": "error"})
    assert "tidak bisa diambil" in q.exchange_rate("euro")


def test_prices_are_kept_a_little():
    calls = []
    _sources(coingecko={"bitcoin": {"idr": 1e9, "usd": 7e4}})
    real = q.requests.get
    q.requests.get = lambda url, timeout=None: calls.append(url) or real(url, timeout=timeout)
    q.coin_price("bitcoin")
    q.coin_price("bitcoin")
    assert len(calls) == 1, calls


def test_world_time():
    assert q.world_time("Tokyo").startswith("Di Tokyo sekarang pukul ") and "hari" in q.world_time("tokyo")
    assert "belum saya kenal" in q.world_time("planet mars")


def test_routes_of_prices_and_time():
    route = brain._route
    assert route("harga emas hari ini") == ("gold_price", {})
    assert route("harga bitcoin hari ini") == ("coin_price", {"coin": "bitcoin"})
    assert route("kurs dolar") == ("exchange_rate", {"currency": "dolar"})
    assert route("100 dolar berapa rupiah") == ("exchange_rate", {"currency": "dolar", "amount": 100})
    assert route("jam berapa di tokyo") == ("world_time", {"place": "tokyo"})
    assert route("jam berapa sekarang") == ("get_time", {}) or route("jam berapa sekarang")[0] == "get_time"
    assert route("harga diri") is None and route("20 orang") is None and route("jam berapa di kampus") is None


def test_rarely_said_sentences():
    route = brain._route
    expected = {"apa yang kamu bisa": "what_i_can_do", "aplikasi paling berat": "heavy_apps", "isi tempat sampah": "recycle_bin_status", "daftar rutinitas": "list_protocols",
                "hapus notifikasi": "clear_notifications", "buka hasil pertama": "open_first_result", "mulai dikte": "start_dictation", "sukai lagu ini": "like_song",
                "daftar pengawasan": "list_watches", "batalkan pengawasan": "cancel_watch", "update": "system_update", "update dong": "system_update"}
    for said, tool in expected.items():
        assert route(said)[0] == tool, said
    assert route("apakah discord dan telegram masih berjalan") == ("apps_open", {"names": "discord dan telegram"})
    assert route("apakah lampu masih nyala") is None or route("apakah lampu masih nyala")[0] != "apps_open"
    assert route("ubah lampu kamera") == route("ubah lampu ke merah") and route("ubah lampu kamera")[0] == "lamp_color"


def test_apps_open():
    import everyday
    kept = wm.find_app, system_control._matching_processes
    import window_manager
    keep2 = window_manager.find_app
    window_manager.find_app = lambda name: {"hwnd": "0x1"} if name == "discord" else None
    system_control._matching_processes = lambda name: []
    try:
        assert everyday.apps_open("discord dan telegram").startswith("Discord masih berjalan, Telegram tidak"), everyday.apps_open("discord dan telegram")
        assert everyday.apps_open("telegram").startswith("Telegram tidak berjalan")
        assert everyday.apps_open("discord").startswith("Discord masih berjalan")
    finally:
        window_manager.find_app, system_control._matching_processes = keep2, kept[1]


def test_messages_survive_a_restart():
    path = Path(tempfile.mkdtemp()) / "notifications.json"
    now = time.time()
    path.write_text(json.dumps([{"app": "whatsie", "summary": "Rina", "body": "halo", "at": now - 60, "read": False},
                                {"app": "whatsie", "summary": "Lama", "body": "kemarin", "at": now - 3 * 86400, "read": False},
                                {"bukan": "pesan"}]))
    notifications._items.clear()
    notifications._load_kept(path)
    try:
        assert [i["summary"] for i in notifications._items] == ["Rina"], "what's old or damaged stays out"
        assert "halo" in notifications.read_notifications(messages_only=True)
        notifications._keep_now()
        kept = json.loads(path.read_text())
        assert kept[0]["read"] is True and (path.stat().st_mode & 0o77) == 0, "read stays read, and the file is private"
    finally:
        notifications._kept_file = None
        notifications._items.clear()


def test_nothing_is_written_unless_started():
    if notifications._keep_timer:
        notifications._keep_timer.cancel()
        notifications._keep_timer.join(1)
    notifications._kept_file = None
    notifications._keep_soon()
    assert notifications._keep_timer is None or not notifications._keep_timer.is_alive()


def test_the_count_whatsapp_itself_shows():
    import window_manager
    keep = window_manager._windows
    window_manager._windows = lambda: [{"title": "WhatSie: (26) WhatsApp", "class": "com.ktechpit.whatsie", "process": "whatsie"}, {"title": "Tugas (3) - Firefox", "class": "firefox", "process": "firefox"}]
    notifications._items.clear()
    try:
        said = notifications.read_notifications(messages_only=True)
        assert "WhatsApp menandai 26 belum dibaca" in said and "Firefox" not in said, said
        window_manager._windows = lambda: [{"title": "WhatsApp", "class": "whatsie", "process": "whatsie"}]
        assert "Belum ada pesan yang masuk" in notifications.read_notifications(messages_only=True)
    finally:
        window_manager._windows = keep


def _heard(c, text):
    now = time.time()
    c.on_heard(text, now - 1, now - 0.5)


def test_oke_needs_no_answer():
    c = core.DigidawCore(voice_enabled=False)
    asked = []
    c.submit = lambda text, source="voice": asked.append(text) or type("D", (), {"wait": lambda self: None})()
    c.followup_until = time.time() + 8
    keep = core.brain._dialog[:] if hasattr(core.brain._dialog, "__getitem__") else list(core.brain._dialog)
    core.brain._dialog.clear()
    try:
        _heard(c, "oke")
        assert not asked and not c.in_followup(), "nothing made up, and the conversation ends"
        c.followup_until = time.time() + 8
        core.brain._dialog.append({"at": time.time(), "text": "x", "reply": "Mau saya putar lagunya?"})
        _heard(c, "oke")
        assert asked == ["oke"], "after a question it's an answer"
    finally:
        core.brain._dialog.clear()
        core.brain._dialog.extend(keep)


def test_the_lamps_question_goes_forward():
    c = core.DigidawCore(voice_enabled=False)
    said, kept = [], (tts.speak, core.smarthome_has if hasattr(core, "smarthome_has") else None)
    tts.speak = lambda text: said.append(text)
    c._pc_speaks = lambda source: True
    import smarthome
    keep_lamps = smarthome.has_lamps
    smarthome.has_lamps = lambda: True
    try:
        c._pending_lamp = time.time()
        c._handle("keterangan", "voice")
        assert "berapa persen" in said[-1] and c._pending_lamp, said
        c._pending_lamp = time.time()
        c._handle("warnanya", "voice")
        assert "Warna apa" in said[-1], said
        c._pending_lamp = time.time()
        c._handle("suasananya", "voice")
        assert "baca, santai" in said[-1], said
        c._pending_lamp = time.time()
        c._run_with_deadline = lambda text: ("ok", False)
        c._handle("lahan", "voice")
        assert "Belum saya tangkap" in said[-1] and not c._pending_lamp, "the same question doesn't come back"
    finally:
        tts.speak = kept[0]
        smarthome.has_lamps = keep_lamps


def test_running_apps_names_what_is_open():
    import everyday
    import window_manager
    keep = window_manager.closable_windows, window_manager._app_label, everyday.heavy_apps
    window_manager.closable_windows = lambda: [{"title": "x", "class": "kitty"}, {"title": "y", "class": "whatsie"}]
    window_manager._app_label = lambda w: w["class"].capitalize()
    everyday.heavy_apps = lambda: "Paling berat sekarang: WhatsApp."
    try:
        said = system_control.list_running_apps()
        assert said.startswith("Yang terbuka: Terminal, WhatsApp.") and "Paling berat" in said, said
    finally:
        window_manager.closable_windows, window_manager._app_label, everyday.heavy_apps = keep


def test_one_slow_source_doesnt_hold_the_search():
    import ai_skills
    kept = ai_skills._news_search, ai_skills._wikipedia, ai_skills._bing, ai_skills.SEARCH_ENOUGH_SECONDS
    ai_skills._news_search = lambda query: ["Berita A"]
    ai_skills._wikipedia = lambda query: None
    ai_skills._bing = lambda query: time.sleep(3) or ["lambat"]
    ai_skills.SEARCH_ENOUGH_SECONDS = 0.2
    try:
        started = time.time()
        said = ai_skills._search("tes", 12)
        assert time.time() - started < 1.5 and "Berita A" in said and "lambat" not in said, said
        ai_skills._news_search = lambda query: None
        ai_skills._bing = lambda query: time.sleep(0.5) or ["sampai juga"]
        assert "sampai juga" in ai_skills._search("tes", 12), "with nothing yet it waits on"
    finally:
        ai_skills._news_search, ai_skills._wikipedia, ai_skills._bing, ai_skills.SEARCH_ENOUGH_SECONDS = kept


def test_old_dispatchers_in_hyprlands_lua():
    """Hyprland 0.57: `hyprctl dispatch` reads Lua. Each line here was tried on a real window."""
    import linux_desktop as ld
    W = "address:0xabc"
    expected = [(("workspace", "3"), 'hl.dsp.focus({workspace="3"})'), (("workspace", "empty"), 'hl.dsp.focus({workspace="empty"})'),
                (("movetoworkspacesilent", f"8,{W}"), f'hl.dsp.window.move({{workspace="8", follow=false, window="{W}"}})'),
                (("movetoworkspace", f"8,{W}"), f'hl.dsp.window.move({{workspace="8", follow=true, window="{W}"}})'),
                (("focuswindow", W), f'hl.dsp.focus({{window="{W}"}})'), (("closewindow", W), f'hl.dsp.window.close({{window="{W}"}})'),
                (("fullscreenstate", "1 1"), "hl.dsp.window.fullscreen_state({internal=1, client=1})"),
                (("settiled", W), f'hl.dsp.window.float({{action="disable", window="{W}"}})'), (("togglefloating", W), f'hl.dsp.window.float({{action="toggle", window="{W}"}})'),
                (("movewindow", "l"), 'hl.dsp.window.move({direction="left"})'), (("swapwindow", "l"), 'hl.dsp.window.swap({direction="left"})'),
                (("resizewindowpixel", f"exact 700 400,{W}"), f'hl.dsp.window.resize({{x=700, y=400, window="{W}"}})'), (("dpms", "off"), 'hl.dsp.dpms({action="disable"})'), (("dpms", "on"), 'hl.dsp.dpms({action="enable"})'),
                (("exec", "[workspace 3 silent] firefox"), 'hl.dsp.exec_cmd("firefox", {workspace="3 silent"})'),
                (("exec", "[float;size 800 750;center] kitty --title \'a b\'"), 'hl.dsp.exec_cmd("kitty --title \'a b\'", {float=true, size="800 750", center=true})'),
                (("exec", "kitty"), 'hl.dsp.exec_cmd("kitty")'), (("focuscurrentorlast",), "hl.dsp.focus({last=true})"),
                (("sendshortcut", f"CTRL SHIFT, V, {W}"), f'hl.dsp.send_shortcut({{mods="CTRL SHIFT", key="V", window="{W}"}})'),
                (("sendshortcut", " , Return, activewindow"), 'hl.dsp.send_shortcut({mods="", key="Return"})'), (("movecursor", "10", "20"), "hl.dsp.cursor.move({x=10, y=20})")]
    for (name, *args), lua in expected:
        assert ld._as_lua(name, tuple(args)) == lua, (name, ld._as_lua(name, tuple(args)))
    assert ld._as_lua("tidakdikenal", ()) is None


def test_dispatch_prefers_lua_and_keeps_the_old_way():
    import linux_desktop as ld
    class Done:
        def __init__(self, out, code=0):
            self.stdout, self.returncode = out, code
    calls, kept = [], ld.run
    ld.run = lambda args, **k: calls.append(args) or Done("ok")
    try:
        assert ld.dispatch("workspace", "3") and calls == [["hyprctl", "dispatch", 'hl.dsp.focus({workspace="3"})']], calls
        calls.clear()
        ld.run = lambda args, **k: calls.append(args) or (Done("Invalid dispatcher") if len(calls) == 1 else Done("ok"))
        assert ld.dispatch("workspace", "3") and calls[-1] == ["hyprctl", "dispatch", "workspace", "3"], "Hyprland before 0.57"
        ld.run = lambda args, **k: Done("error: no", 7)
        assert not ld.dispatch("workspace", "3"), "a refusal is reported as one"
    finally:
        ld.run = kept


def test_hotkeys_through_lua():
    import hotkeys_linux as hk
    import linux_desktop as ld
    class Done:
        stdout, returncode = "ok", 0
    calls, kept = [], ld.run
    ld.run = lambda args, **k: calls.append(args) or Done
    try:
        assert hk._bind("ctrl+alt+d", "CTRL ALT", "D") and calls[0][:2] == ["hyprctl", "eval"] and len(calls) == 1, calls
        assert 'hl.unbind("CTRL + ALT + D")' in calls[0][2] and 'hl.bind("CTRL + ALT + D"' in calls[0][2] and "hotkeys_linux.py ctrl+alt+d" in calls[0][2], calls
    finally:
        ld.run = kept
    keep_out = ld.out
    ld.out = lambda args, **k: json.dumps([{"description": f"{hk.MARKER} ctrl+alt+d", "arg": "92"}, {"description": "lain", "arg": "5"}, {"arg": f"python {hk.MARKER} ctrl+alt+h"}])
    try:
        assert hk._ours_present() == {"ctrl+alt+d", "ctrl+alt+h"}
    finally:
        ld.out = keep_out


def test_a_temporary_copy_leaves_the_service_alone():
    import config
    import settings
    kept, written = config.BASE_DIR, []
    keep_path = settings.LINUX_SERVICE
    settings.LINUX_SERVICE = Path(tempfile.mkdtemp()) / "digidaw.service"
    try:
        config.BASE_DIR = Path("/tmp/salinan/digidaw")
        assert settings._temporary_copy() and settings.install_service() is False and not settings.LINUX_SERVICE.exists()
        config.BASE_DIR = Path("/home/tuan/Project/digidaw")
        assert not settings._temporary_copy()
    finally:
        config.BASE_DIR = kept
        settings.LINUX_SERVICE = keep_path


class Ran:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


@contextlib.contextmanager
def recorded_actions():
    """brain.fast_route with every action replaced by a note of it (the real ones would really run)."""
    calls = []
    real = dict(brain.TOOL_MAP), brain.alarms.handle, brain.agenda.handle
    for name in list(brain.TOOL_MAP):
        brain.TOOL_MAP[name] = (lambda n: lambda **kw: calls.append((n, kw)) or f"{n} selesai.")(name)
    brain.alarms.handle = lambda text: calls.append(("alarm", text)) or "Timer dipasang."
    brain.agenda.handle = lambda text: None
    safety.cancel()
    try:
        yield calls
    finally:
        brain.TOOL_MAP.clear()
        brain.TOOL_MAP.update(real[0])
        brain.alarms.handle, brain.agenda.handle = real[1:]
        safety.cancel()


FREEZE_CHECK = r"""
import faulthandler, os, sys, threading, time
faulthandler.dump_traceback_later(20, exit=True, file=open(os.devnull, "w"))     # from C: a stuck GIL stops all of Python
import pygame, tts
pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)
stop = threading.Event()
sound = bytes(2400)                                                              # 50 ms: it ends all the time, and then SDL wants the GIL


def ending():
    channel = pygame.mixer.find_channel(True)
    while not stop.is_set():
        channel.play(pygame.mixer.Sound(buffer=sound))
        time.sleep(0.01)


def hold():
    while not stop.is_set():
        tts._pause_music(); pygame.mixer.pause(); tts._resume_music(); pygame.mixer.unpause()
        time.sleep(0.0005)


for job in (ending, hold):
    threading.Thread(target=job, daemon=True).start()
time.sleep(3)
os._exit(0)
"""


def test_holding_the_voice_cannot_freeze_everything():
    # (6 Nov 16:26: pygame's own music.pause() held the GIL while waiting for the audio lock. Everything stood still for 110 s.)
    import subprocess
    done = subprocess.run([sys.executable, "-c", FREEZE_CHECK], env={**os.environ, "SDL_AUDIODRIVER": "dummy"},
                          capture_output=True, timeout=40)
    assert done.returncode == 0, "semuanya beku"
    assert tts._sdl_mixer(), "SDL_mixer tidak ditemukan: pygame.mixer.music.pause() yang bisa membekukan dipakai lagi"


def test_decoding_from_memory_gives_the_same_sound():
    import glob
    import io
    import pygame
    files = sorted(glob.glob("temp_audio/cache/*.mp3"))
    if not files:
        return
    if not pygame.mixer.get_init():
        pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)
    mp3 = Path(files[0]).read_bytes()
    assert tts._decoded(mp3) == pygame.mixer.Sound(file=io.BytesIO(mp3)).get_raw()
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        tts._decoded(mp3[:2000])
    assert len(os.listdir("/proc/self/fd")) == before
    try:
        tts._decoded(mp3[:32])                                                   # too little yet: the caller expects an error
    except pygame.error:
        pass


def test_the_offer_survives_being_looked_at():
    # (6 Nov 16:19: hearing.level used it up. "iya" was then left unanswered, and "bacakan" got "kurang jelas".)
    import hearing
    notifications.clear_offer()
    notifications.offer_reading("Rina")
    try:
        assert hearing._answer_awaited()                                         # "ya" stays "ya", whatever Google's next guess is
        hearing.level("iya")
        wanted = ("read_notifications", {"messages_only": True, "sender": "Rina"})
        assert brain._route("iya") == wanted and brain._route("bacakan") == wanted
        with recorded_actions() as calls:
            assert brain.fast_route("iya") and calls[0][0] == "read_notifications", calls
            assert brain._route("iya") is None                                   # read out: used up now
    finally:
        notifications.clear_offer()


def test_bacakan_later_reads_what_is_unread():
    real = notifications.unread_summary
    notifications.clear_offer()
    try:
        notifications.unread_summary = lambda since=0: "ada pesan WhatsApp dari Rina."
        assert brain._route("bacakan") == ("read_notifications", {"messages_only": True})
        assert brain._route("ya") is None                                        # a plain "ya" only answers the question
        notifications.unread_summary = lambda since=0: ""
        assert brain._route("bacakan") is None
    finally:
        notifications.unread_summary = real


def test_saying_no_to_the_offer_is_heard():
    c = core.DigidawCore(voice_enabled=False)
    said = []
    c._say_back = lambda text, source, reply, follow=True: said.append(reply)
    safety.cancel()
    notifications.offer_reading("Rina")
    try:
        assert c._question_open()                                                # a lone word can be the answer
        c._handle("tidak", "voice")
        assert len(said) == 1 and said[0].startswith("Baik") and notifications.offered() is None, said
        for no in ("nggak usah", "tidak usah dibacakan", "nanti saja", "jangan dulu"):
            assert core.OFFER_DECLINED_RE.fullmatch(no), no
        for other in ("tidak tahu", "nanti malam ke rumah", "jangan matikan lampu"):
            assert not core.OFFER_DECLINED_RE.fullmatch(other), other
    finally:
        notifications.clear_offer()


def test_a_misheard_sentence_gets_the_nearest_command_offered():
    for heard in ("pelatih panggilan", "padi panggilan"):
        assert brain._near_phrase(heard) == "latih panggilan", heard
    for other in ("tidak tahu", "iya", "posted 2", "lagu bagus", "tutup Vivo", "Apakah", "two player pro", "jelaskan", "bacakan", "hidup Rindu",
                  "Maki kandar ya", "udah full access", "coba lagi", "lagi dong"):
        assert brain._near_phrase(other) == "", other
    with recorded_actions() as calls:
        safety.guard("latih panggilan", brain._run_near, always=True, asking="Maksudnya latih panggilan?", phrase="latih panggilan")
        assert not calls                                                         # nothing runs before the "ya"
        safety.confirm()
        assert [c[0] for c in calls] == ["train_my_call"], calls


def test_closing_a_service_stops_the_service():
    import io
    real = system_control._matching_processes, system_control.ld.run, system_control.ld.out
    calls = []
    alive = {"yes": True}

    class Process:
        pid, info = 4242, {"name": "hermes", "pid": 4242}

        def is_running(self):
            return alive["yes"]

    def run(args, timeout=5, input=None):
        calls.append(args)
        alive["yes"] = False
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def show(args, timeout=5):
        return {"Restart": "always", "UnitFileState": "enabled"}[args[args.index("-p") + 1]]
    cgroups = iter(["0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service\n"])
    system_control.open = lambda path: io.StringIO(next(cgroups))
    system_control._matching_processes = lambda name: [Process()]
    system_control.ld.run, system_control.ld.out = run, show
    try:
        said = system_control.close_application("hermes")
        assert ["systemctl", "--user", "stop", "hermes-gateway.service"] in calls and "layanannya saya hentikan" in said and "login berikutnya" in said, said
        # not every program in a service: only one that systemd would bring straight back, and never Digidaw (it has what it opened)
        system_control.ld.out = lambda args, timeout=5: "on-failure"
        system_control.open = lambda path: io.StringIO("0::/user.slice/user-1000.slice/user@1000.service/app.slice/digidaw.service\n")
        assert system_control._service_that_comes_back([Process()]) == ""
        system_control.ld.out = show
        system_control.open = lambda path: io.StringIO("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-Hyprland-kitty-1.scope\n")
        assert system_control._service_that_comes_back([Process()]) == ""
    finally:
        del system_control.open
        system_control._matching_processes, system_control.ld.run, system_control.ld.out = real


def test_a_name_that_is_no_app_can_be_a_site_the_user_visits():
    import site_finder
    real = site_finder._from_history
    history = {"komiku": "https://komiku.org/", "bank": "https://bankmandiri.co.id/", "kampusku": "https://lms.kampusku.ac.id/"}
    site_finder._from_history = lambda key: history.get(key)
    try:
        assert site_finder.visited("komiku") == "https://komiku.org/"
        assert site_finder.visited("Kampusku") == "https://lms.kampusku.ac.id/"
        assert site_finder.visited("bank") is None                               # only a part of the name: not this site
        assert site_finder.visited("abc") is None and site_finder.visited("tidak ada") is None
    finally:
        site_finder._from_history = real


def test_a_long_command_output_is_shown_not_read_out():
    table = "\n".join(f"tcp LISTEN 0 4096 127.0.0.1:{p} 0.0.0.0:*" for p in range(9000, 9015))
    said = core._for_speech(f"Perintah berhasil. Output: {table}")
    assert said.startswith("Perintah berhasil") and "15 baris" in said and "LISTEN" not in said, said
    assert core._for_speech("Perintah berhasil. Output: 12 GB") == "Perintah berhasil. Output: 12 GB"
    assert core._for_speech("Lampu dimatikan, Tuan.") == "Lampu dimatikan, Tuan."
    assert system_control._read_only_command("ss -tulpn")                        # only looks: no "yakin?" first


def test_moving_the_music_says_music():
    real = wm._window_by_word, wm.ld.move_to_workspace
    window = {"hwnd": "1", "title": "lagu", "process": "chrome", "class": "chrome", "pid": 1, "workspace": "1"}
    wm._window_by_word = lambda name: window
    wm.ld.move_to_workspace = lambda hwnd, target, follow=False: True
    try:
        assert wm.move_to_workspace("3", "musik").startswith("Musik dipindah")
    finally:
        wm._window_by_word, wm.ld.move_to_workspace = real


@contextlib.contextmanager
def fresh_messages():
    real = list(notifications._items), dict(notifications._last_chat), dict(notifications._told)
    notifications._items.clear()
    notifications._last_chat.update(name="", app="", at=0.0)
    notifications._told.clear()
    try:
        yield
    finally:
        notifications._items.clear()
        notifications._items.extend(real[0])
        notifications._last_chat.update(real[1])
        notifications._told.clear()
        notifications._told.update(real[2])


def test_the_two_whatsapps_are_told_apart():
    import persona
    import resolver
    import window_manager_linux as w
    whatsie = {"hwnd": "1", "title": "(3) WhatsApp", "process": "whatsie", "class": "com.ktechpit.whatsie", "pid": 1, "workspace": "1"}
    business = {"hwnd": "2", "title": "WasIstLos", "process": "wasistlos", "class": "wasistlos", "pid": 2, "workspace": "1"}
    for said in ("whatsapp bisnis", "wa bisnis", "whatsapp business", "wasit lost", "wasis lord", "wasit", "wasistlos"):
        assert w.is_app_window(business, said) and not w.is_app_window(whatsie, said), said
        assert resolver.SHORT.get(said, said) == "wasistlos", said
    for said in ("whatsapp", "wa"):
        assert w.is_app_window(whatsie, said) and not w.is_app_window(business, said), said
    assert persona.brand("WasIstLos") == "WhatsApp Bisnis" and persona.brand("whatsapp bisnis") == "WhatsApp Bisnis" and persona.brand("whatsie") == "WhatsApp"
    assert notifications._label("wasistlos") == "WhatsApp Bisnis" and notifications._label("whatsapp bisnis") == "WhatsApp Bisnis"
    assert notifications._label("WhatSie") == "WhatsApp" and notifications._label("whatsapp") == "WhatsApp"
    assert system_control._match_key("whatsapp bisnis", system_control.APPS) == "whatsapp bisnis"
    assert system_control.APPS["whatsapp bisnis"] == ["wasistlos"]
    for asked in ("wasistlos", "whatsapp bisnis", ""):
        assert w._spoken_name(business, asked) == "WhatsApp Bisnis", asked       # never "WasIstLos di WhatsApp Bisnis"


def test_business_messages_are_announced_and_read():
    with fresh_messages():
        notifications._add("wasistlos", "Rina", "halo dari toko")
        notifications._add("WhatSie", "Budi", "halo dari rumah")
        business = notifications._items[0]
        assert notifications._is_chat("wasistlos")
        assert "ada pesan WhatsApp Bisnis dari Rina" in notifications.announcement([business])
        said = notifications.read_notifications(app="whatsapp bisnis", messages_only=True)
        assert "Rina" in said and "halo dari toko" in said and "Budi" not in said and "WhatsApp Bisnis" in said, said
        assert not notifications._items[1]["read"]                               # the other one is still unread
        both = notifications.read_notifications(app="whatsapp", include_read=True, messages_only=True)
        assert "Rina" in both and "Budi" in both, both
        assert notifications.worth_telling([dict(business, read=False)], {"class": "wasistlos", "process": "wasistlos", "title": "WasIstLos"}) == []     # it's in front
        assert notifications.worth_telling([dict(business, read=False)], {"class": "com.ktechpit.whatsie", "process": "whatsie", "title": "WhatsApp"})   # the other one is
        assert notifications.app_of_chat("rina") == "wasistlos" and notifications.app_of_chat("budi") == "WhatSie" and notifications.app_of_chat("adi") == ""


def test_a_reply_goes_back_the_way_the_message_came():
    import whatsapp
    sent = []
    real = whatsapp._send, whatsapp.shutil.which
    whatsapp._send = lambda name, text, account="": sent.append((name, text, account)) or f"Terkirim ke {name}."
    whatsapp.shutil.which = lambda name: f"/usr/bin/{name}"
    safety.cancel()
    try:
        with fresh_messages():
            notifications._add("wasistlos", "Rina", "ada stok?")
            notifications.note_chat(notifications._items[-1])
            whatsapp.reply_message("ada kak")                                    # to the chat just heard about: its app
            assert "WhatsApp Bisnis ke Rina" in safety.question() and not sent, safety.question()
            safety.confirm()
            assert sent == [("Rina", "ada kak", "bisnis")], sent
            sent.clear()
            whatsapp.reply_message("oke", "Rina")                                # by name: where the name's last message came in
            safety.confirm()
            assert sent == [("Rina", "oke", "bisnis")], sent
            sent.clear()
            notifications._add("WhatSie", "Budi", "halo")
            notifications.note_chat(notifications._items[-1])
            whatsapp.reply_message("halo juga")
            assert "WhatsApp ke Budi" in safety.question() and "Bisnis" not in safety.question()
            safety.confirm()
            assert sent == [("Budi", "halo juga", "")], sent
            sent.clear()
            whatsapp.reply_message("halo", "Adi", account="bisnis")              # said outright
            safety.confirm()
            assert sent == [("Adi", "halo", "bisnis")], sent
            sent.clear()
            whatsapp.reply_message("halo", "Adi")                                # nothing known about Adi: the personal one
            safety.confirm()
            assert sent == [("Adi", "halo", "")], sent
    finally:
        whatsapp._send, whatsapp.shutil.which = real
        safety.cancel()


def test_saying_which_whatsapp_in_a_sentence():
    import whatsapp
    for said, cleaned in (("kirim whatsapp bisnis ke Rina isinya oke", "kirim whatsapp ke Rina isinya oke"),
                          ("kirim pesan whatsapp bisnis ke Rina: oke", "kirim pesan whatsapp ke Rina: oke"),
                          ("kirim pesan lewat wa bisnis ke Rina: oke", "kirim pesan ke Rina: oke"),
                          ("kirim pesan ke Rina lewat whatsapp bisnis: oke", "kirim pesan ke Rina: oke"),
                          ("balas lewat whatsapp bisnis oke saya datang", "balas oke saya datang"),
                          ("balas whatsapp bisnis oke", "balas oke"),
                          ("kirim pesan ke whatsapp bisnis", "kirim pesan"),
                          ("bilang ke Rina lewat wa bisnis kalau saya telat", "bilang ke Rina kalau saya telat")):
        assert brain._message_account(said) == (whatsapp.BUSINESS, cleaned), said
    # not about the app: a chat called Bisnis, the message's own words, another command
    for said in ("kirim pesan ke bisnis", "kirim pesan whatsapp ke bisnis isinya halo", "kirim pesan ke Rina isinya whatsapp bisnis saya aktif",
                 "kirim pesan ke Rina: whatsapp bisnis aktif", "buka whatsapp bisnis", "catat whatsapp bisnis aktif"):
        assert brain._message_account(said) == ("", said), said
    with recorded_actions() as calls:
        whatsapp.stop_composing()
        try:
            assert "WhatsApp Bisnis ke siapa" in brain.fast_route("kirim whatsapp bisnis")
            assert "Rina, isinya apa" in brain.fast_route("Rina")
            real = whatsapp.reply_message
            whatsapp.reply_message = lambda text, to="", account="": calls.append(("kirim", text, to, account)) or "yakin?"
            try:
                brain.fast_route("oke saya datang")
            finally:
                whatsapp.reply_message = real
            assert calls[-1] == ("kirim", "oke saya datang", "Rina", "bisnis"), calls
        finally:
            whatsapp.stop_composing()


class FakeInspector:
    """The socket of WebKit's inspector: answers through the target, with other events in between."""

    def __init__(self, answers):
        self.answers, self.sent, self.queue = list(answers), [], []

    def send(self, text):
        message = json.loads(text)
        inner = json.loads(message["params"]["message"])
        self.sent.append((message["params"]["targetId"], inner))
        self.queue.append({"method": "Target.didCommitProvisionalTarget", "params": {"oldTargetId": "page-1", "newTargetId": "page-2"}})
        self.queue.append({"method": "Target.dispatchMessageFromTarget", "params": {"targetId": "page-2", "message": json.dumps({"id": inner["id"] + 100})}})
        self.queue.append({"method": "Target.dispatchMessageFromTarget", "params": {"targetId": "page-2", "message": json.dumps(
            {"id": inner["id"], "result": self.answers.pop(0)})}})

    def recv(self, timeout=None):
        return json.dumps(self.queue.pop(0))

    def close(self):
        pass


def test_the_webkit_inspector_is_spoken_to_through_its_target():
    import itertools
    import whatsapp
    page = whatsapp._WebKitPage.__new__(whatsapp._WebKitPage)
    page._ws, page._ids, page._page = FakeInspector([{"result": {"type": "number", "value": 3}, "wasThrown": False},
                                                     {"result": {"type": "object"}, "wasThrown": True}]), itertools.count(1), "page-1"
    assert page.js("1+2") == 3
    assert page._ws.sent[0][0] == "page-1" and page._ws.sent[0][1]["method"] == "Runtime.evaluate"
    assert page._page == "page-2"                                                # the page swapped its target while navigating
    assert page.js("throw 1") is None and page._ws.sent[1][0] == "page-2"
    # a page listed after WhatsApp's service workers
    real = whatsapp.requests.get
    html = '<a href="/socket/2/3/ServiceWorker"></a><a href="/socket/2/1/ServiceWorker"></a><a href="/socket/1/1/WebPage"></a>'
    whatsapp.requests.get = lambda url, timeout=3: type("Answer", (), {"text": html})()
    import websockets.sync.client as client
    real_connect, opened = client.connect, []

    class Hello:
        def recv(self, timeout=None):
            return json.dumps({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "page-9", "type": "page"}}})
    client.connect = lambda url, **kw: opened.append(url) or Hello()
    try:
        page = whatsapp._WebKitPage(9999)
        assert opened == ["ws://127.0.0.1:9999/socket/1/1/WebPage"] and page._page == "page-9", opened
    finally:
        whatsapp.requests.get, client.connect = real, real_connect


def test_sending_through_the_business_app_checks_like_the_other():
    import whatsapp

    class Page(whatsapp._WebKitPage):
        def __init__(self, header, after=None):
            self.header, self.after, self.compose, self.pressed, self.closed = header, after, "", 0, False

        def js(self, expression, timeout=10.0):
            if expression.lstrip().startswith("((want)"):
                return {"x": 1, "y": 2, "title": "Rina Kusuma"}
            return {whatsapp.JS_READY: True, whatsapp.JS_HEADER: self.header, whatsapp.JS_FOCUS_COMPOSE: True, whatsapp.JS_COMPOSE_TEXT: self.compose}.get(expression)

        def click(self, x, y):
            pass

        def clear_field(self):
            pass

        def insert(self, text):
            self.compose = self.after if self.after is not None else text

        def enter(self):
            self.pressed += 1
            self.compose = ""

        def close(self):
            self.closed = True
    real = whatsapp._connect, whatsapp.time.sleep, tts.prepare
    whatsapp.time.sleep = lambda s: None
    tts.prepare = lambda *a, **k: None
    try:
        page = Page("Rina Kusuma")
        whatsapp._connect = lambda account="": (page, "") if account == "bisnis" else (None, "akun salah")
        said = whatsapp._send("rina", "oke saya datang", "bisnis")
        assert said.startswith("Terkirim ke Rina Kusuma di WhatsApp Bisnis") and page.pressed == 1 and page.closed, said
        page = Page("Budi")                                                      # another chat opened: nothing typed, nothing sent
        assert "tidak saya temukan di WhatsApp Bisnis" in whatsapp._send("rina", "oke", "bisnis") and page.pressed == 0 and not page.compose
        page = Page("Rina Kusuma", after="ok")                                   # the text didn't arrive whole
        assert "tidak masuk dengan benar" in whatsapp._send("rina", "oke saya datang", "bisnis") and page.pressed == 0
    finally:
        whatsapp._connect, whatsapp.time.sleep, tts.prepare = real


def test_enter_is_only_pressed_in_the_message_box():
    # (6 Nov: a real Enter in the empty search box opened the first chat)
    import linux_desktop as ld
    import whatsapp
    import window_manager
    pressed, clicked = [], []
    state = {"focused": False, "text": "oke"}

    class Page(whatsapp._WebKitPage):
        def __init__(self):
            pass

        def js(self, expression, timeout=10.0):
            if expression == whatsapp.JS_SEND_BUTTON:
                clicked.append(1)
            return {whatsapp.JS_FOCUS_COMPOSE: True, whatsapp.JS_COMPOSE_FOCUSED: state["focused"], whatsapp.JS_COMPOSE_TEXT: state["text"]}.get(expression)
    real = ld.send_keys, window_manager.find_app, whatsapp.time.sleep
    ld.send_keys = lambda keys, address="": pressed.append((keys, address)) or state.update(text="") or True
    window_manager.find_app = lambda name: {"hwnd": "0xabc"}
    whatsapp.time.sleep = lambda s: None
    try:
        Page().enter()
        assert not pressed and clicked                                           # not in the box: no key, only the box's own send button
        clicked.clear()
        state.update(focused=True, text="oke")
        Page().enter()
        assert pressed == [(["enter"], "0xabc")] and not clicked, (pressed, clicked)     # sent: the button isn't touched as well
    finally:
        ld.send_keys, window_manager.find_app, whatsapp.time.sleep = real


def test_each_whatsapp_starts_with_its_control_port():
    import subprocess
    import linux_desktop as ld
    import whatsapp
    assert list(whatsapp.control_env("wasistlos").values()) == ["127.0.0.1:9231"] and whatsapp.control_env("firefox") == {}
    assert all(v.startswith("127.0.0.1:") for p in ("whatsie", "wasistlos") for v in whatsapp.control_env(p).values())     # never open to the network
    started = []
    real = subprocess.Popen
    subprocess.Popen = lambda args, **kw: started.append((args, kw["env"])) or object()
    try:
        assert ld.spawn(["wasistlos"]) and ld.spawn(["/usr/bin/whatsie"]) and ld.spawn(["firefox"])
    finally:
        subprocess.Popen = real
    assert started[0][1]["WEBKIT_INSPECTOR_HTTP_SERVER"] == "127.0.0.1:9231"
    assert started[1][1]["QTWEBENGINE_REMOTE_DEBUGGING"] == "127.0.0.1:9230"
    assert "WEBKIT_INSPECTOR_HTTP_SERVER" not in started[2][1] and "QTWEBENGINE_REMOTE_DEBUGGING" not in started[2][1]


def test_a_long_sentence_keeps_every_word():
    # (from the log: the first was never said, 6 Nov 15:26; the second lost "Ini dia" and said the rest twice)
    for said in ("Saat ini jendela yang terbuka di layar adalah Terminal (dua jendela kitty) dan Mozilla Firefox, Tuan.",
                 "Ini dia, DJ HELIKOPTER TURUN KE PADANG X BABAT BABAT VIRAL TIKTOK dalam mode mix, Tuan.",
                 "Hmm, jendela Facebook sepertinya tidak sedang terbuka, Tuan.",
                 "Tidak ada jadwal hari ini, Tuan. Acara terdekat: agenda besok."):
        assert " ".join(tts.in_clauses(said)).split() == said.split(), said


def test_a_timer_in_a_sentence_with_something_else_does_both():
    with recorded_actions() as calls:
        assert brain.fast_route("pasang timer 25 menit lalu matikan lampu")
        assert [c[0] for c in calls] == ["alarm", "lamp_power"] and calls[0][1] == "pasang timer 25 menit", calls
        calls.clear()
        assert brain.fast_route("matikan lampu dan pasang alarm jam 7")
        assert [c[0] for c in calls] == ["lamp_power", "alarm"] and calls[1][1] == "pasang alarm jam 7", calls
        calls.clear()
        assert brain.fast_route("matikan lampu dan jangan ganggu")
        assert [c[0] for c in calls] == ["lamp_power", "do_not_disturb"], calls


def test_one_reminder_stays_one_reminder():
    with recorded_actions() as calls:
        for one in ("ingatkan aku buka laptop jam 7", "ingatkan aku jam 7 kirim tugas dan matikan lampu",
                    "bangunkan aku jam 6 dan nyalakan lampu", "pasang alarm jam 7 buka youtube"):
            calls.clear()
            brain.fast_route(one)
            assert calls == [("alarm", one)], (one, calls)


def test_nothing_runs_when_one_part_cannot():
    with recorded_actions() as calls:
        brain.alarms.handle = lambda text: None
        assert brain.fast_route("matikan lampu dan ingatkan aku beli susu") is None and not calls, calls     # Gemini gets all of it, once
        assert brain.fast_route("buka firefox lalu ingatkan aku rapat") is None and not calls, calls         # not only the first half
        assert brain.fast_route("catat beli susu dan kirim paket") and calls[-1][0] == "write_note", calls   # but one note stays one note


def test_a_second_question_waits_for_the_first():
    with recorded_actions() as calls:
        brain.TOOL_MAP["port_process"] = lambda port, kill=False: safety.guard(
            f"menghentikan port {port}", lambda: calls.append(port) or f"Port {port} dihentikan.", always=True)
        said = brain.fast_route("matikan port 3000 dan matikan port 4000")
        assert said.count("Yakin") == 1 and "3000" in said and "4000" not in said, said
        said = safety.confirm()
        assert calls == [3000] and "Port 3000 dihentikan." in said and "4000" in said and safety.has_pending(), said
        safety.confirm()
        assert calls == [3000, 4000] and not safety.has_pending()
        calls.clear()
        brain.fast_route("matikan port 3000 dan matikan port 4000")
        safety.cancel()
        assert not calls and not safety.has_pending()                     # "tidak" drops the rest too


def test_a_port_of_another_user_is_not_called_free():
    import everyday
    real = everyday._find_port_owner, everyday._port_open
    everyday._find_port_owner = lambda port: None
    everyday._port_open = lambda port: port == 5432
    safety.cancel()
    try:
        said = everyday.port_process(5432)
        assert "terbuka" in said and "PostgreSQL" in said and "Tidak ada" not in said, said
        assert "Tidak ada" in everyday.port_process(4999)
        assert "layanannya" in everyday.port_process(5432, kill=True) and not safety.has_pending()
    finally:
        everyday._find_port_owner, everyday._port_open = real


def test_open_ports_said_short():
    import everyday
    table = "\n".join([f'LISTEN 0 5 0.0.0.0:8766 0.0.0.0:* users:(("python",pid={os.getpid()},fd=33))',
                       "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*", "LISTEN 0 128 [::]:22 [::]:*",
                       "LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:*", "LISTEN 0 200 127.0.0.1:5432 0.0.0.0:*", "LISTEN 0 200 [::1]:5432 [::]:*",
                       'LISTEN 0 2048 127.0.0.1:46699 0.0.0.0:* users:(("hermes",pid=856,fd=12))'] +
                      [f"LISTEN 0 5 127.0.0.1:{p} 0.0.0.0:*" for p in range(40000, 40006)])
    real = everyday.ld.out
    everyday.ld.out = lambda args, timeout=5: table
    try:
        said = everyday.open_ports()
    finally:
        everyday.ld.out = real
    assert said.startswith("Ada 11 port TCP terbuka"), said
    reach, local = said.split("Hanya di komputer ini:")
    assert "8766 Digidaw" in reach and "22 SSH" in reach and "11434" not in reach, said
    assert "5432 PostgreSQL" in local and "11434 Ollama" in local and "dan 3 lainnya" in local, said


def test_service_states_as_they_are():
    import everyday
    import sysadmin
    units = {("swaync.service", True): "failed", ("bluetooth.service", False): "active", ("sshd.service", False): "inactive"}
    files = {("*hermes*", True): "hermes-gateway.service enabled enabled", ("*dig*", True): "digidaw.service enabled enabled",
             ("*net*", False): "netctl.service disabled enabled\nnetctl@.service disabled enabled\nnetworkd.service enabled enabled"}
    units[("hermes-gateway.service", True)] = "active"
    units[("digidaw.service", True)] = "active"

    def out(args, timeout=5):
        user = "--user" in args
        if "show" in args:
            state = units.get((args[args.index("show") + 1], user))
            return f"LoadState={'loaded' if state else 'not-found'}\nActiveState={state or 'inactive'}"
        return files.get((args[-1], user), "")
    real = everyday.ld.out, sysadmin.run_as_root
    everyday.ld.out = out
    sysadmin.run_as_root = lambda command: f"ROOT {command}"
    safety.cancel()
    try:
        assert "gagal berjalan" in everyday.service_control("swaync") and "tidak ditemukan" not in everyday.service_control("swaync")
        assert "aktif" in everyday.service_control("bluetooth")
        assert "sedang mati" in everyday.service_control("sshd")
        assert "tidak ada di komputer ini" in everyday.service_control("layanan-yang-tidak-ada")
        assert "hermes-gateway" in everyday.service_control("hermes")          # a part of the name is enough when one fits
        assert "netctl" in everyday.service_control("net") and "Yang mana" in everyday.service_control("net")
        assert "tidak saya matikan" in everyday.service_control("dig", "stop") and not safety.has_pending()
        assert everyday.service_control("layanan-yang-tidak-ada", "stop").startswith("Layanan")   # no password window for nothing
        assert everyday.service_control("bluetooth", "restart") == "ROOT systemctl restart bluetooth.service"
        assert everyday.service_control("hermes", "stop").startswith(safety.CONFIRMATION_TOKEN) and "hermes-gateway" in safety.pending_description()
    finally:
        everyday.ld.out, sysadmin.run_as_root = real
        safety.cancel()


def test_only_a_real_error_gets_explained():
    import ai_skills
    import everyday
    import local_llm
    summary = ("Asisten Penjelas Error Terminal/Layar (explain_last_error):\n  Membaca teks yang sedang diblok, isi clipboard "
               "(bila berisi pesan error/traceback), atau OCR layar aktif.")
    crash = 'Traceback (most recent call last):\n  File "main.py", line 3, in <module>\nModuleNotFoundError: No module named \'requests\''
    state = {"selected": "", "clip": "", "screen": "", "answer": "Modul requests belum terpasang."}
    asked = []
    real = (everyday._clip_text, everyday._ocr_screen_text, local_llm.available, local_llm.generate, ai_skills.analyze_screen, everyday.ld.active_window)
    everyday._clip_text = lambda primary=False: state["selected" if primary else "clip"]
    everyday._ocr_screen_text = lambda: (state["screen"], "")
    local_llm.available = lambda refresh=False: True
    local_llm.generate = lambda system, user, **kw: asked.append(user) or state["answer"]
    ai_skills.analyze_screen = lambda question: "DARI LAYAR"
    everyday.ld.active_window = lambda: {"title": "Terminal"}
    try:
        state.update(selected=summary)                 # highlighted long ago, and it only mentions errors: 6 Nov 15:30
        assert everyday.explain_last_error() == "DARI LAYAR" and not asked
        state.update(selected=crash)
        assert everyday.explain_last_error() == state["answer"] and "No module named" in asked[-1]
        state.update(selected="", clip=crash)
        assert everyday.explain_last_error() == state["answer"]
        state.update(clip="", screen="baris biasa\n" * 400 + "zsh: command not found: pyhton\n~ > ")
        assert everyday.explain_last_error() == state["answer"] and "command not found: pyhton" in asked[-1]     # where a terminal has it: the end
        state.update(screen="", selected=crash, answer="Maaf, aku tidak bisa membantu karena tidak ada konteks.")
        assert everyday.explain_last_error() == "DARI LAYAR"                     # a shrug from the small model isn't an answer
    finally:
        everyday._clip_text, everyday._ocr_screen_text, local_llm.available, local_llm.generate, ai_skills.analyze_screen, everyday.ld.active_window = real


def test_reading_the_screen_goes_all_the_way_to_tesseract():
    # (capture() gives a JPEG and a monitor, not a dict: the first version never got past that line, and every test
    # replaced this function)
    import everyday
    import pc_control
    from PIL import Image
    seen = {}

    def run(args, timeout=5, input=None):
        seen.update(args=args, kind=Image.open(args[1]).format)
        return Ran(out=" dua kata \n")
    real = pc_control.focused_picture, everyday.ld.run, everyday.ld.out, everyday.shutil.which
    pc_control.focused_picture = lambda hide_own=False: (Image.new("RGB", (64, 32)), {})
    everyday.ld.run = run
    everyday.ld.out = lambda args, timeout=5: "List of available languages (3):\neng\nind\nosd"
    everyday.shutil.which = lambda name: f"/usr/bin/{name}"
    try:
        assert everyday._ocr_screen_text() == ("dua kata", "")
        assert seen["kind"] == "PNG" and seen["args"][seen["args"].index("-l") + 1] == "ind+eng" and not os.path.exists(seen["args"][1])
        everyday.ld.run = lambda args, timeout=5, input=None: None                 # tesseract took too long
        assert "tidak sempat" in everyday._ocr_screen_text()[1]

        def no_screen(hide_own=False):
            raise RuntimeError("grim gagal")
        pc_control.focused_picture = no_screen
        assert "tidak bisa ditangkap" in everyday._ocr_screen_text()[1]
    finally:
        pc_control.focused_picture, everyday.ld.run, everyday.ld.out, everyday.shutil.which = real


def test_clipboard_history_keeps_links_and_not_passwords():
    import everyday
    for kept in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123456", "/home/tuan/Project/digidaw/tests/test_control.py", "~/Downloads/film.mkv",
                 "sudo pacman -Syu", "test_control.py", "python3.12", "10.0.0.18", "nama@contoh.com", "HelloWorld", "halo apa kabar"):
        assert not everyday._looks_secret(kept), kept
    for secret in ("Xk9#mP2$vL7qR4!z", "MyP@ssw0rd2026", "hunter2hunter2", "ghp_" + "abcdefghijklmnopqrstuvwxyz123456",
                   "AIza" + "SyD-abc_defghijklmnopqrstuvwxyz1234567", "password saya rahasia123", "GEMINI_API_KEY=abc",
                   "a37c0123456789abcdef0123456789ab", "__digidaw_1793950994.12__", "https://situs.com/masuk?token=abc123"):
        assert everyday._looks_secret(secret), secret
    assert everyday._clip_preview("https://www.youtube.com/watch?v=abc") == "link youtube.com"
    assert everyday._clip_preview("/home/tuan/tests/test_control.py") == "file test_control.py"


def test_muting_the_mic_asks_and_the_talk_key_brings_it_back():
    import config
    import pc_control
    state = {"muted": False}

    def run(args, timeout=5, input=None):
        if "set-mute" in args:
            state["muted"] = args[-1] == "1"
        return Ran()
    real = pc_control.ld.run, pc_control.ld.out, pc_control.shutil.which
    pc_control.ld.run = run
    pc_control.ld.out = lambda args, timeout=5: "Volume: 1.00 [MUTED]" if state["muted"] else "Volume: 1.00"
    pc_control.shutil.which = lambda name: f"/usr/bin/{name}"
    safety.cancel()
    try:
        said = pc_control.mic_control("mute")
        assert said.startswith(safety.CONFIRMATION_TOKEN) and not state["muted"] and "tidak bisa mendengar" in safety.question()
        reply = safety.confirm()
        assert state["muted"] and config.PUSH_TO_TALK_HOTKEY.upper() in reply, reply
        assert pc_control.unmute_for_talking() is True and not state["muted"]
        assert pc_control.unmute_for_talking() is False
    finally:
        pc_control.ld.run, pc_control.ld.out, pc_control.shutil.which = real
        safety.cancel()
    # the hotkey / HUD mic goes through it, before the listening starts
    order = []
    c = core.DigidawCore(voice_enabled=False)
    c.voice_enabled = True
    c.arm = lambda how: order.append("dengar")
    real_unmute = pc_control.unmute_for_talking
    pc_control.unmute_for_talking = lambda: order.append("mic") or True
    try:
        c.push_to_talk("tombol")
    finally:
        pc_control.unmute_for_talking = real_unmute
    assert order == ["mic", "dengar"], order


def test_update_watch_says_done_only_after_it_ran():
    import watchers
    real = watchers._updating_now, watchers.STATE_FILE
    running = {"now": False}
    watchers._updating_now = lambda: running["now"]
    watchers.STATE_FILE = Path(tempfile.mkdtemp()) / "watchers.json"
    try:
        w = {"kind": "update", "target": "", "value": "", "created": time.time() - 20, "state": {}}
        assert watchers._check_update(w) is None                          # no update ever ran: not "selesai"
        w["created"] = time.time() - 999
        assert "tidak melihat" in watchers._check_update(w)               # it gives up, and says so
        w = {"kind": "update", "target": "", "value": "", "created": time.time(), "state": {}}
        running["now"] = True
        assert watchers._check_update(w) is None and w["state"]["seen"]
        running["now"] = False
        assert "sudah selesai" in watchers._check_update(w)
    finally:
        watchers._updating_now, watchers.STATE_FILE = real


def test_small_model_gets_the_real_sound_outputs():
    import local_router
    import pc_control
    real = pc_control.ld.out
    pc_control.ld.out = lambda args, timeout=5: json.dumps([{"name": "a", "description": "USB Audio"}])
    try:
        assert "USB Audio" in local_router._tool_candidates("audio_output")
    finally:
        pc_control.ld.out = real


def test_started_from_the_menu_goes_through_the_service():
    script = Path("start_digidaw.sh").read_text()
    assert "systemctl --user start digidaw" in script and "/proc/self/cgroup" in script
    assert script.index("systemctl --user start digidaw") < script.index('exec "$VENV/bin/python" main.py')


if __name__ == "__main__":
    ok = True
    for test in [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]:
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
