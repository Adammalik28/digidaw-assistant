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
import safety        # noqa: E402
import screen_agent  # noqa: E402
import system_control as sc  # noqa: E402


REAL_AIM = screen_agent._aim


def _answers(*answers):
    """local_llm.generate replaced: gives these answers in turn, remembers what it was asked."""
    asked, queue = [], list(answers)

    def fake(system, user, schema=None, max_tokens=200, temperature=0.2, history=None):
        asked.append({"system": system, "user": user, "schema": schema})
        return queue.pop(0)
    local_llm.generate = fake
    return asked


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


if __name__ == "__main__":
    ok = True
    for test in (test_pick_is_forced_to_the_list, test_arguments_are_grounded, test_routed_brain_acts_or_hands_over,
                 test_learning_from_the_teacher, test_screen_job_steps, test_closer_look_before_a_click, test_click_target_not_there_or_risky_label, test_scale_and_position, test_risky_steps_wait_for_yes,
                 test_screen_job_limits, test_busy_ai_is_waited_out, test_ask_user_then_go_on, test_read_only_commands_need_no_yes, test_files,
                 test_settings, test_control_routes):
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
