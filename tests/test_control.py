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


REAL_AIM = screen_agent._aim
REAL_CHANGED_NEAR = pc_control.changed_near


def _answers(*answers):
    """local_llm.generate replaced: gives these answers in turn, remembers what it was asked."""
    asked, queue = [], list(answers)

    def fake(system, user, schema=None, max_tokens=200, temperature=0.2, history=None):
        asked.append({"system": system, "user": user, "schema": schema})
        return queue.pop(0)
    local_llm.generate = fake
    return asked


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
    for closing in ("tutup skrip", "tutup script", "tutup screen", "tutup sc"):
        assert brain._route(closing) == ("close_script_window", {}), closing
    assert brain._route("hidupkan layar") == ("screen_power", {"on": True})
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
        _answers(f'{{"pilihan": "{local_router.KNOW}"}}')
        assert brain.ask_local("kenapa langit biru") is None
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


def test_learning_from_the_teacher():
    experience.FILE, experience._items = Path(tempfile.mkdtemp()) / "e.json", []
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
    pc_control.capture = lambda max_width=0: (b"jpeg", {"x": 1920, "y": 1080, "width": 1920, "height": 1080, "scale": 1})
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


if __name__ == "__main__":
    ok = True
    for test in (test_scripts_and_fan, test_sums, test_pick_is_forced_to_the_list, test_arguments_are_grounded, test_routed_brain_acts_or_hands_over,
                 test_learning_from_the_teacher, test_screen_job_steps, test_closer_look_before_a_click, test_click_target_not_there_or_risky_label, test_scale_and_position, test_risky_steps_wait_for_yes,
                 test_screen_job_limits, test_esc_while_the_ai_is_thinking, test_new_job_replaces_old_one_and_new_command_stops_it,
                 test_no_step_on_an_old_picture, test_user_busy_stops_the_job, test_changed_near, test_busy_ai_is_waited_out, test_ask_user_then_go_on, test_read_only_commands_need_no_yes, test_files,
                 test_settings, test_control_routes, test_try_again_repeats_the_last_command):
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
