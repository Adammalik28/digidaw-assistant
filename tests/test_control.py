"""
Tests for "full control" (4 Nov 2026): the local brain's forced choice, learning from the online AI, the screen job
(with a fake AI, a fake screen and a fake mouse: nothing is really clicked), settings, files. Offline.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_control.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config        # noqa: E402
import brain         # noqa: E402
import experience    # noqa: E402
import file_tools    # noqa: E402
import local_llm     # noqa: E402
import local_router  # noqa: E402
import pc_control    # noqa: E402
import presence      # noqa: E402
import safety        # noqa: E402
import screen_agent  # noqa: E402
import system_control as sc  # noqa: E402
import habits        # noqa: E402
import knowledge     # noqa: E402
import metrics       # noqa: E402

habits.HABITS_FILE, habits._data = Path(tempfile.mkdtemp()) / "habits.json", None      # (a test opening "youtube" was counted in the real file)
metrics.count = lambda *a, **k: None


REAL_AIM = screen_agent._aim
REAL_CHANGED_NEAR = pc_control.changed_near


def _answers(*answers):
    """local_llm.generate replaced: gives these answers in turn, remembers what it was asked."""
    asked, queue = [], list(answers)

    def fake(system, user, schema=None, max_tokens=200, temperature=0.2, history=None, stop=None):
        asked.append({"system": system, "user": user, "schema": schema})
        return queue.pop(0)
    local_llm.generate = fake
    return asked


def test_closing_only_what_is_meant():
    """"tutup agenda" once quit all of Chrome with the HUD in it: a window named by its title goes alone, and the browser the
    HUD lives in is never quit by a plain "tutup"."""
    import window_manager as wm
    import psutil

    class Proc:
        def __init__(self, pid, name): self.pid, self.info = pid, {"name": name, "pid": pid}
        def name(self): return self.info["name"]
    windows = [{"hwnd": "0x1", "title": "DIGIDAW HUD", "pid": 50, "process": "chrome", "class": "chrome", "workspace": "1"},
               {"hwnd": "0x2", "title": "Agenda Digidaw", "pid": 50, "process": "chrome", "class": "chrome-agenda", "workspace": "1"},
               {"hwnd": "0x3", "title": "Berita - Google Chrome", "pid": 50, "process": "chrome", "class": "google-chrome", "workspace": "2"},
               {"hwnd": "0x4", "title": "Catatan", "pid": 60, "process": "mousepad", "class": "Mousepad", "workspace": "2"}]
    closed, quit_ = [], []
    real = (wm._windows, wm.ld.close, sc._matching_processes, sc._quit_processes, sc._program_of, psutil.Process)
    wm._windows, wm.ld.close = (lambda: windows), lambda hwnd: closed.append(hwnd) or True
    sc._program_of = lambda name: None
    sc._matching_processes = lambda name: [Proc(50, "chrome")] if name == "chrome" else []
    sc._quit_processes = lambda procs: (quit_.extend(p.pid for p in procs), ([], []))[1]
    try:
        said = sc.close_application("agenda")                                   # (found by the words of its title)
        assert closed == ["0x2"] and not quit_ and "ditutup" in said, (closed, quit_, said)
        closed.clear()
        said = sc.close_application("chrome")                                   # the HUD is in there: the other windows go
        assert set(closed) == {"0x2", "0x3"} and not quit_ and "HUD tetap" in said, (closed, quit_, said)
        closed.clear()
        said = sc.close_application("revo")                                     # a misheard name: what is open helps
        assert "Tidak ada" in said and "Mousepad" in said and not closed and not quit_, said
    finally:
        wm._windows, wm.ld.close, sc._matching_processes, sc._quit_processes, sc._program_of, psutil.Process = real
    assert sc._process_said("soffice.bin") == "LibreOffice" and sc._process_said("firefox") == "firefox"


def test_learning_needs_more_than_one_answer():
    experience.FILE, experience._items = Path(tempfile.mkdtemp()) / "e.json", []
    experience.note("atur power", "power_mode", {"mode": "seimbang"})           # a setting the user never said
    experience.note("atur power", "power_mode", {"mode": "seimbang"})
    assert experience.count() == 0
    experience.note("mode hemat dong", "power_mode", {"mode": "hemat"})
    experience.note("mode hemat dong", "power_mode", {"mode": "hemat"})
    assert [i["text"] for i in experience.similar("mode hemat dong")] == ["mode hemat dong"]
    experience.note("buka hud", "show_hud", {})
    experience.note("buka hud", "open_application", {"name": "hud"})            # another answer: it starts over
    assert experience.similar("buka hud") == []
    # a command right after the online AI's answer: the answer doesn't count ("tutup screen", then "hidupkan layar")
    brain._lesson.clear()
    brain._lesson.update(text="tutup screen", tool="screen_power", args={"on": False}, at=time.time())
    brain._settle_lesson()
    assert not any(i["text"] == "tutup screen" for i in experience._items) and not brain._lesson
    brain._lesson.update(text="kalimat lama sekali", tool="get_time", args={}, at=time.time() - 60)
    brain._settle_lesson()
    assert any(i["text"] == "kalimat lama sekali" for i in experience._items)


def test_guesses_and_misheard_words():
    """Google's other guesses, and the words it really wrote in the log."""
    import core
    import hearing
    import habits
    routes = {"tutup brave": ("close_application", {"name": "brave"}), "tutup bread": None, "putar bread": ("play_youtube", {"query": "bread"})}
    real_route, real_resolve = brain._route, __import__("resolver").resolve
    brain._route = lambda text: routes.get(text.lower())
    __import__("resolver").resolve = lambda name, kinds=(), keep=None, sure_only=False: ("sure" if name == "brave" else "none", [])
    try:
        assert hearing.best_guess(["tutup bread", "tutup bray", "tutup Brave"]) == "tutup Brave"
        assert hearing.best_guess(["tutup Brave", "tutup bread"]) == "tutup Brave"      # the first guess wins when it's good
        assert hearing.best_guess(["apa kabar", "apa kabar ya"]) == "apa kabar"         # nothing better: the first
        assert hearing.best_guess(["putar bread", "tutup bread"]) == "putar bread"
        routes["ig"] = ("open_website", {"site": "instagram"})
        assert hearing.best_guess(["lagi dong", "IG"]) == "lagi dong"                   # (the name misheard: a call stays a call)
        assert hearing.best_guess(["kapan hujan", "IG"]) == "kapan hujan"               # a stray guess that isn't alike
        safety.guard("uji", lambda: None, always=True)
        assert hearing.best_guess(["tutup bread", "tutup Brave"]) == "tutup bread"      # an answer is awaited: nothing changes
        safety.cancel()
        brain._remember_turn("tambah agenda", "Acaranya apa dan kapan, Tuan?")
        assert hearing.best_guess(["tutup bread", "tutup Brave"]) == "tutup bread"
        brain._dialog.clear()
        long = "kenapa kalau aku tutup bread terus lama sekali ya"
        assert hearing.best_guess([long, "tutup Brave"]) == long                        # talk stays talk
    finally:
        brain._route, __import__("resolver").resolve = real_route, real_resolve
    # the first guess also stands when choosing fails
    import stt
    engine = stt.stt_engine
    saved = engine.choose_guess
    engine.choose_guess = lambda guesses: 1 / 0
    assert engine._understood_guess(["a", "b"]) == "a"
    engine.choose_guess = lambda guesses: "b"
    assert engine._understood_guess(["a", "b"]) == "b"
    engine.choose_guess = saved

    for said, meant in (("gigi daun buka wasit lost", "buka wasit lost"), ("Set timer 20 menit biji daun Set timer 20 menit", "Set timer 20 menit"),
                        ("tutup Firefox di bidang", "tutup Firefox"), ("tutup agenda hijau", "tutup agenda"), ("7 daun buka terminal", "buka terminal"), ("bidang Besok kuliah apa", "Besok kuliah apa"),
                        ("ubah lampu ke hijau", "ubah lampu ke hijau"), ("lampu hijau", "lampu hijau"), ("putar lagu Hijau Daun", "putar lagu Hijau Daun"),
                        ("putar Dido", "putar Dido"), ("cari gigi palsu", "cari gigi palsu"), ("sakit gigi", "sakit gigi"), ("hijau daun", "hijau daun")):
        assert core.after_misheard_name(said) == meant, (said, core.after_misheard_name(said))
    for call in ("hijau daun", "biji daun", "gigi daun", "7 daun", "hijau", "bidang", "dicky", "dino", "dihidang"):
        assert core.sounds_like_call(call), call
    for words in ("lampu hijau", "buka firefox", "firefox", "ya"):
        assert not core.sounds_like_call(words), words

    # routes for what "pindah" and "tutup" turn into, and a stray first word
    assert brain._route("ini pindah ke word ps2") == ("switch_workspace", {"workspace": "2"})
    assert brain._route("jangan pindah ke firefox") is None and brain._route("jangan buka firefox") is None
    assert brain._route("tidak berapa sekarang") == ("get_time", {})
    assert brain._route("minta maaf") is None and brain._route("youtube musik santai") is None
    assert brain._route("oke buka youtube") == brain._route("buka youtube")


def test_windows_find_their_workspace():
    """A routine must not move the screen, and a slow app's later window (Discord's updater, then Discord) goes to its place too."""
    import window_manager as wm
    windows = [{"hwnd": "0xa", "title": "Discord | Firefox", "pid": 1, "process": "firefox", "class": "firefox", "workspace": "2"},
               {"hwnd": "0xb", "title": "scratch", "pid": 2, "process": "kitty", "class": "kitty", "workspace": "special:scratchpad"}]
    moved, switched = [], []
    real = (wm._windows, pc_control.ld.move_to_workspace, pc_control.ld.switch_workspace, pc_control.PLACE_SECONDS, sc.open_app_or_web)
    wm._windows = lambda: list(windows)
    pc_control.ld.move_to_workspace = lambda address, workspace, follow=False: (moved.append((address, workspace)), next(w for w in windows if w["hwnd"] == address).update(workspace=workspace))
    pc_control.ld.switch_workspace = lambda target: switched.append(target)
    pc_control.PLACE_SECONDS = 6
    opened = []
    sc.open_app_or_web = lambda name: opened.append(name) or "ok"
    try:
        with pc_control.ld.quiet_opening():                                       # as in a routine
            pc_control.open_on_workspace("discord", "1")
        assert opened == ["discord"] and not switched, (opened, switched)         # the screen stays
        time.sleep(0.4)
        assert not moved                                                          # a browser tab named Discord isn't Discord
        windows.append({"hwnd": "0xc", "title": "Discord Updater", "pid": 3, "process": "Discord", "class": "discord", "workspace": "2"})
        time.sleep(0.4)
        assert moved == [("0xc", "1")], moved
        windows[-1].update(workspace="2")                                         # the user moved it afterwards: stays
        time.sleep(0.4)
        assert moved == [("0xc", "1")], moved
        windows.append({"hwnd": "0xd", "title": "Discord", "pid": 3, "process": "Discord", "class": "discord", "workspace": "2"})
        time.sleep(0.4)
        assert moved == [("0xc", "1"), ("0xd", "1")], moved                       # the real window, which comes later
        windows[:] = [w for w in windows if w["hwnd"] != "0xc"]                   # closed, and its address given to a new window
        time.sleep(0.4)
        windows.append({"hwnd": "0xc", "title": "Discord", "pid": 3, "process": "Discord", "class": "discord", "workspace": "2"})
        time.sleep(0.4)
        assert moved[-1] == ("0xc", "1") and len(moved) == 3, moved
        moved.clear()
        assert wm.is_app_window(windows[-1], "discord") and not wm.is_app_window(windows[0], "discord")
        assert wm.is_app_window({"process": "whatsie", "class": "com.ktechpit.whatsie"}, "whatsapp")
        assert wm.is_app_window({"process": "vesktop", "class": "vesktop"}, "discord")
        assert not wm.is_app_window({"process": "firefox", "class": "firefox"}, "whatsapp")
        # already open: moved, not opened again
        opened.clear()
        windows.append({"hwnd": "0xe", "title": "Telegram", "pid": 4, "process": "telegram-desktop", "class": "org.telegram.desktop", "workspace": "3"})
        with pc_control.ld.quiet_opening():
            pc_control.open_on_workspace("telegram", "1")
        time.sleep(0.4)
        assert not opened and ("0xe", "1") in moved, (opened, moved)
        # on its own the screen goes there, once
        switched.clear()
        pc_control.open_on_workspace("telegram", "4")
        assert switched == ["4"], switched
    finally:
        wm._windows, pc_control.ld.move_to_workspace, pc_control.ld.switch_workspace, pc_control.PLACE_SECONDS, sc.open_app_or_web = real
        pc_control._placing.clear()
    assert not pc_control.ld.opening_quietly()


def test_going_where_you_are_stays_there():
    """binds:workspace_back_and_forth: "workspace 2" while on 2 throws the screen back to the one before."""
    ld = pc_control.ld
    sent = []
    real = (ld.active_workspace, ld.dispatch)
    ld.active_workspace, ld.dispatch = lambda: "2", lambda *a: sent.append(a) or True
    try:
        assert ld.switch_workspace("2") and not sent
        assert ld.switch_workspace("3") and sent == [("workspace", "3")]
        assert ld.switch_workspace("+1") and sent[-1] == ("workspace", "+1")
    finally:
        ld.active_workspace, ld.dispatch = real


def test_routine_steps_with_workspace_and_sites():
    import routines
    import site_finder
    real_find, site_finder.find = site_finder.find, lambda name, online=False: {"medanpedia": "https://medanpedia.co.id/", "uneedpanel": "https://uneedpanel.com/",
                                                                  "lms kampus": "https://lms.kampus.ac.id/"}.get(name) or (f"https://{name}" if site_finder.is_address(name) else None)
    try:
        def one(phrase):
            steps, missed = routines.parse_step(phrase)
            assert not missed, (phrase, missed)
            return steps
        assert one("buka whatsapp di workspace 1") == [{"say": "buka whatsapp di workspace 1", "tool": "open_on_workspace", "args": {"name": "whatsapp", "workspace": "1"}}]
        assert one("firefox di workspace 2")[0]["args"] == {"name": "firefox", "workspace": "2"}
        assert one("buka Firefox ke workspace dua")[0]["args"]["workspace"] == "2"
        assert one("buka whatsapp di word ps1")[0]["args"]["workspace"] == "1"
        assert one("buka youtube di workspace 3")[0]["args"] == {"name": "youtube", "workspace": "3"}
        # sites Digidaw didn't know: what kept a routine from being saved
        assert one("buka situs medanpedia") == [{"say": "buka medanpedia.co.id", "tool": "open_website", "args": {"site": "https://medanpedia.co.id/"}}]
        assert [s["args"]["site"] for s in one("buka situs medanpedia dan uneedpanel")] == ["https://medanpedia.co.id/", "https://uneedpanel.com/"]
        assert one("buka lms kampus")[0]["args"]["site"] == "https://lms.kampus.ac.id/"
        assert one("buka situs medanpedia di workspace 3")[0]["args"] == {"name": "https://medanpedia.co.id/", "workspace": "3"}
        assert one("buka situs contoh titik co titik id")[0]["args"]["site"] == "https://contoh.co.id"
        assert one("buka youtube") == [{"say": "buka youtube"}] and one("lampu warna merah") == [{"say": "lampu warna merah"}]
        steps, missed = routines.parse_step("buka situs tidakada")
        assert not steps and "tidakada" in missed
        steps, missed = routines.parse_step("buka situs medanpedia dan tidakada")      # what was found is kept
        assert [s["args"]["site"] for s in steps] == ["https://medanpedia.co.id/"] and "tidakada" in missed
        assert "Alamat tidakada belum ketemu" in routines._not_understood(missed)
        assert routines.parse_step("daun meja piring") == ([], "daun meja piring")     # a word with no "buka" opens nothing
        assert routines._split("whatsapp di workspace 1 dan firefox di workspace 2") == ["whatsapp di workspace 1", "firefox di workspace 2"]
        assert routines._split("buka firefox dan buka instagram") == ["buka firefox", "buka instagram"]
        assert routines._split("buka situs medanpedia dan uneedpanel") == ["buka situs medanpedia dan uneedpanel"]
    finally:
        site_finder.find = real_find
    assert site_finder.is_address("lms.kampus.ac.id") and site_finder.is_address("https://a.id/x") and not site_finder.is_address("medanpedia")
    assert brain._route("whatsapp di workspace 1") == ("open_on_workspace", {"name": "whatsapp", "workspace": "1"})
    assert brain._route("buka web youtube di workspace 2") == ("open_on_workspace", {"name": "youtube", "workspace": "2"})
    assert brain._route("pindah ke workspace 2") == ("switch_workspace", {"workspace": "2"})
    assert (brain._route("daun meja di workspace 2") or ("",))[0] != "open_on_workspace"


def test_routines_by_talking():
    """A routine taught step by step, in words, and run without any AI."""
    import routines
    import protocols
    import habits
    file = Path(tempfile.mkdtemp()) / "protocols.json"
    real_file, protocols.PROTOCOLS_FILE = protocols.PROTOCOLS_FILE, file
    real_alias_file, habits.HABITS_FILE = habits.HABITS_FILE, Path(tempfile.mkdtemp()) / "habits.json"
    real_data, habits._data = habits._data, None
    real_map = dict(brain.TOOL_MAP)
    done = []
    for tool in ("open_application", "open_website", "lamp_color", "set_volume", "switch_workspace", "close_application"):
        brain.TOOL_MAP[tool] = (lambda t: lambda **kw: (done.append((t, kw)), brain._calls_this_turn.append((t, kw, "ok")), "ok")[2])(tool)
    real_sleep, protocols.time.sleep = protocols.time.sleep, lambda s: done.append(("tunggu", s))
    try:
        routines._teaching.clear()
        # step by step
        assert "Nama rutinitasnya" in routines.handle("buat rutinitas baru") and routines.teaching()
        assert "Langkah pertamanya" in routines.handle("kuliah")
        assert routines.handle("buka Firefox") == "Buka Firefox. Lalu?" and not done     # taught, not run
        assert "belum saya pahami" in routines.handle("daun meja piring")
        assert routines.handle("lampu warna putih").endswith("Lalu?")
        assert "hapus" in routines.handle("hapus yang tadi")                             # taken back
        assert routines.handle("lampu warna merah").endswith("Lalu?")
        assert routines.handle("tunggu 2 detik").endswith("Lalu?")
        assert routines.handle("pindah ke workspace 2 lalu volume 30").endswith("Lalu?")
        said = routines.handle("selesai")
        assert "tersimpan" in said and "5 langkah" in said and "mode kuliah" in said, said
        assert not routines.teaching()
        saved = protocols.load()["kuliah"]
        assert saved["custom"] and [s["say"] for s in saved["steps"]] == ["buka Firefox", "lampu warna merah", "tunggu 2 detik", "pindah ke workspace 2", "volume 30"]
        assert saved["lamp_added"]                                                       # no lamp step added by itself
        assert not done

        # running it: what was said, in order, with no AI, a pause for the window, a short answer
        for run in ("mode kuliah", "jalankan mode kuliah", "rutinitas kuliah", "Mode Kuliah."):
            done.clear()
            brain._calls_this_turn.clear()
            said = routines.handle(run)
            assert said.startswith("Rutinitas kuliah sudah jalan"), (run, said)
            assert [d[0] for d in done] == ["open_application", "tunggu", "lamp_color", "tunggu", "switch_workspace", "tunggu", "set_volume"], done
        assert done[2][1]["color"] == "merah" and done[4][1]["workspace"] == "2" and done[6][1]["level"] == 30, done

        # all at once, a name another command uses, cancelling, "sudah" with nothing yet
        assert "tersimpan" in routines.handle("buat rutinitas pagi buka firefox lalu volume 20")
        assert [s["say"] for s in protocols.load()["pagi"]["steps"]] == ["buka firefox", "volume 20"]
        assert "sudah dipakai" in routines.handle("buat rutinitas hemat")
        routines._teaching.clear()
        assert routines.handle("buat mode hemat") is None                                # that's the power mode
        assert "Belum ada" in (routines.handle("buat mode ngoding"), routines.handle("selesai"))[1]
        assert "tidak jadi" in routines.handle("batal") and not routines.teaching()
        assert "ngoding" not in protocols.load()

        # other things stay what they are
        assert routines.handle("mode hemat") is None and routines.handle("mode malam") is None
        assert routines.handle("buka firefox") is None and routines.handle("lampu mode santai") is None
        assert routines.handle("mode jangan ganggu") is None
        # a routine doesn't run a routine, send a message, or do something unknown
        assert not routines.understood("mode kuliah") and not routines.understood("balas pesannya oke")
        assert not routines.understood("daun meja piring")
        assert routines.understood("set timer 25 menit") and routines.understood("tutup firefox")

        # asking, deleting (only on "ya")
        assert "buka Firefox" in routines.handle("apa isi mode kuliah")
        assert "kuliah" in routines.handle("rutinitas saya apa saja")
        question = routines.handle("hapus rutinitas kuliah")
        assert question.startswith(safety.CONFIRMATION_TOKEN) and "kuliah" in protocols.load()
        assert "kuliah" in safety.question()
        safety.cancel()
        assert "dihapus" in routines.delete("kuliah") and "kuliah" not in protocols.load()

        # a step that needs "ya" is asked at the end; what fails is said
        protocols.save({"coba": {"custom": True, "lamp_added": True, "steps": [{"say": "buka firefox"}, {"say": "volume 20"}]}})
        brain.TOOL_MAP["set_volume"] = lambda **kw: safety.guard("uji", lambda: None, always=True)
        assert "uji" in routines.handle("mode coba") and safety.has_pending()
        safety.cancel()
        brain.TOOL_MAP["set_volume"] = lambda **kw: "Volume gagal diubah."
        assert "Yang tidak berhasil: volume 20" in routines.handle("mode coba")

        # teaching a word
        said = routines.handle("kalau aku bilang ruang dua maksudnya pindah ke workspace 2")
        assert said.startswith("Siap") and habits.learned_aliases()["ruang dua"] == "pindah ke workspace 2", said
        assert habits.apply_aliases("ruang dua") == "pindah ke workspace 2"
        assert brain._route(habits.apply_aliases("ruang dua")) == ("switch_workspace", {"workspace": "2"})
        assert "belum paham" in routines.handle("kalau saya bilang daun maksudnya meja piring lemari")
        assert "dipakai" in routines.handle("kalau aku bilang buka artinya tutup firefox") and "buka" not in habits.learned_aliases()
        assert "lupakan" in routines.handle("lupakan kata ruang dua") and "ruang dua" not in habits.learned_aliases()

        # the Control Panel
        assert routines.save_typed("sore", ["buka firefox", "", "volume 20"])["ok"] and "sore" in protocols.load()
        wrong = routines.save_typed("malam ini", ["daun meja"])
        assert not wrong["ok"] and "daun meja" in wrong["error"]
        assert not routines.save_typed("kosong", [" "])["ok"]
        rows = protocols.for_hud()
        assert rows[0]["custom"] and rows[0]["steps"]                                    # the user's own first
        assert protocols.step_words({"tool": "set_volume", "args": {"level": 30}}) == "volume 30"
        assert protocols.step_words({"tool": "window_action", "args": {"action": "minimize_all"}}) == "sembunyikan semua jendela"
    finally:
        protocols.PROTOCOLS_FILE, habits.HABITS_FILE, habits._data = real_file, real_alias_file, real_data
        protocols.time.sleep = real_sleep
        brain.TOOL_MAP.clear()
        brain.TOOL_MAP.update(real_map)
        routines._teaching.clear()
        safety.cancel()


def test_hud_buttons_follow_use():
    import habits
    real, habits._data = habits._data, {"usage": {"app": {"firefox": {"count": 65, "last": 5}, "terminal": {"count": 70, "last": 4},
                                                          "dipakai sekali": {"count": 1, "last": 9}, "tidak ada namanya": {"count": 9, "last": 9}},
                                                  "web": {"youtube": {"count": 51, "last": 3}, "www": {"count": 1, "last": 9}}}, "aliases": {}}
    try:
        names = [s["command"] for s in habits.shortcuts()]
        assert names == ["buka terminal", "buka firefox", "buka youtube"], names      # most used; no one-offs, no unknown names
    finally:
        habits._data = real


def test_other_apps_get_their_volume_back():
    """Firefox ended at 48%, Chrome at 24%: the sound system remembers each app's last volume, a new stream started low, and the
    next lowering started from there."""
    import media
    import linux_desktop as ld
    streams = [{"index": 1, "volume": {"a": {"value_percent": "100%"}}, "properties": {"application.name": "Firefox", "application.process.id": "11"}},
               {"index": 2, "volume": {"a": {"value_percent": "80%"}}, "properties": {"application.name": "Google Chrome", "application.process.id": "22"}},
               {"index": 3, "volume": {"a": {"value_percent": "100%"}}, "properties": {"application.name": "Digidaw", "application.process.id": str(os.getpid())}},
               {"index": 4, "corked": True, "volume": {"a": {"value_percent": "100%"}}, "properties": {"application.name": "Spotify", "application.process.id": "33"}}]

    def run(args, **kw):
        if args[:2] == ["pactl", "set-sink-input-volume"]:
            for s in streams:
                if s["index"] == int(args[2]):
                    s["volume"]["a"]["value_percent"] = args[3]
        calls.append(args)
    calls = []
    level = lambda i: next(s["volume"]["a"]["value_percent"] for s in streams if s["index"] == i)
    real = (ld._sink_inputs, ld.run, ld._is_child, media.DUCK_FILE, config.DUCK_LEVEL, config.DUCK_MEDIA)
    ld._sink_inputs, ld.run, ld._is_child = (lambda: streams), run, (lambda pid: False)
    remembered = []
    real_remember, ld._remember_volume = ld._remember_volume, lambda app, percent: remembered.append((app, percent))
    media.DUCK_FILE, config.DUCK_LEVEL, config.DUCK_MEDIA = Path(tempfile.mkdtemp()) / "ducked.json", 0.5, True
    media._ducked.clear()
    media._duck_depth = 0
    try:
        media.duck()
        assert (level(1), level(2), level(3), level(4)) == ("50%", "40%", "100%", "100%")     # only what plays, not Digidaw
        assert json.loads(media.DUCK_FILE.read_text()) == {"Firefox": 100, "Google Chrome": 80}
        media.duck()                                                       # (speaking while listening: once, not twice)
        assert level(1) == "50%"
        streams.append({"index": 5, "volume": {"a": {"value_percent": "50%"}},       # the next song: a new stream, started low
                        "properties": {"application.name": "Firefox", "application.process.id": "11"}})
        streams[:] = [s for s in streams if s["index"] != 2]               # Chrome stopped while lowered
        media.unduck()
        assert level(1) == "50%" and media.is_ducked()                     # the other one still needs it
        media.unduck()
        assert (level(1), level(5)) == ("100%", "100%") and not media.is_ducked() and not media.DUCK_FILE.exists()
        assert remembered == [("Google Chrome", 80)], remembered           # nothing playing to set: what's remembered is set

        # again and again it stays where it was
        for _ in range(4):
            media.duck()
            media.unduck()
        assert level(1) == "100%"

        # killed while lowered: the next start gives it back, and doesn't take the low volume for the real one
        media.duck()
        assert level(1) == "50%"
        media._ducked.clear()
        media._duck_depth = 0
        media.restore_left_ducked()
        assert level(1) == "100%" and not media.DUCK_FILE.exists()
    finally:
        ld._sink_inputs, ld.run, ld._is_child, media.DUCK_FILE, config.DUCK_LEVEL, config.DUCK_MEDIA = real
        ld._remember_volume = real_remember
        media._ducked.clear()
        media._duck_depth = 0


def test_recap_waits_quietly():
    """With Do Not Disturb on, the recap came up on the HUD every 30 s without a sound, for hours."""
    import daily
    announced = []
    real = (daily.ANNOUNCE, config.DAILY_RECAP_HOUR, presence.busy_reason, presence.is_away, daily._current, daily._save_locked)
    day = {"active": 9000, "recap": False, "apps": {}, "commands": 0, "tools": {}, "day": ""}
    daily.ANNOUNCE = lambda text, level, **kw: announced.append(text) or True
    config.DAILY_RECAP_HOUR = 1
    daily._current, daily._save_locked = (lambda: day), (lambda: None)
    presence.is_away = lambda: False
    try:
        import datetime
        if datetime.datetime.now().hour >= 1:
            presence.busy_reason = lambda front=None: "Do Not Disturb"
            for _ in range(3):
                daily._maybe_recap()
            assert not announced and not day["recap"]                      # nothing yet, and still to come
            presence.busy_reason = lambda front=None: ""
            daily._maybe_recap()
            daily._maybe_recap()
            assert len(announced) == 1 and day["recap"], announced         # said once, when it can be heard
    finally:
        daily.ANNOUNCE, config.DAILY_RECAP_HOUR, presence.busy_reason, presence.is_away, daily._current, daily._save_locked = real


def test_arranging_windows():
    """Sentences, and what the tools do with made-up windows (a real run moves real windows: tests/e2e_hands.py style, by hand)."""
    import window_manager as wm
    for sentence, action in (("bagi layar firefox dan terminal", ("side_by_side", {"left": "firefox", "right": "terminal"})),
                             ("sandingkan firefox sama terminal", ("side_by_side", {"left": "firefox", "right": "terminal"})),
                             ("firefox kiri terminal kanan", ("side_by_side", {"left": "firefox", "right": "terminal"})),
                             ("terminal di kanan firefox di kiri", ("side_by_side", {"left": "firefox", "right": "terminal"})),
                             ("taruh firefox di kiri", ("place_window", {"name": "firefox", "side": "kiri"})),
                             ("buka terminal di kanan", ("place_window", {"name": "terminal", "side": "kanan"})),
                             ("geser ke kanan", ("place_window", {"name": "", "side": "kanan"})),
                             ("taruh jendela ini di kiri", ("place_window", {"name": "", "side": "kiri"})),
                             ("tukar posisi jendela", ("swap_windows", {})), ("susun jendela", ("tidy_windows", {})), ("rapikan jendela", ("tidy_windows", {})),
                             ("kembali ke aplikasi tadi", ("focus_previous", {})), ("jendela sebelumnya", ("focus_previous", {})),
                             ("tutup semua kecuali firefox", ("close_others", {"keep": "firefox"})),
                             ("tutup semua aplikasi kecuali firefox dan terminal", ("close_others", {"keep": "firefox dan terminal"})),
                             ("buka terminal di folder project", ("open_terminal_in", {"folder": "project"})),
                             ("buka terminal di downloads", ("open_terminal_in", {"folder": "downloads"}))):
        assert brain._route(sentence) == action, (sentence, brain._route(sentence))
    # (hyprland only: _ws_route)
    if brain._route("pindah ke workspace 3"):
        assert brain._route("buka firefox di workspace 3") == ("open_on_workspace", {"name": "firefox", "workspace": "3"})
        assert brain._route("buka terminal di word ps 2") == ("open_on_workspace", {"name": "terminal", "workspace": "2"})
        assert brain._route("pindahkan semua ke workspace 2") == ("move_all_to_workspace", {"workspace": "2"})
        assert brain._route("pindahkan firefox ke workspace 2")[0] == "move_to_workspace"
        assert brain._route("buka workspace 2")[0] == "switch_workspace"
    # what must stay what it was
    assert brain._route("buka firefox")[0] != "side_by_side" and brain._route("tutup semua aplikasi") == ("close_all_apps", {})
    assert brain._route("buka kunci layar") == ("unlock_screen", {})

    windows = [{"hwnd": "0x1", "title": "Firefox", "pid": 10, "process": "firefox", "class": "firefox", "workspace": "2", "fullscreen": 0, "floating": False},
               {"hwnd": "0x2", "title": "kitty", "pid": 20, "process": "kitty", "class": "kitty", "workspace": "3", "fullscreen": 1, "floating": True},
               {"hwnd": "0x3", "title": "Catatan", "pid": 30, "process": "mousepad", "class": "Mousepad", "workspace": "2", "fullscreen": 0, "floating": False},
               {"hwnd": "0x4", "title": "DIGIDAW HUD", "pid": 40, "process": "chrome", "class": "chrome-hud", "workspace": "2", "fullscreen": 0, "floating": False}]
    calls = []
    real = (wm._windows, pc_control.ld.dispatch, pc_control.ld.active_workspace_id, pc_control.ld._hypr_json, pc_control.ld.focus, pc_control.time.sleep)
    active = {"id": 2}

    def dispatch(*args):
        calls.append(args)
        if args[:2] == ("workspace", "empty"):
            active["id"] = 9
        return True
    wm._windows, pc_control.ld.dispatch = (lambda: windows), dispatch
    pc_control.ld.active_workspace_id = lambda: active["id"]
    pc_control.ld.focus = lambda address: calls.append(("fokus", address)) or True
    pc_control.time.sleep = lambda s: None
    edges = {"0x1": 900, "0x2": 10}
    pc_control.ld._hypr_json = lambda what: {} if what == "activeworkspace" else [{"address": a, "at": [x, 0]} for a, x in edges.items()]
    try:
        said = pc_control.side_by_side("firefox", "kitty")
        assert "Firefox di kiri" in said, said
        assert ("workspace", "empty") in calls                                    # others on this workspace: the pair goes elsewhere
        assert ("movetoworkspacesilent", "9,address:0x1") in calls and ("movetoworkspacesilent", "9,address:0x2") in calls, calls
        assert ("settiled", "address:0x2") in calls and ("fullscreenstate", "0 0") in calls
        assert ("swapwindow", "l") in calls                                       # it came out on the wrong side
        assert "sama" in pc_control.side_by_side("firefox", "firefox")

        calls.clear()
        active["id"] = 2
        question = pc_control.close_others("firefox")
        assert question.startswith(safety.CONFIRMATION_TOKEN) and not calls       # only on "ya"
        assert "2 jendela" in safety.question() or "kecuali firefox" in safety.question()
        assert safety._pending["kwargs"]["addresses"] == ["0x2", "0x3"]           # not the HUD, not what's kept
        safety.cancel()
        assert "belum ada" in pc_control.close_others("spotify")                  # a name not there closes nothing
        assert not safety.has_pending()

        calls.clear()
        said = pc_control.move_all_to_workspace("5")
        assert "2 jendela" in said and ("movetoworkspacesilent", "5,address:0x3") in calls and ("workspace", "5") in calls, (said, calls)
        assert not any("0x4" in str(c) for c in calls)                            # the HUD stays
        assert "sudah di workspace" in pc_control.move_all_to_workspace("2")
    finally:
        wm._windows, pc_control.ld.dispatch, pc_control.ld.active_workspace_id, pc_control.ld._hypr_json, pc_control.ld.focus, pc_control.time.sleep = real
        safety.cancel()
    assert pc_control._terminal_folder("folder tidak ada sama sekali") is None
    assert pc_control._terminal_folder("home") == os.path.expanduser("~")
    root = Path(tempfile.mkdtemp())
    (root / "Project" / "digidaw").mkdir(parents=True)
    (root / "DIGIDAW").mkdir()
    real_home, os.environ["HOME"] = os.environ["HOME"], str(root)
    try:
        assert pc_control._terminal_folder("digidaw") == str(root / "Project" / "digidaw")     # not the other one in home
        assert pc_control._terminal_folder("folder DIGIDAW") in (str(root / "Project" / "digidaw"), str(root / "DIGIDAW"))
    finally:
        os.environ["HOME"] = real_home


def test_scripts_and_fan():
    import tempfile
    from pathlib import Path
    import config
    import pc_control
    import safety
    for sentence, action in (("fanset 50", ("fan_speed", {"percent": 50})), ("fan set 70", ("fan_speed", {"percent": 70})),
                             ("kipas 50 persen", ("fan_speed", {"percent": 50})), ("atur kipas ke 80%", ("fan_speed", {"percent": 80})),
                             ("kecepatan kipas 30", ("fan_speed", {"percent": 30})), ("kipas maksimal", ("fan_speed", {"percent": 100})),
                             ("kipas otomatis", ("fan_speed", {"auto": True})), ("matikan kipas", ("fan_speed", {"percent": 0})),
                             ("kipas berapa persen", ("fan_speed", {})), ("kecepatan kipas sekarang", ("fan_speed", {})),
                             ("buka skrip", ("open_script_manager", {})), ("buka pengelola skrip", ("open_script_manager", {})),
                             ("skrip saya", ("open_script_manager", {})), ("jalankan script", ("open_script_manager", {})),
                             ("jalankan skrip cek cpu", ("run_script", {"name": "cek cpu"}))):
        assert brain._route(sentence) == action, (sentence, brain._route(sentence))
    assert brain._route("atur power") == ("power_mode", {})                        # no mode said: asks
    assert brain._route("atur power ke performa") == ("power_mode", {"mode": "performa"})
    assert brain._route("1 power ke performa") == ("power_mode", {"mode": "performa"})
    assert "?" in pc_control.power_mode() and "tidak bisa" not in pc_control.power_mode()
    assert not any("atur power" in " ".join(words) for words in pc_control.POWER_MODES.values())
    assert "power" not in pc_control.POWER_MODES["power-saver"]                    # "atur power" once meant "hemat"
    powers, real_power = [], pc_control.ld.run
    pc_control.ld.run = lambda args, **k: powers.append(args) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    assert "performa" in pc_control.answer_about_power("performa", "Mode dayanya mau hemat, seimbang, atau performa, Tuan?")
    assert powers[-1][-1] == "performance", powers
    assert pc_control.answer_about_power("performa", "Lampunya mau apa?") is None
    assert pc_control.answer_about_power("buka firefox", "Mode dayanya mau apa?") is None
    pc_control.ld.run = real_power
    assert brain._route("buka buka firefox") == brain._route("buka firefox")
    assert brain._route("pindah ke pindah ke firefox") == brain._route("pindah ke firefox")
    import stt
    assert stt.dangling("buka") and stt.dangling("Tutup.") and stt.dangling("pindah ke")
    assert not stt.dangling("buka firefox") and not stt.dangling("mau tutup jendela")
    said = []
    real_announce, screen_agent.ANNOUNCE = screen_agent.ANNOUNCE, lambda text, level, listen=0, push=True: said.append(push)
    screen_agent._say("Yakin?", listen=10)
    screen_agent._say("Selesai")
    screen_agent.ANNOUNCE = real_announce
    assert said == [True, False], said                                             # only a question reaches the phone
    for closing in ("tutup skrip", "tutup script", "tutup screen", "tutup sc"):
        assert brain._route(closing) == ("close_script_window", {}), closing
    assert brain._route("hidupkan layar") == ("screen_power", {"on": True})
    for opens in ("buka sistem", "buka system", "Buka sistemnya", "tampilkan sistem"):
        assert brain._route(opens) == ("show_hud", {}), opens
    assert brain._route("tutup sistem") == ("hide_hud", {})
    assert brain._route("status sistem")[0] != "show_hud" and brain._route("analisis sistem")[0] != "show_hud"
    assert brain._route("matikan sistem")[0] == "shutdown_pc"
    assert brain._route("matikan layar") == ("screen_power", {"on": False})
    assert brain._search_route("berita elon musk hari ini") is None
    assert not local_router._makes_sense("power_mode", "mode", "hemat", "atur power", {})   # a setting never said
    assert local_router._makes_sense("power_mode", "mode", "performa", "atur ke mode performa", {})
    real_clients, closed = pc_control.ld._hypr_json, []
    pc_control.ld._hypr_json = lambda what: [{"title": "Pengelola skrip", "address": "0x1"}, {"title": "Skrip cekcpu.sh", "address": "0x2"},
                                             {"title": "Firefox", "address": "0x3"}, {"title": "Skrip kuliah.pdf", "address": "0x4"}]
    real_dispatch = pc_control.ld.dispatch
    pc_control.ld.dispatch = lambda *a: closed.append(a[1]) or True
    assert "tutup" in pc_control.close_script_window() and closed == ["address:0x1", "address:0x2"], closed
    pc_control.ld._hypr_json = lambda what: []
    assert "Tidak ada" in pc_control.close_script_window()
    pc_control.ld._hypr_json, pc_control.ld.dispatch = real_clients, real_dispatch
    for other in ("buka skripsi", "kenapa kipas kencang", "buka fan page"):
        assert (brain._route(other) or ("",))[0] not in ("fan_speed", "run_script", "open_script_manager"), other

    calls = []
    real = (pc_control.ld.dispatch, pc_control.ld.run, pc_control.shutil.which, config.SCRIPTS_DIR, config.CONFIRM_DANGEROUS_ACTIONS)
    pc_control.ld.dispatch = lambda *a: calls.append(a) or True
    pc_control.ld.run = lambda args, **k: calls.append(tuple(args)) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    pc_control.shutil.which = lambda name: "/bin/" + name
    config.CONFIRM_DANGEROUS_ACTIONS = True
    try:
        with tempfile.TemporaryDirectory() as folder:
            config.SCRIPTS_DIR = Path(folder)
            for name in ("atur.sh", "lib.sh", "cekcpu.sh", "cekjadwal.sh", "ssh.key.sh", "modeon.sh", "maintance.sh"):
                (Path(folder) / name).write_text("#!/bin/bash\n")
                (Path(folder) / name).chmod(0o755)

            def started():
                line = calls[-1][1] if calls else ""
                calls.clear()
                return line
            assert "cekcpu saya" in pc_control.run_script("cek cpu") and "cekcpu.sh" in (line := started()) and "--hold" in line
            assert "ssh key saya" in pc_control.run_script("SSH key") and "ssh.key.sh" in started()
            assert "Yang mana" in pc_control.run_script("cek") and not calls            # three of them: never a guess
            assert "cekjadwal saya" in brain._process_command("jadwal")[0] and "cekjadwal.sh" in started()   # the answer
            pc_control.run_script("cek")
            assert pc_control.answer_which_script("buka firefox") is None and not calls
            assert pc_control.answer_which_script("cpu") is None                         # asked once, answered once
            assert brain._route("jalankan cekcpu") == ("run_script", {"name": "cekcpu"})
            assert brain._route("jalanin mode on") == ("run_script", {"name": "mode on"})
            assert (brain._route("jalankan protokol kerja") or ("",))[0] != "run_script"
            for no_such in ("hapus semua", "lib"):                                       # the phrase is never a command
                pc_control.run_script(no_such)
                assert safety.has_pending() and not calls and "tidak ada" in safety.question(), no_such
                assert len(safety.question()) < 80                                        # no list of names read aloud
                safety.cancel()
            assert "cekcpu saya" in pc_control.run_script("jalankan skrip cek cpu") and started()   # the local brain's "name"
            assert "cekcpu saya" in pc_control.run_script("check CPU") and started()
            pc_control.run_script("cekcpu", unsure=True)
            assert safety.has_pending() and not calls and "cekcpu" in safety.question()
            safety.cancel()
            assert "cekcpu saya" in pc_control.answer_about_script("cekcpu", "Skrip yang mana, Tuan?") and started()
            assert pc_control.answer_about_script("cekcpu", "Lampunya mau warna apa?") is None
            assert pc_control.answer_about_script("buka firefox", "Skrip yang mana?") is None and not calls
            assert brain._route("jalankan check cpu") == ("run_script", {"name": "check cpu"})
            assert brain._route("terangkan cekcpu") == ("run_script", {"name": "cekcpu", "unsure": True})
            assert (brain._route("terangkan lampu") or ("",))[0] != "run_script"
            assert brain._route("buka sc") == brain._route("buka script") == ("open_script_manager", {})
            pc_control.run_script("mode on")
            assert "--hold" not in started() and not safety.has_pending()               # no terminal to close its apps
            assert "maintance saya" in pc_control.run_script("maintenance") and started()   # how it's really said
            pc_control.run_script("maintenans")                                          # misheard: asks first
            assert safety.has_pending() and not calls and "maintance" in safety.question() and ".sh" not in safety.question()
            safety.cancel()
            assert "Pengelola" in pc_control.run_script("atur") and "atur.sh" in started()
            calls.clear()
        pc_control.fan_speed(50)
        assert calls[-1] == ("nbfc", "set", "-s", "50"), calls
        calls.clear()
        assert "100" in pc_control.fan_speed(500) and not calls
        pc_control.fan_speed(0)
        assert safety.has_pending() and not calls                                         # off: only on "ya"
        safety.cancel()
        pc_control.fan_speed(auto=True)
        assert calls[-1] == ("nbfc", "set", "-a"), calls
    finally:
        pc_control.ld.dispatch, pc_control.ld.run, pc_control.shutil.which, config.SCRIPTS_DIR, config.CONFIRM_DANGEROUS_ACTIONS = real
        safety.cancel()


def test_sums():
    import calculator
    for sentence, result in (("berapa 20 * 30", "600"), ("7 + 7 + 8 + 9", "31"), ("10 + 10 berapa", "20"),
                             ("berapa 15 ditambah 8", "23"), ("hitung 100 dibagi 4", "25"), ("20 x 30", "600"),
                             ("25 persen dari 80", "20"), ("akar 144", "12"), ("2 pangkat 10", "1.024"),
                             ("7 dibagi 2", "3,5"), ("1.000 kali 3", "3.000"), ("berapa 10 kurang 15", "-5")):
        action = brain._route(sentence)
        assert action and action[0] == "calculate", (sentence, action)
        assert calculator.solve(action[1]["sum_said"]) == result, (sentence, action)
    assert calculator.solve("5 dibagi 0") is None and calculator.solve("9 pangkat 9 pangkat 9") is None
    for other in ("volume 20", "timer 5 menit", "jam 10", "pindah ke ws 2", "buka 2 terminal", "kecerahan 50 persen",
                  "ingatkan jam 5 kurang 10 menit", "__import__ + 1", "sqrt + 1"):
        assert (brain._route(other) or ("",))[0] != "calculate", other


def test_pick_is_forced_to_the_list():
    asked = _answers('{"pilihan": "lamp_color"}')
    assert local_router.pick("lampunya biru dong") == "lamp_color"
    choices = asked[0]["schema"]["properties"]["pilihan"]["enum"]
    assert "lamp_color" in choices and local_router.AI in choices and local_router.ASK in choices
    assert all(n in brain.TOOL_MAP for n in local_router.CATALOG)         # every choice is a real skill
    asked = _answers('{"pilihan": "volume_up"}')
    local_router.pick("lagi", ("naikkan volume", "Volume naik."), [("gedein dong", "volume_up")])
    assert "Sebelumnya user: naikkan volume" in asked[0]["user"] and '"gedein dong" -> volume_up' in asked[0]["user"]


def test_arguments_are_grounded():
    def lamp_power(on: bool = True, name: str = ""): ...
    def type_text(text: str, press_enter: bool = False): ...
    def open_website(site: str, browser: str = ""): ...
    def set_brightness(level: int): ...
    def find_files(name: str, location: str = ""): ...
    def play_youtube(query: str): ...
    _answers('{"on": false, "name": "lamp_power"}')
    assert local_router.fill_args(lamp_power, "matiin lampunya") == {"on": False}        # off stays off; invented name goes
    _answers('{"text": "halo", "press_enter": true}')
    assert local_router.fill_args(type_text, "tulis halo") == {"text": "halo"}           # no Enter nobody asked for
    _answers('{"text": "halo", "press_enter": false}')
    assert local_router.fill_args(type_text, "ketik halo lalu enter")["press_enter"] is True
    _answers('{"site": "gitar", "browser": "firefox"}')
    assert local_router.fill_args(open_website, "buka situs gitar") == {"site": "gitar"}
    _answers('{"level": 1}')
    assert local_router.fill_args(set_brightness, "layarnya terlalu terang") is None     # no number said: not a guess
    _answers('{"level": 30}')
    assert local_router.fill_args(set_brightness, "kecerahan tiga puluh") == {"level": 30}
    _answers('{"name": "find_files"}')
    assert local_router.fill_args(find_files, "cari file skripsi") is None               # it repeated its own name
    _answers('{"query": "putar lagu-lagu Tulus"}')
    assert local_router.fill_args(play_youtube, "aku pengen denger lagu galau") is None  # (holdout exam) a singer nobody named
    _answers('{"query": "lagu galau"}')
    assert local_router.fill_args(play_youtube, "aku pengen denger lagu galau") == {"query": "lagu galau"}
    _answers('{"query": "lagu tenang"}')
    assert local_router.fill_args(play_youtube, "putar lagunya yang tenangnya itu") == {"query": "lagu tenang"}
    # what real sentences turned out to do
    def write_note(content: str, title: str = ""): ...
    def open_application(name: str): ...
    def wifi_connect(name: str): ...
    def set_reminder_at(time_hhmm: str, message: str): ...
    _answers('{"level": 0}')
    assert local_router.fill_args(set_brightness, "atur kecerahan di 50%") is None       # brightness 0 was never said
    _answers('{"level": 50}')
    assert local_router.fill_args(set_brightness, "atur kecerahan di 50%") == {"level": 50}
    _answers('{"content": "Buatkan pantun"}')
    assert local_router.fill_args(write_note, "buatkan pantun") is None                  # the sentence isn't the note
    _answers('{"content": "ide channel"}')
    assert local_router.fill_args(write_note, "catat ide channel") == {"content": "ide channel"}
    for sentence in ("buka dikit dong", "langsung buka", "Buka aplikasi"):
        _answers(json.dumps({"name": sentence.lower()}))
        assert local_router.fill_args(open_application, sentence) is None, sentence
    _answers('{"name": "firefox"}')
    assert local_router.fill_args(open_application, "firefox") == {"name": "firefox"}    # one word may be all of it
    _answers('{"name": "connect_wifi_reconnect"}')
    assert local_router.fill_args(wifi_connect, "coba sambungkan lagi") is None
    _answers('{"name": "kantor"}')
    assert local_router.fill_args(wifi_connect, "sambungin wifi kantor") == {"name": "kantor"}
    _answers('{"name": "rumah"}')
    assert local_router.fill_args(wifi_connect, "sambungin wifi kantor") is None         # a network nobody named
    _answers('{"time_hhmm": "21:00", "message": "ke WordPress 2"}')
    assert local_router.fill_args(set_reminder_at, "ingatkan Firefox ke WordPress") is None  # no time was said
    _answers('{"query": "putar lagu yang tenang"}')
    assert local_router.fill_args(play_youtube, "putar lagu yang tenang") == {"query": "lagu yang tenang"}


def test_routed_brain_acts_or_hands_over():
    ran = []

    def set_timer(minutes: float, label: str = ""):
        ran.append(minutes)
        return f"Timer {minutes:g} menit diset."
    real = (brain.TOOL_MAP.get("set_timer"), local_llm.available, brain._dynamic_context)
    brain.TOOL_MAP["set_timer"], local_llm.available = set_timer, (lambda refresh=False: True)
    brain._dynamic_context = lambda text, active=False: "[Konteks]"
    brain._dialog.clear()
    experience.FILE, experience._items = Path(tempfile.mkdtemp()) / "e.json", []
    try:
        _answers('{"pilihan": "set_timer"}', '{"minutes": 3}')
        assert brain.ask_local("timer masak mie 3 menit") == "Timer 3 menit diset." and ran == [3]
        _answers(f'{{"pilihan": "{local_router.AI}"}}')
        assert brain.ask_local("harga emas hari ini") is None                            # the online AI's job
        _answers(f'{{"pilihan": "{local_router.KNOW}"}}', "Karena cahaya biru paling mudah tersebar.")
        assert "tersebar" in brain.ask_local("kenapa langit biru")                       # an explanation: said here
        _answers(f'{{"pilihan": "{local_router.AI}"}}')
        assert brain.ask_local("analisis layar", tools_only=True) is None
        _answers(f'{{"pilihan": "{local_router.CHAT}"}}')                                 # a big-job word and no tool: not chat
        assert brain.ask_local("analisis ini dong", tools_only=True) is None
        _answers(f'{{"pilihan": "{local_router.KNOW}"}}')
        assert brain.ask_local("apa rumus luas lingkaran", tools_only=True) is None
        _answers('{"pilihan": "set_timer"}', '{"minutes": 3}')                            # the word doesn't stop a plain command
        assert brain.ask_local("timer masak mie 3 menit", tools_only=True) == "Timer 3 menit diset."
        _answers(f'{{"pilihan": "{local_router.ASK}"}}')
        assert "kurang jelas" in brain.ask_local("daun meja piring")
        _answers(f'{{"pilihan": "{local_router.ASK}"}}')                                  # someone explaining something
        assert brain.ask_local("kenapa temanku selama empat tahun selalu cemburu") is None
        _answers('{"pilihan": "do_on_screen"}')                                           # a job: the online AI decides
        assert brain.ask_local("perbaiki lagi tidak bisa diklik") is None
        _answers(f'{{"pilihan": "{local_router.CHAT}"}}', "Sudah saya kirim pesannya.")
        assert brain.ask_local("halo") is None                                           # a deed claimed in words: no
        _answers(f'{{"pilihan": "{local_router.CHAT}"}}', "Halo juga.")
        assert brain.ask_local("halo") == "Halo juga."
    finally:
        brain.TOOL_MAP["set_timer"], local_llm.available, brain._dynamic_context = real


def test_knowledge_sheet():
    # the question decides: an unknown word means the sheet isn't about it
    assert "Pontianak" in knowledge.find("ibu kota kalimantan barat apa")[0]
    assert "Bandung" in knowledge.find("ibukota jabar apa")[0]
    assert "Habibie" in knowledge.find("presiden ke-3 siapa")[0] and "Habibie" in knowledge.find("siapa presiden ketiga indonesia")[0]
    assert "Adam Malik" in knowledge.find("siapa wakil presiden ketiga indonesia")[0]        # not the president of the same number
    assert "Andrea Hirata" in knowledge.find("laskar pelangi karya siapa")[0]
    assert "Jupiter" in knowledge.find("planet terbesar di tata surya apa")[0]
    assert "22 Desember" in knowledge.find("kapan hari ibu")[0]
    for elsewhere in ("ibu kota narnia apa", "siapa presiden amerika pertama wanita", "siapa nama saya", "siapa ibu saya", "siapa penemu kucing", "apa itu gravitasi", "kenapa besi berkarat", "halo"):
        assert not knowledge.find(elsewhere), elsewhere
    assert knowledge.agreed(["Bandar Lampung", "Kota Bandar Lampung\nLampung", "bandar lampung"]) == "Bandar Lampung"
    assert knowledge.agreed(["Jawa Timur", "Jawa Tengah", "Jawa Timur"]) == ""               # "jawa" in common isn't agreeing
    assert knowledge.agreed(["Tidak ada", "Tidak ada", "Tidak ada"]) == ""                   # agreeing that it can't say
    assert knowledge.said_in("Makassar", "Ibu kota itu adalah Makassar.") and not knowledge.said_in("0", "Tidak ada jawaban yang benar.")
    assert knowledge.AGREES_WRONGLY_RE.search("organ terbesar tubuh manusia apa") and knowledge.AGREES_WRONGLY_RE.search("apa bahasa jawa makan")
    assert not knowledge.AGREES_WRONGLY_RE.search("apa bahasa inggrisnya kucing")


def test_knowledge_answer_routes():
    real = local_llm.available
    local_llm.available = lambda refresh=False: True
    calls = []

    def answers(*said):
        queue = list(said)

        def generate(system, user, **kwargs):
            calls.append(system)
            return queue.pop(0)
        local_llm.generate = generate
    real_generate = local_llm.generate
    try:
        answers("Ibu kota Jawa Barat (provinsi) adalah Bandung.")
        assert brain._know_answer("ibu kota jawa barat apa", False) == "Ibu kota Jawa Barat adalah Bandung."     # from the sheet, one call
        assert brain._know_stats["how"] == "lembar fakta"
        answers("Canberra", "Canberra", "Canberra", "BENAR", "Ibu kota Vanuatu adalah Canberra.")
        assert "Canberra" in brain._know_answer("ibu kota vanuatu apa", False)                                   # agreed and stood by
        answers("Port Vila", "Luganville", "Port Vila")
        assert brain._know_answer("ibu kota vanuatu apa", False) is None                                         # three different stories: the online AI
        answers("Port Vila", "Port Vila", "Port Vila", "SALAH")
        assert brain._know_answer("ibu kota vanuatu apa", False) is None                                         # it doesn't stand by it
        answers("Port Vila", "Luganville", "Port Vila", "Ibu kotanya Port Vila.")
        said = brain._know_answer("ibu kota vanuatu apa", True)                                                  # no online AI: an answer, marked
        assert said.startswith(brain.persona._pronouns("Setahu saya")) and "port vila" in said.lower()
        answers("0", "0", "0", "BENAR", "Tidak ada jawaban yang benar.")
        assert brain._know_answer("berapa titik beku air murni", False) == "0."                                  # the sentence lost the answer
        calls.clear()
        assert brain._know_answer("organ terbesar kedua tubuh manusia apa", False) is None and not calls         # (agrees with itself and is wrong on these)
        assert brain._spoken_answer("Jawaban: Tokyo (Jepang).") == "Tokyo."
        assert brain._spoken_answer("Satu. Dua. Tiga yang dikarang.") == "Satu. Dua."
        assert brain._know_answer("buatkan pantun lucu", False) is None and not calls                            # written better online
    finally:
        local_llm.available, local_llm.generate = real, real_generate


def test_follow_ups_become_whole_sentences():
    real = (local_llm.available, local_llm.generate)
    local_llm.available = lambda refresh=False: True
    now = time.time()
    told = []

    def generate(system, user, **kwargs):
        told.append(user)
        return generate.reply
    local_llm.generate = generate
    brain._last_fast.update(tool="", kwargs={}, at=0)
    try:
        generate.reply = "cuaca besok"
        brain._dialog[:] = [{"user": "cuaca hari ini", "reply": "Cerah.", "brain": "local", "at": now}]
        assert brain._expand_follow_up("kalau besok") == "cuaca besok" and brain._understood_as == "cuaca besok"
        told.clear()
        generate.reply = "Sebelumnya: cuaca hari ini"
        assert brain._expand_follow_up("kalau besok") == "kalau besok" and told                                  # it echoed: as it was
        generate.reply = "Cuaca besok adalah cerah"
        assert brain._expand_follow_up("kalau besok") == "kalau besok"                                           # it answered: as it was
        told.clear()
        for not_one in ("yang benar saja", "kalau begitu oke", "terus kenapa", "buka firefox", "kalau besok saya ada ujian penting sekali", "terus", "yang itu"):
            assert brain._expand_follow_up(not_one) == not_one
        assert not told, told
        brain._dialog[:] = [{"user": "cuaca hari ini", "reply": "Cerah.", "brain": "local", "at": now - 300}]
        assert brain._expand_follow_up("kalau besok") == "kalau besok" and not told                              # too long ago
        brain._dialog.clear()
        assert brain._expand_follow_up("kalau besok") == "kalau besok"
        brain._dialog[:] = [{"user": "putar lagu coldplay", "reply": "Memutar.", "brain": "fast", "at": now}]
        brain._last_fast.update(tool="play_youtube", kwargs={}, at=now)
        assert brain._expand_follow_up("yang lain") == "lagu berikutnya"
    finally:
        local_llm.available, local_llm.generate = real
        brain._dialog.clear()
        brain._last_fast.update(tool="", kwargs={}, at=0)
        brain._understood_as = ""


def test_undo_only_the_alarm_just_set():
    import alarms
    import datetime
    real = (alarms.STATE_FILE, list(alarms._items), dict(alarms._loaded))
    alarms.STATE_FILE = Path(tempfile.mkdtemp()) / "alarms.json"
    alarms._items.clear()
    try:
        old = alarms.add("alarm", "bangun", datetime.datetime.now() + datetime.timedelta(hours=9))
        old["created"] = time.time() - 3600
        assert not alarms.just_added() and "Tidak ada" in alarms.undo_last_alarm()
        alarms.add("timer", "", datetime.datetime.now() + datetime.timedelta(minutes=5))
        assert alarms.just_added() and brain._route("batalin") == ("undo_last_alarm", {}) and brain._route("gak jadi") == ("undo_last_alarm", {})
        assert "dibatalkan" in alarms.undo_last_alarm()
        assert [i["label"] for i in alarms._items] == ["bangun"]                                                 # the morning alarm stays
        assert brain._route("batalin")[0] == "say"
    finally:
        alarms.STATE_FILE = real[0]
        alarms._items[:] = real[1]
        alarms._loaded.clear()
        alarms._loaded.update(real[2])


def test_learning_from_the_teacher():
    experience.FILE, experience._items = Path(tempfile.mkdtemp()) / "e.json", []
    experience.note("pindah ke wasit lost", "focus_window", {"name": "whatsapp"})
    assert experience.similar("pindah ke wasit los") == []                                 # once could be a mistake
    experience.note("pindah ke wasit lost", "focus_window", {"name": "whatsapp"})
    experience.note("balas ke rina oke", "reply_message", {"text": "oke"})                 # messages are never kept
    experience.note("tik", "open_website", {"site": "tiktok"})                             # one word: too often misheard
    assert [i["tool"] for i in experience.similar("pindah ke wasit los")] == ["focus_window"]
    assert experience.similar("harga emas") == [] and experience.count() == 1
    assert experience.FILE.stat().st_mode & 0o777 == 0o600
    experience.forget("Pindah ke wasit lost!")                                             # corrected: unlearned
    assert experience.count() == 0


def _fake_screen(steps):
    """screen_agent with a fake AI giving these steps, a fake screen and a fake mouse. Returns what was done, said."""
    done, said = [], []
    queue = list(steps)
    screen_agent._decide = lambda task, history, image: queue.pop(0) if queue else {"aksi": "selesai", "langkah": "", "teks": "Beres."}
    screen_agent._aim = lambda step: None
    pc_control.front_window = lambda: ""
    pc_control.changed_near = lambda x, y: False
    pc_control.capture = lambda max_width=0, hide_own=False: (b"jpeg", {"x": 1920, "y": 1080, "width": 1920, "height": 1080, "scale": 1})
    pc_control.click = lambda x, y, button="left", double=False: done.append(("klik", x, y, button, double))
    pc_control.scroll = lambda amount, x=None, y=None: done.append(("gulir", amount))
    sc.type_text = lambda text, press_enter=False: done.append(("ketik", text, press_enter))
    sc.press_keys = lambda keys: done.append(("tekan", keys)) or "ok"
    screen_agent.ANNOUNCE = lambda text, level="info", **kw: said.append(text) or True
    screen_agent.SETTLE = 0
    screen_agent._locked = lambda: False
    screen_agent._cancel.clear()
    return done, said


def _wait(condition, seconds=3):
    end = time.time() + seconds
    while not condition() and time.time() < end:
        time.sleep(0.01)
    assert condition()


def test_screen_job_steps():
    done, said = _fake_screen([
        {"aksi": "klik", "x": 500, "y": 500, "langkah": "klik kotak cari", "berisiko": False},
        {"aksi": "ketik", "teks": "harga rtx", "enter": True, "langkah": "ketik pencarian", "berisiko": False},
        {"aksi": "gulir", "jumlah": -3, "langkah": "gulir ke bawah", "berisiko": False},
        {"aksi": "selesai", "langkah": "terlihat", "teks": "Hasilnya sudah terbuka."}])
    assert screen_agent._run("cari harga rtx") == "Hasilnya sudah terbuka."
    assert done == [("klik", 2880, 1620, "left", False), ("ketik", "harga rtx", True), ("gulir", -3)]    # layout position


def test_closer_look_before_a_click():
    from PIL import Image
    import llm
    real = (screen_agent._aim, llm.generate)
    screen_agent._aim = REAL_AIM
    pc_control._latest["picture"] = Image.new("RGB", (1920, 1080))
    picture, back = pc_control.closer_look(500, 20)                       # near the top edge: the crop stays on the picture
    assert picture and Image.open(__import__("io").BytesIO(picture)).width == pc_control.CAPTURE_WIDTH
    x, y = back(500, 0)
    assert abs(x - 500) < 0.5 and y == 0 and abs(back(0, 1000)[1] - 1000 / 3) < 0.5
    assert abs(pc_control.closer_look(990, 500)[1](1000, 0)[0] - 1000) < 0.5              # the right edge too
    asked = []

    def fake_generate(contents, system_instruction=None, max_tokens=600, response_schema=None, temperature=0.4):
        asked.append(contents[1])
        return json.dumps(answers.pop(0))
    llm.generate = fake_generate
    try:
        answers = [{"ada": True, "x": 500, "y": 500, "tulisan": "Simpan"}]
        step = {"aksi": "klik", "x": 500, "y": 500, "sasaran": "tombol Simpan", "langkah": "klik"}
        REAL_AIM(step)
        assert asked[-1] == "Sasaran: tombol Simpan" and step["tulisan"] == "Simpan" and abs(step["x"] - 500) < 1
        answers = [{"ada": True, "x": 0, "y": 0, "tulisan": ""}]
        step = {"aksi": "klik", "x": 500, "y": 500, "sasaran": "ikon"}
        REAL_AIM(step)                                                     # corrected to the crop's corner: a third of the screen
        assert abs(step["x"] - (500 - 1000 / 6)) < 1 and abs(step["y"] - (500 - 1000 / 6)) < 1
        answers = [{"ada": False}]
        step = {"aksi": "klik", "x": 500, "y": 500, "sasaran": "ikon"}
        REAL_AIM(step)
        assert step["tidak_ada"]
    finally:
        screen_agent._aim, llm.generate = real


def test_click_target_not_there_or_risky_label():
    done, said = _fake_screen([{"aksi": "klik", "x": 5, "y": 5, "sasaran": "tombol X", "langkah": "klik X"}] * 9)
    screen_agent._aim = lambda step: step.update(tidak_ada=True)
    assert "tidak menemukan tombol X" in screen_agent._run("klik X") and not done        # never clicks blindly
    # the AI calls it harmless, but the button under the pointer says "Hapus": the label asks first
    done, said = _fake_screen([{"aksi": "klik", "x": 5, "y": 5, "sasaran": "tombol merah", "langkah": "klik tombol merah"}])
    screen_agent._aim = lambda step: step.update(tulisan="Hapus akun")
    _answer_when_asked(False)
    assert "tidak saya lakukan" in screen_agent._run("rapikan") and not done


def test_scale_and_position():
    monitor = {"x": 100, "y": 50, "width": 2880, "height": 1800, "scale": 2}
    assert pc_control.to_layout(monitor, 0, 0) == (100, 50)
    assert pc_control.to_layout(monitor, 1000, 500) == (100 + 1440, 50 + 450)
    assert pc_control.to_layout(monitor, 5000, -3) == (1540, 50)                         # outside the picture: its edge


def _answer_when_asked(yes: bool):
    def run():
        _wait(safety.has_pending)
        safety.confirm() if yes else safety.cancel()
    threading.Thread(target=run, daemon=True).start()


def test_risky_steps_wait_for_yes():
    # the AI didn't flag it, but the words say it sends something
    done, said = _fake_screen([{"aksi": "klik", "x": 10, "y": 10, "langkah": "klik tombol Kirim", "berisiko": False},
                               {"aksi": "selesai", "langkah": "", "teks": "Terkirim."}])
    _answer_when_asked(False)
    assert "tidak saya lakukan" in screen_agent._run("kirim pesan") and not done
    assert any("Yakin" in s for s in said)
    done, said = _fake_screen([{"aksi": "klik", "x": 10, "y": 10, "langkah": "klik tombol Kirim", "berisiko": True},
                               {"aksi": "selesai", "langkah": "", "teks": "Terkirim."}])
    _answer_when_asked(True)
    assert screen_agent._run("kirim pesan") == "Terkirim." and len(done) == 1


def test_screen_job_limits():
    same = {"aksi": "klik", "x": 1, "y": 1, "langkah": "klik sesuatu", "berisiko": False}
    done, said = _fake_screen([same] * 10)
    assert "macet" in screen_agent._run("x") and len(done) == 2                          # the same step: not forever
    done, said = _fake_screen([{"aksi": "ketik", "teks": "rahasia123", "langkah": "ketik password", "berisiko": False},
                               {"aksi": "selesai", "langkah": "", "teks": "ok"}])
    assert screen_agent._run("masuk") == "ok" and not done                               # never types a password
    done, said = _fake_screen([same])
    screen_agent._cancel.set()
    assert "dihentikan" in screen_agent._run("x") and not done                           # Esc


def test_esc_while_the_ai_is_thinking():
    """Seen in real use: Esc at 13:43:00, the AI answered at 13:43:13, and that step was still carried out."""
    done, said = _fake_screen([])
    real_decide = {"aksi": "klik", "x": 5, "y": 5, "langkah": "klik sesuatu", "sasaran": "x"}

    def slow_answer(task, history, image):
        screen_agent.cancel()                                           # Esc, while this "request" is on its way
        return real_decide
    screen_agent._decide = slow_answer
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    screen_agent._job["thread"] = threading.current_thread()            # (running() needs a live thread)
    assert "dihentikan" in screen_agent._run("x") and not done
    assert screen_agent.cancel()                                        # Esc again straight after: still about this
    screen_agent._job["stopped_at"] = 0.0
    assert not screen_agent.running() and not screen_agent.cancel()


def test_new_job_replaces_old_one_and_new_command_stops_it():
    done, said = _fake_screen([])
    screen_agent.AI_RETRY_SECONDS = 0
    release, seen = threading.Event(), []

    def thinking(task, history, image):                                 # the old job's AI answers late
        seen.append(task)
        if task == "lama":
            release.wait(3)
            return {"aksi": "klik", "x": 5, "y": 5, "langkah": "klik", "sasaran": "tombol lama"}
        return {"aksi": "selesai", "langkah": "", "teks": "Yang baru beres."}
    screen_agent._decide = thinking
    screen_agent._start("lama")
    _wait(lambda: "lama" in seen)
    assert screen_agent.running()
    reply = screen_agent._start("baru")
    assert "tugas tadi saya hentikan" in reply
    _wait(lambda: any("baru beres" in s for s in said))
    release.set()
    time.sleep(0.05)
    assert not done and not any("dihentikan" in s for s in said)        # the old one neither clicked nor spoke

    # a command that changes the screen stops a job; one that only reads doesn't
    done, said = _fake_screen([])
    release = threading.Event()
    screen_agent._decide = lambda task, history, image: release.wait(3) and {"aksi": "selesai", "langkah": "", "teks": "ok"}
    calls = []
    window_tool = brain._logged(lambda **kw: calls.append(kw) or "ok")
    window_tool.__wrapped__.__name__ = "x"
    def open_website(site=""):
        return "ok"
    def get_time():
        return "jam 10"
    screen_agent._start("kerja")
    _wait(screen_agent.running)
    brain._logged(get_time)()
    assert screen_agent.running()                                       # "jam berapa" doesn't disturb a job
    brain._logged(open_website)(site="youtube")
    assert not screen_agent.running() and brain._job_stopped_now
    brain._job_stopped_now.clear()
    release.set()


def test_no_step_on_an_old_picture():
    click = {"aksi": "klik", "x": 500, "y": 500, "langkah": "klik", "sasaran": "tombol"}
    finish = {"aksi": "selesai", "langkah": "", "teks": "Beres."}
    # the window in front changed while the AI was thinking: not clicked, looked again
    done, said = _fake_screen([click, finish])
    windows = iter(["jendela-a", "jendela-b", "jendela-b", "jendela-b"])
    pc_control.front_window = lambda: next(windows)
    assert screen_agent._run("x") == "Beres." and not done
    # the place to click looks different now: not clicked
    done, said = _fake_screen([click, finish])
    changes = iter([True, False])
    pc_control.changed_near = lambda x, y: next(changes)
    assert screen_agent._run("x") == "Beres." and not done
    # it keeps moving: the job ends instead of guessing
    done, said = _fake_screen([dict(click, x=100 * i) for i in range(1, 9)])
    pc_control.changed_near = lambda x, y: True
    assert "sudah berubah" in screen_agent._run("x") and not done
    # still the same: clicked
    done, said = _fake_screen([click, finish])
    assert screen_agent._run("x") == "Beres." and len(done) == 1
    # the user touched the mouse meanwhile: their move, no step
    done, said = _fake_screen([click, finish])
    screen_agent.CALM_SECONDS = 0
    presence._watching = True
    real_decide = screen_agent._decide

    def touched(task, history, image):
        presence._last_input = time.time() + 0.05
        return real_decide(task, history, image)
    screen_agent._decide = touched
    time.sleep(0.01)
    assert screen_agent._run("x") == "Beres." and not done
    presence._watching = False
    screen_agent.CALM_SECONDS = 3


def test_user_busy_stops_the_job():
    done, said = _fake_screen([{"aksi": "klik", "x": 5, "y": 5, "langkah": "klik"}])
    screen_agent.USER_BUSY_WAIT, real_wait = 0.1, screen_agent.USER_BUSY_WAIT
    presence._watching, presence._last_input = True, time.time() + 60          # still typing
    try:
        assert "sedang memakai komputer" in screen_agent._run("x") and not done
    finally:
        presence._watching, presence._last_input = False, 0.0
        screen_agent.USER_BUSY_WAIT = real_wait


def test_changed_near():
    from PIL import Image, ImageDraw
    before = Image.new("RGB", (1920, 1080), (20, 30, 40))
    ImageDraw.Draw(before).rectangle((900, 500, 1020, 560), fill=(200, 200, 200))
    pc_control._latest["picture"] = before
    real = (pc_control._grab, pc_control.focused_monitor)
    pc_control.focused_monitor = lambda: {"name": "x"}
    try:
        pc_control._grab = lambda monitor: before.copy()
        assert not REAL_CHANGED_NEAR(500, 500)                    # nothing changed
        moved = before.copy()
        ImageDraw.Draw(moved).rectangle((0, 0, 1920, 1080), fill=(250, 250, 250))
        pc_control._grab = lambda monitor: moved
        assert REAL_CHANGED_NEAR(500, 500)                        # another page
        far = before.copy()
        ImageDraw.Draw(far).rectangle((0, 0, 300, 200), fill=(250, 250, 250))
        pc_control._grab = lambda monitor: far
        assert not REAL_CHANGED_NEAR(500, 500)                    # something moved elsewhere (a video, a clock)
        blink = before.copy()
        ImageDraw.Draw(blink).rectangle((958, 528, 962, 532), fill=(0, 0, 0))
        pc_control._grab = lambda monitor: blink
        assert not REAL_CHANGED_NEAR(500, 500)                    # a blinking cursor is no change
    finally:
        pc_control._grab, pc_control.focused_monitor = real


def test_busy_ai_is_waited_out():
    done, said = _fake_screen([])
    screen_agent.AI_RETRY_SECONDS = 0
    calls = {"n": 0}

    def busy_twice(task, history, image):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("server AI Google sedang sibuk")
        return {"aksi": "selesai", "langkah": "", "teks": "Beres."}
    screen_agent._decide = busy_twice
    assert screen_agent._run("x") == "Beres." and sum("sibuk" in s for s in said) == 1      # told once, not at each try
    screen_agent._decide = lambda task, history, image: (_ for _ in ()).throw(RuntimeError("sibuk"))
    assert "Coba lagi" in screen_agent._run("x")                                            # not forever either


def test_ask_user_then_go_on():
    done, said = _fake_screen([{"aksi": "tanya", "teks": "Tolong isi kata sandinya.", "langkah": "minta sandi", "berisiko": False},
                               {"aksi": "selesai", "langkah": "", "teks": "Sudah masuk."}])
    results = []
    screen_agent.USER_WAIT = 5
    worker = threading.Thread(target=lambda: results.append(screen_agent._run("masuk")))
    screen_agent._resume.clear()
    worker.start()
    screen_agent._job["thread"] = worker          # (what do_on_screen does)
    _wait(lambda: any("kata sandinya" in s for s in said))
    assert screen_agent.waiting()
    assert brain.RESUME_RE.fullmatch("sudah") and brain.RESUME_RE.fullmatch("oke lanjut") and not brain.RESUME_RE.fullmatch("sudah makan")
    assert "lanjutkan" in screen_agent.resume_task()
    worker.join(3)
    assert results == ["Sudah masuk."] and not screen_agent.waiting()


def test_read_only_commands_need_no_yes():
    assert sc._read_only_command("df -h") and sc._read_only_command("pacman -Qi firefox")
    assert sc._read_only_command("systemctl --user status digidaw")
    for risky in ("rm -rf /", "df -h; rm x", "cat .env", "cat ~/.ssh/id_rsa", "pacman -Syu", "systemctl restart x",
                  "ls > a", "git push", "echo $(whoami)", "sudo ls"):
        assert not sc._read_only_command(risky), risky


def test_files():
    home = Path(tempfile.mkdtemp())
    real = (file_tools.ld.known_folder, file_tools.Path.home)
    file_tools.ld.known_folder = lambda name: home / name.capitalize()
    (home / "Downloads").mkdir()
    (home / "Documents").mkdir()
    (home / "Downloads" / "laporan.txt").write_text("isi laporan")
    (home / "Downloads" / ".env").write_text("KUNCI=rahasia")
    try:
        assert "laporan.txt" in file_tools.list_folder("unduhan") and ".env" not in file_tools.list_folder("unduhan")
        assert file_tools.read_text_file("unduhan/laporan.txt") == "isi laporan"
        assert "rahasia, jadi tidak" in file_tools.read_text_file("unduhan/.env")
        assert "disalin" in file_tools.copy_file("unduhan/laporan.txt", "dokumen")
        assert "tidak saya timpa" in file_tools.copy_file("unduhan/laporan.txt", "dokumen")      # never overwritten
        assert "bernama" in file_tools.rename_file("dokumen/laporan.txt", "akhir.txt")
        assert "dipindahkan" in file_tools.move_file("unduhan/laporan.txt", "dokumen")
        assert sorted(p.name for p in (home / "Documents").iterdir()) == ["akhir.txt", "laporan.txt"]
        assert "tidak saya ubah" in file_tools.move_file("dokumen/akhir.txt", str(config.BASE_DIR))
        assert "tidak saya ubah" in file_tools.move_file("dokumen/akhir.txt", "/etc")
    finally:
        file_tools.ld.known_folder = real[0]


class _Done:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


def test_settings():
    calls = []
    real = (pc_control.ld.run, pc_control.ld.out, pc_control.ld.dispatch)
    pc_control.ld.run = lambda args, timeout=5, input=None: calls.append(args) or _Done()
    pc_control.ld.out = lambda args, timeout=5: {
        ("bluetoothctl", "devices"): "Device AA:BB Sony WH-1000\nDevice CC:DD Speaker Kamar",
        ("pactl", "-f", "json", "list", "sinks"): json.dumps([{"name": "hdmi", "description": "HDMI 1"},
                                                              {"name": "lap", "description": "Audio Speaker"}]),
        ("pactl", "-f", "json", "list", "sink-inputs"): "[]"}.get(tuple(args), "")
    try:
        result = pc_control.wifi_power(False)                     # off cuts Digidaw's own hearing: asks first
        assert result.startswith(safety.CONFIRMATION_TOKEN) and not calls
        assert "dimatikan" in safety.confirm() and ["nmcli", "radio", "wifi", "off"] in calls
        assert "tersambung" in pc_control.bluetooth("connect", "sony") and ["bluetoothctl", "connect", "AA:BB"] in calls
        assert "Yang dikenal" in pc_control.bluetooth("connect", "kulkas")
        assert "hemat daya" in pc_control.power_mode("hemat baterai") and ["powerprofilesctl", "set", "power-saver"] in calls
        assert "performa" in pc_control.power_mode("gaming")
        assert "belum saya kenal" in pc_control.power_mode("ajaib")
        assert "Speaker" in pc_control.audio_output("speaker laptop") and ["pactl", "set-default-sink", "lap"] in calls
    finally:
        pc_control.ld.run, pc_control.ld.out, pc_control.ld.dispatch = real


def test_control_routes():
    cases = {"matikan wifi": ("wifi_power", {"on": False}), "nyalakan wi fi": ("wifi_power", {"on": True}),
             "matikan bluetooth": ("bluetooth", {"action": "off"}),
             "sambungkan bluetooth ke sony": ("bluetooth", {"action": "connect", "device": "sony"}),
             "sambungkan headset": ("bluetooth", {"action": "connect", "device": "headset"}),
             "mode hemat baterai": ("power_mode", {"mode": "hemat baterai"}), "jangan ganggu": ("do_not_disturb", {"on": True}),
             "matikan mode jangan ganggu": ("do_not_disturb", {"on": False}), "suara ke hdmi": ("audio_output", {"name": "hdmi"}),
             "matikan layar": ("screen_power", {"on": False}), "klik tombol download": ("click_on_screen", {"target": "tombol download"}),
             "hentikan tugas": ("stop_task", {}), "isi folder unduhan": ("list_folder", {"path": "unduhan"})}
    for said, expected in cases.items():
        assert brain._route(said) == expected, (said, brain._route(said))
    assert brain._route("tekan enter")[0] == "press_keys"
    assert brain._route("klik enter") == ("press_keys", {"keys": "enter"})        # (said in the real log) a key, not a target
    assert brain._route("tekan kanan") == ("press_keys", {"keys": "right"})       # arrow key
    assert brain._route("klik kanan") == ("click_here", {"button": "right"})      # right mouse button
    assert brain._route("klik") == ("click_here", {})
    assert brain._route("klik dua kali") == ("click_here", {"double": True})
    assert brain._route("klik dua kali ikon steam") == ("click_on_screen", {"target": "dua kali ikon steam"})
    assert brain._route("klik kanan ikon steam") == ("click_on_screen", {"target": "kanan ikon steam"})
    # heard in real use
    real_title = brain._active_title
    brain._active_title = lambda: "youtube - mozilla firefox"
    assert brain._route("telusuri doraemon") == ("search_youtube", {"query": "doraemon"})
    brain._active_title = lambda: "kitty"
    assert brain._route("telusuri doraemon") == ("google_search", {"query": "doraemon"})
    brain._active_title = real_title
    assert brain._route("cari di pencarian youtube doraemon") == ("search_youtube", {"query": "doraemon"})
    import habits
    assert habits.apply_aliases("ubah lampu kamera") == "ubah lampu ke merah"
    assert brain._route("ubah lampu ke merah")[0] == "lamp_color"
    assert brain._route("matikan notifikasi") == ("do_not_disturb", {"on": True})     # the opposite of the next one
    assert brain._route("matikan jangan ganggu") == ("do_not_disturb", {"on": False})
    assert brain._route("nyalakan notifikasi lagi") == ("do_not_disturb", {"on": False})
    assert brain._route("buka kunci layar") == ("unlock_screen", {})
    assert brain._route("kunci layar") == ("lock_workstation", {})
    assert brain._route("pindah ke world ps2") == ("switch_workspace", {"workspace": "2"})
    assert brain.gemini_reason("atur volume 10% dan kecerahan 20%")


def test_try_again_repeats_the_last_command():
    said = []
    brain.TOOL_MAP["_t"] = lambda: None
    real = brain.fast_route
    brain.fast_route = lambda text: said.append(text) or "ok"
    brain._last_action.clear()
    try:
        assert brain._process_command("coba lagi")[0] == "ok" and said == ["coba lagi"]     # nothing to repeat: as said
        said.clear()
        brain._last_action.update(text="sambungan keyboard", calls=[("bluetooth", {}, "gagal")], at=time.time())
        brain._process_command("coba lagi")
        brain._process_command("Coba sambungkan lagi.")
        assert said == ["sambungan keyboard", "sambungan keyboard"], said
        said.clear()
        brain._process_command("coba putar lagi")                                           # about something else
        assert said == ["coba putar lagi"]
        said.clear()
        brain._last_action["at"] = time.time() - 600
        brain._process_command("coba lagi")                                                 # long ago
        assert said == ["coba lagi"]
    finally:
        brain.fast_route = real
        brain._last_action.clear()
        brain.TOOL_MAP.pop("_t", None)


def test_jarvis_pillars_upgrade():
    import datetime
    import everyday
    import jadwal_kuliah
    import media
    import watchers

    # 1. Per-app volume mixer & ducking sync + system mic control
    calls = []
    real_ld = (pc_control.ld.run, pc_control.ld.out, pc_control.ld._sink_inputs)
    fake_inputs = [{"index": 42, "mute": False, "volume": {"front-left": {"value_percent": "80%"}},
                    "properties": {"application.name": "vesktop", "application.process.binary": "vesktop", "media.name": "Voice"}}]
    pc_control.ld._sink_inputs = lambda: fake_inputs
    pc_control.ld.run = lambda args, timeout=5, input=None: calls.append(args) or _Done()
    pc_control.ld.out = lambda args, timeout=5: "Volume: 0.75 [MUTED]" if "get-volume" in args else ""
    real_duck_file = media.DUCK_FILE
    media.DUCK_FILE = Path(tempfile.mkdtemp()) / "ducked.json"      # (the real one: its "vesktop 30" would be applied at Digidaw's next start)
    media._ducked.clear()
    media._ducked["vesktop"] = 80
    try:
        res = pc_control.app_volume("discord", level=30)
        assert "30 persen" in res and ["pactl", "set-sink-input-volume", "42", "30%"] in calls
        assert media._ducked["vesktop"] == 30                       # unduck() won't overwrite the new volume back to 80%
        assert "dibisukan" in pc_control.app_volume("discord", action="mute")
        assert ["pactl", "set-sink-input-mute", "42", "1"] in calls
        assert pc_control.mic_control("mute").startswith(safety.CONFIRMATION_TOKEN)     # asked first: Digidaw hears through it
        assert "dibisukan" in safety.confirm()
        assert "dinyalakan" in pc_control.mic_control("unmute")
        assert "75 persen" in pc_control.mic_control("status")
    finally:
        media._ducked.clear()
        media.DUCK_FILE = real_duck_file
        pc_control.ld.run, pc_control.ld.out, pc_control.ld._sink_inputs = real_ld

    # 2. Clipboard history & paste_previous
    clip_state = {"val": "teks pertama"}
    real_clip = (everyday.ld.clipboard_get, everyday.ld.clipboard_set, everyday.sc.press_keys)
    pressed = []
    everyday.ld.clipboard_get = lambda primary=False: clip_state["val"]
    everyday.ld.clipboard_set = lambda text: clip_state.update(val=text) or True
    everyday.sc.press_keys = lambda k: pressed.append(k)
    everyday._clip_history.clear()
    try:
        everyday._sync_clipboard()
        clip_state["val"] = "teks kedua"
        everyday._sync_clipboard()
        assert "2 salinan" in everyday.clipboard_history()
        assert "teks pertama" in everyday.paste_previous(2)
        assert clip_state["val"] == "teks pertama" and pressed
    finally:
        everyday.ld.clipboard_get, everyday.ld.clipboard_set, everyday.sc.press_keys = real_clip
        everyday._clip_history.clear()

    # 3. Port process & systemd service control
    real_find_port, real_open = everyday._find_port_owner, everyday._port_open
    everyday._find_port_owner = lambda port: (9999, "node") if port == 3000 else None
    everyday._port_open = lambda port: False
    safety.cancel()
    try:
        assert "node" in everyday.port_process(3000)
        assert "Tidak ada" in everyday.port_process(4000)
        guarded = everyday.port_process(3000, kill=True)
        assert guarded.startswith(safety.CONFIRMATION_TOKEN)
        safety.cancel()
    finally:
        everyday._find_port_owner, everyday._port_open = real_find_port, real_open

    # 4. Watchers: new CPU & update kinds + JSON persistence
    assert watchers.parse("kabari saya kalau beban cpu sudah tenang") == ("cpu", "", "<25")
    assert watchers.parse("kabari kalau cpu di bawah 20 persen") == ("cpu", "", "<20")
    assert watchers.parse("kabari kalau update sistem selesai") == ("update", "", "")
    tmp_watch = Path(tempfile.mkdtemp()) / "watchers.json"
    real_watch_file, real_started = watchers.STATE_FILE, watchers._started
    watchers.STATE_FILE, watchers._started = tmp_watch, True
    watchers._watches.clear()
    try:
        watchers.watch_for("cpu", value="<25")
        assert tmp_watch.exists()
        watchers._watches.clear()
        watchers._load_saved()
        assert len(watchers._watches) == 1 and watchers._watches[0]["kind"] == "cpu"
    finally:
        watchers._watches.clear()
        watchers.STATE_FILE, watchers._started = real_watch_file, real_started

    # 5. Fast routes & local compound execution
    new_routes = {
        "volume discord 30 persen": ("app_volume", {"app": "discord", "level": 30}),
        "bisukan suara spotify": ("app_volume", {"app": "spotify", "action": "mute"}),
        "bisukan mikrofon": ("mic_control", {"action": "mute"}),
        "nyalakan mikrofon": ("mic_control", {"action": "unmute"}),
        "riwayat clipboard": ("clipboard_history", {}),
        "tempel salinan kedua": ("paste_previous", {"index": 2}),
        "salin teks di layar": ("copy_screen_text", {}),
        "siapa yang pakai port 8080": ("port_process", {"port": 8080}),
        "matikan port 3000": ("port_process", {"port": 3000, "kill": True}),
        "status service docker": ("service_control", {"name": "docker", "action": "status"}),
        "restart layanan bluetooth": ("service_control", {"name": "bluetooth", "action": "restart"}),
        "jelaskan error ini": ("explain_last_error", {}),
    }
    for said, expected in new_routes.items():
        assert brain._route(said) == expected, (said, brain._route(said))

    # Compound settings execute locally without Gemini
    comp_calls = []
    real_tools = (brain.TOOL_MAP["set_volume"], brain.TOOL_MAP["set_brightness"])
    brain.TOOL_MAP["set_volume"] = lambda level: comp_calls.append(("vol", level)) or f"Volume diatur ke {level} persen, {config.USER_TITLE}."
    brain.TOOL_MAP["set_brightness"] = lambda level: comp_calls.append(("bri", level)) or f"Kecerahan layar diatur ke {level} persen, {config.USER_TITLE}."
    try:
        out = brain.fast_route("atur volume 10% dan kecerahan 20%")
        assert out and ("vol", 10) in comp_calls and ("bri", 20) in comp_calls
    finally:
        brain.TOOL_MAP["set_volume"], brain.TOOL_MAP["set_brightness"] = real_tools

    # 6. A class reminder only reminds: no offer to start a routine (turned off by the user on 6 Nov; "belajar" was the
    # Fokus routine: every window minimized, music, a 25 minute timer, right before class)
    real_on_date, real_min = jadwal_kuliah.on_date, config.WEEKLY_REMIND_MINUTES
    now_dt = datetime.datetime.now()
    soon_dt = now_dt + datetime.timedelta(minutes=10)
    if 6 <= soon_dt.hour < 22:
        jadwal_kuliah.on_date = lambda d, classes_only=False, items=None: [
            {"id": "k1", "day": d.isoweekday(), "time": f"{soon_dt:%H:%M}", "name": "Kalkulus", "room": "Ruang B2",
             "urgency": "normal", "is_class": True}]
        config.WEEKLY_REMIND_MINUTES = 15
        safety.cancel()
        try:
            rem = jadwal_kuliah.due_reminders(set())
            assert rem and "Kalkulus" in rem[0][1] and "?" not in rem[0][1] and not safety.has_pending()
        finally:
            jadwal_kuliah.on_date, config.WEEKLY_REMIND_MINUTES = real_on_date, real_min


if __name__ == "__main__":
    ok = True
    for test in (test_going_where_you_are_stays_there, test_windows_find_their_workspace, test_routine_steps_with_workspace_and_sites, test_other_apps_get_their_volume_back, test_recap_waits_quietly, test_arranging_windows, test_routines_by_talking, test_hud_buttons_follow_use, test_closing_only_what_is_meant, test_learning_needs_more_than_one_answer, test_guesses_and_misheard_words, test_scripts_and_fan, test_sums, test_pick_is_forced_to_the_list, test_arguments_are_grounded, test_routed_brain_acts_or_hands_over,
                 test_knowledge_sheet, test_knowledge_answer_routes, test_follow_ups_become_whole_sentences, test_undo_only_the_alarm_just_set,
                 test_learning_from_the_teacher, test_screen_job_steps, test_closer_look_before_a_click, test_click_target_not_there_or_risky_label, test_scale_and_position, test_risky_steps_wait_for_yes,
                 test_screen_job_limits, test_esc_while_the_ai_is_thinking, test_new_job_replaces_old_one_and_new_command_stops_it,
                 test_no_step_on_an_old_picture, test_user_busy_stops_the_job, test_changed_near, test_busy_ai_is_waited_out, test_ask_user_then_go_on, test_read_only_commands_need_no_yes, test_files,
                 test_settings, test_control_routes, test_try_again_repeats_the_last_command, test_jarvis_pillars_upgrade):
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
