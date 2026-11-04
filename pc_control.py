"""
Digidaw's hands on this PC beyond opening apps: the mouse and the system's settings (Linux with Hyprland).

The mouse: on Wayland no program may move the real pointer, but Hyprland moves it on request (movecursor) and a virtual
input device can click. Screen captures come from grim. Coordinates: screen_agent.py gets them relative to the picture
of the focused monitor; to_layout() turns them into Hyprland's coordinates (monitor position and scale included).

Settings: Wi-Fi, Bluetooth, power mode, Do Not Disturb, the audio output, the screen, the fan. Also the user's own scripts. Turning Wi-Fi off asks first:
it also takes away Digidaw's own hearing and voice (both use the internet).
"""
import difflib
import io
import json
import os
import re
import shlex
import shutil
import threading
import time

import config
import linux_desktop as ld
import safety

T = config.USER_TITLE
CAPTURE_WIDTH = 1280               # pixels: enough to read a screen, small enough to send at every step
_mouse = {"dev": None}
_latest = {"picture": None}        # the last capture at full size, for a closer look
CHANGED_SHARE = 0.08               # this share of the pixels around a target differing = it is not the same picture
_mouse_lock = threading.Lock()


# ───────────────────────────── seeing ─────────────────────────────

def focused_monitor() -> dict:
    """The monitor in front: {name, x, y, width, height, scale} (position in Hyprland's layout)."""
    monitors = json.loads(ld.out(["hyprctl", "monitors", "-j"]) or "[]")
    if not monitors:
        raise RuntimeError("monitor tidak terbaca (bukan Hyprland?)")
    return next((m for m in monitors if m.get("focused")), monitors[0])


def _grab(monitor: dict):
    from PIL import Image
    r = ld.run_bytes(["grim", "-o", monitor["name"], "-t", "ppm", "-"], timeout=10)
    if not r:
        raise RuntimeError("layar tidak bisa dipotret (grim)")
    return Image.open(io.BytesIO(r)).convert("RGB")


def capture(max_width: int = CAPTURE_WIDTH) -> tuple[bytes, dict]:
    """(JPEG of the focused monitor, that monitor). Only this monitor: the fewer other things it shows, the better."""
    monitor = focused_monitor()
    image = _latest["picture"] = _grab(monitor)
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=78)
    return out.getvalue(), monitor


def closer_look(x: float, y: float):
    """A third of the last capture's width and height around a point (in thousandths), enlarged. Returns (JPEG, back),
    where back(x, y) turns a point on this crop (in thousandths of the crop) into thousandths of the whole picture."""
    image = _latest["picture"]
    if image is None:
        raise RuntimeError("belum ada gambar layar")
    width, height = image.width // 3, image.height // 3
    left = max(0, min(image.width - width, round(x / 1000 * image.width - width / 2)))
    top = max(0, min(image.height - height, round(y / 1000 * image.height - height / 2)))
    crop = image.crop((left, top, left + width, top + height))
    crop = crop.resize((CAPTURE_WIDTH, round(height * CAPTURE_WIDTH / width)))
    out = io.BytesIO()
    crop.save(out, "JPEG", quality=85)

    def back(cx: float, cy: float) -> tuple[float, float]:
        return (left + cx / 1000 * width) / image.width * 1000, (top + cy / 1000 * height) / image.height * 1000
    return out.getvalue(), back


def changed_near(x: float, y: float) -> bool:
    """Has the screen around this point (in thousandths) changed since the last capture? The AI can take half a minute
    to answer: by then the page has scrolled or the user has gone somewhere else, and a click would land on something
    nobody looked at."""
    from PIL import ImageChops
    before = _latest["picture"]
    if before is None:
        return False
    now = _grab(focused_monitor())
    if now.size != before.size:
        return True
    cx, cy = x / 1000 * before.width, y / 1000 * before.height
    half_width, half_height = before.width / 16, before.height / 10
    box = (max(0, int(cx - half_width)), max(0, int(cy - half_height)),
           min(before.width, int(cx + half_width)), min(before.height, int(cy + half_height)))
    if box[2] <= box[0] or box[3] <= box[1]:
        return False
    diff = ImageChops.difference(before.crop(box), now.crop(box)).convert("L").point(lambda v: 255 if v > 24 else 0)
    return diff.histogram()[255] / (diff.width * diff.height) > CHANGED_SHARE


def front_window() -> str:
    """The address of the window in front ("" when none, or not Hyprland)."""
    try:
        return json.loads(ld.out(["hyprctl", "activewindow", "-j"]) or "{}").get("address") or ""
    except ValueError:
        return ""


def to_layout(monitor: dict, x: float, y: float) -> tuple[int, int]:
    """A point on the picture, in thousandths of its width and height (0-1000), to Hyprland's coordinates."""
    scale = float(monitor.get("scale") or 1) or 1
    width, height = monitor["width"] / scale, monitor["height"] / scale
    x, y = max(0.0, min(1000.0, float(x))), max(0.0, min(1000.0, float(y)))
    return round(monitor["x"] + x / 1000 * width), round(monitor["y"] + y / 1000 * height)


# ───────────────────────────── the mouse ─────────────────────────────

def _device():
    with _mouse_lock:
        if _mouse["dev"] is None:
            from evdev import UInput, ecodes as e
            # ("agent" in the name: presence.py doesn't take its clicks for the user being back)
            _mouse["dev"] = UInput({e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                                    e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]}, name="digidaw-agent-mouse")
            time.sleep(0.3)                # let the compositor pick the new device up
        return _mouse["dev"]


def _press(button: str, double: bool):
    from evdev import ecodes as e
    ui = _device()
    code = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}[button]
    for _ in range(2 if double else 1):
        ui.write(e.EV_KEY, code, 1)
        ui.syn()
        time.sleep(0.03)
        ui.write(e.EV_KEY, code, 0)
        ui.syn()
        time.sleep(0.06)


def _move(x: int, y: int):
    """Hyprland moves the pointer at once, but a window that already had the pointer over it only hears of the new spot
    with the next real mouse event: the click then landed on the PREVIOUS spot (seen in tests/e2e_hands.py). So a tiny
    real movement follows."""
    from evdev import ecodes as e
    ui = _device()                     # (before moving: a new device takes a moment)
    ld.dispatch("movecursor", str(x), str(y))
    time.sleep(0.03)
    for step in (1, -1):
        ui.write(e.EV_REL, e.REL_X, step)
        ui.syn()
        time.sleep(0.02)
    time.sleep(0.05)


def click(x: int, y: int, button: str = "left", double: bool = False):
    """Move the pointer to this layout position and click."""
    _move(x, y)
    _press(button, double)


def click_here(button: str = "left", double: bool = False) -> str:
    """Click where the mouse pointer is now ("klik", "klik kanan", "klik dua kali"). button: left or right."""
    _press("right" if button == "right" else "left", double)
    return f"Sudah diklik, {T}."


def scroll(amount: int, x: int | None = None, y: int | None = None):
    """Wheel notches: positive = up, negative = down (over this point, if given)."""
    from evdev import ecodes as e
    ui = _device()
    if x is not None and y is not None:
        _move(x, y)
    for _ in range(min(abs(int(amount)), 30)):
        ui.write(e.EV_REL, e.REL_WHEEL, 1 if amount > 0 else -1)
        ui.syn()
        time.sleep(0.02)


# ───────────────────────────── settings ─────────────────────────────

def _ok(result) -> bool:
    return bool(result and result.returncode == 0)


def _do_wifi(on: bool) -> str:
    if not _ok(ld.run(["nmcli", "radio", "wifi", "on" if on else "off"], timeout=10)):
        return f"Wi-Fi tidak bisa di{'nyalakan' if on else 'matikan'}, {T}."
    return f"Wi-Fi {'menyala' if on else 'dimatikan'}, {T}."


def wifi_power(on: bool = True) -> str:
    """Turn Wi-Fi on or off ("matikan wifi", "nyalakan wifi"). Off asks first: Digidaw's hearing and voice need the
    internet."""
    if on:
        return _do_wifi(True)
    return safety.guard("mematikan Wi-Fi (pendengaran dan suara Digidaw ikut butuh internet)", _do_wifi, on=False)


def wifi_connect(name: str) -> str:
    """Connect to a Wi-Fi network the laptop already knows ("sambungkan wifi ke Kantor")."""
    known = [c.split(":")[0] for c in ld.out(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"]).splitlines()
             if c.endswith("wireless")]
    match = _closest(name, known)
    if not match:
        return f"Wi-Fi {name} belum pernah tersambung di laptop ini, {T}. Kata sandinya perlu dimasukkan dulu."
    if _ok(ld.run(["nmcli", "con", "up", "id", match], timeout=30)):
        return f"Tersambung ke Wi-Fi {match}, {T}."
    return f"Wi-Fi {match} tidak bisa disambungkan, {T}."


def _closest(said: str, names: list[str]) -> str:
    """The name that matches what was said: contained in it, or sounding close to it."""
    low = said.lower().strip()
    for name in names:
        if low and low in name.lower():
            return name
    close = difflib.get_close_matches(low, [n.lower() for n in names], n=1, cutoff=0.6)
    return next((n for n in names if close and n.lower() == close[0]), "")


def bluetooth(action: str, device: str = "") -> str:
    """Bluetooth. action: "on", "off", "connect" or "disconnect"; device: part of its name ("sambungkan bluetooth ke
    headset", "matikan bluetooth")."""
    action = action.lower().strip()
    if action in ("on", "off"):
        if not _ok(ld.run(["bluetoothctl", "power", action], timeout=8)):
            return f"Bluetooth tidak bisa di{'nyalakan' if action == 'on' else 'matikan'}, {T}."
        return f"Bluetooth {'menyala' if action == 'on' else 'dimatikan'}, {T}."
    if action not in ("connect", "disconnect"):
        return f"Perintah Bluetooth {action} belum saya kenal, {T}."
    devices = dict(reversed(line.split(" ", 2)[1:]) for line in ld.out(["bluetoothctl", "devices"], timeout=5).splitlines()
                   if line.startswith("Device ") and len(line.split(" ", 2)) == 3)
    if action == "disconnect" and not device:
        return f"Bluetooth terputus, {T}." if _ok(ld.run(["bluetoothctl", "disconnect"], timeout=8)) \
            else f"Tidak ada perangkat Bluetooth yang tersambung, {T}."
    match = _closest(device, list(devices))
    if not match:
        names = ", ".join(devices) or "belum ada"
        return f"Perangkat {device} tidak ada di daftar Bluetooth, {T}. Yang dikenal: {names}."
    ld.run(["bluetoothctl", "power", "on"], timeout=8)
    if _ok(ld.run(["bluetoothctl", action, devices[match]], timeout=15)):
        return f"{match} {'tersambung' if action == 'connect' else 'terputus'}, {T}."
    return f"{match} tidak bisa di{'sambungkan' if action == 'connect' else 'putuskan'}. Pastikan perangkatnya menyala, {T}."


POWER_MODES = {"power-saver": ("hemat", "irit", "power"), "balanced": ("seimbang", "normal", "biasa", "balanced"),
               "performance": ("performa", "kencang", "cepat", "game", "gaming", "performance")}
POWER_SAY = {"power-saver": "hemat daya", "balanced": "seimbang", "performance": "performa tinggi"}


def power_mode(mode: str) -> str:
    """The laptop's power mode: "hemat" (saves battery), "seimbang" or "performa" (for games and heavy work)."""
    said = mode.lower()
    profile = next((p for p, words in POWER_MODES.items() if any(w in said for w in words)), "")
    if not profile:
        return f"Mode daya {mode} belum saya kenal, {T}. Pilihannya hemat, seimbang, atau performa."
    if not _ok(ld.run(["powerprofilesctl", "set", profile], timeout=8)):
        return f"Mode daya tidak bisa diubah, {T}."
    return f"Mode daya sekarang {POWER_SAY[profile]}, {T}."


def do_not_disturb(on: bool = True) -> str:
    """Do Not Disturb: notifications stay quiet ("aktifkan jangan ganggu"); Digidaw keeps small news to itself too."""
    for tool, args in (("swaync-client", ["-dn" if on else "-df"]),
                       ("makoctl", ["mode", "-a" if on else "-r", "do-not-disturb"]),
                       ("dunstctl", ["set-paused", "true" if on else "false"])):
        if shutil.which(tool) and _ok(ld.run([tool, *args], timeout=5)):
            return f"Mode jangan ganggu {'aktif' if on else 'dimatikan'}, {T}."
    return f"Mode jangan ganggu tidak bisa diubah di sini, {T}."


def audio_output(name: str) -> str:
    """Where the sound comes out: part of the device's name ("pindahkan suara ke speaker laptop", "suara ke HDMI",
    "ke speaker USB", "ke headset")."""
    sinks = json.loads(ld.out(["pactl", "-f", "json", "list", "sinks"]) or "[]")
    said = re.sub(r"\b(?:laptop|keluaran|output|suara)\b", "", name.lower()).strip() or name.lower()
    match = next((s for s in sinks if said in str(s.get("description", "")).lower()), None)
    if not match:
        names = ", ".join(str(s.get("description", ""))[-34:].strip() for s in sinks)
        return f"Keluaran suara {name} tidak ditemukan, {T}. Yang ada: {names}."
    if not _ok(ld.run(["pactl", "set-default-sink", match["name"]])):
        return f"Keluaran suara tidak bisa diubah, {T}."
    for stream in json.loads(ld.out(["pactl", "-f", "json", "list", "sink-inputs"]) or "[]"):
        ld.run(["pactl", "move-sink-input", str(stream.get("index")), match["name"]])
    return f"Suara sekarang keluar lewat {str(match.get('description', '')).split(' Controller ')[-1]}, {T}."


def screen_power(on: bool = False) -> str:
    """Turn the screen off or on without sleeping. Only when the user says that of the screen ("matikan layar",
    "nyalakan layar"): "tutup screen" is not this, it's closing a window. Moving the mouse brings it back."""
    threading.Timer(1.5 if not on else 0, lambda: ld.dispatch("dpms", "on" if on else "off")).start()
    return f"Layar saya {'nyalakan' if on else 'matikan'}, {T}."


def fan_speed(percent: int = -1, auto: bool = False) -> str:
    """The laptop's fan (NoteBook FanControl, what the user's "fanset 50" does): percent 0-100 ("kipas 50 persen",
    "fanset 70"), auto=True gives it back to the laptop ("kipas otomatis"), no arguments says how it is now."""
    if not shutil.which("nbfc"):
        return f"Pengatur kipas (nbfc) tidak ada di komputer ini, {T}."
    if auto:
        return f"Kipas kembali otomatis, {T}." if _ok(ld.run(["nbfc", "set", "-a"], timeout=8)) \
            else f"Kipas tidak bisa diubah, {T}. Layanan nbfc tidak merespons."
    if percent < 0:
        status = ld.out(["nbfc", "status"], timeout=8)
        speed = re.search(r"Current Fan Speed\s*:\s*([\d.]+)", status)
        heat = re.search(r"Temperature\s*:\s*([\d.]+)", status)
        mode = "otomatis" if re.search(r"Auto Control Enabled\s*:\s*true", status) else "manual"
        if not speed:
            return f"Status kipas tidak terbaca, {T}."
        return f"Kipas {round(float(speed[1]))} persen, mode {mode}" + (f", suhu {round(float(heat[1]))} derajat" if heat else "") + f", {T}."
    if percent > 100:
        return f"Kecepatan kipas hanya 0 sampai 100 persen, {T}."
    if percent == 0:
        return safety.guard("mematikan kipas laptop", _set_fan, percent=0)
    return _set_fan(percent)


def _set_fan(percent: int) -> str:
    if _ok(ld.run(["nbfc", "set", "-s", str(percent)], timeout=8)):
        return f"Kipas saya atur ke {percent} persen, {T}."
    return f"Kipas tidak bisa diubah, {T}. Layanan nbfc tidak merespons."


SAID_AS = {"check": "cek", "maintenance": "maintance"}       # how Google writes the words of the user's script names
SCRIPT_REQUEST_RE = re.compile(r"^(?:(?:tolong|coba|jalankan|jalanin|eksekusi|run|buka|skrip|script|scrip|sekrip|sc|yang)\s+)+",
                               re.IGNORECASE)


def _key(name: str) -> str:
    """"Cek CPU", "check cpu", "cekcpu.sh", "ssh key" and "ssh.key.sh" should meet."""
    words = re.sub(r"\.sh$", "", name.lower().strip()).split()
    return re.sub(r"[^a-z0-9]", "", "".join(SAID_AS.get(w, w) for w in words))


def _my_scripts() -> dict[str, str]:
    """{key: file name} of the scripts the user's own manager lists (a new one is known as soon as it's there)."""
    folder = config.SCRIPTS_DIR.expanduser()
    if not folder.is_dir():
        return {}
    return {_key(p.name): p.name for p in sorted(folder.glob("*.sh"))
            if p.is_file() and p.name not in (config.SCRIPT_MANAGER, *config.SCRIPTS_NOT_RUN)}


SCRIPT_WINDOW_RE = re.compile(r"Pengelola skrip|Skrip \S+\.sh")


def _in_window(command: str, title: str) -> bool:
    """A terminal floating like the manager's own Super+S window. Started by Hyprland: the terminal shouldn't inherit
    Digidaw's environment or end with it."""
    line = f"[float;size 800 750;center] kitty --title {shlex.quote(title)} {command}"
    return ld.dispatch("exec", line) or ld.spawn(["sh", "-c", line.split("] ", 1)[1]])


def open_script_manager() -> str:
    """Open the user's script manager, the fzf list in ~/myskrip behind Super+S ("buka skrip", "pengelola skrip")."""
    path = config.SCRIPTS_DIR.expanduser() / config.SCRIPT_MANAGER
    if not path.is_file():
        return f"Pengelola skrip {path} tidak ditemukan, {T}."
    return f"Pengelola skrip saya buka, {T}." if _in_window(shlex.quote(str(path)), "Pengelola skrip") \
        else f"Pengelola skrip tidak bisa dibuka, {T}."


def _spoken(file: str) -> str:
    return re.sub(r"[.]sh$", "", file).replace("_", " ").replace(".", " ")


def _start_script(file: str) -> str:
    path = config.SCRIPTS_DIR.expanduser() / file
    if not path.is_file():
        return f"Skrip {_spoken(file)} sudah tidak ada, {T}."
    command = shlex.quote(str(path)) if os.access(path, os.X_OK) else f"bash {shlex.quote(str(path))}"
    if file in config.SCRIPTS_WITHOUT_WINDOW:     # it only starts apps; closing a terminal would close them as well
        started = ld.dispatch("exec", command) or ld.spawn(["sh", "-c", command])
    else:                                         # menus, questions and sudo's password happen there, for the user's eyes
        started = _in_window(f"--hold {command}", f"Skrip {file}")
    return f"Skrip {_spoken(file)} saya jalankan, {T}." if started else f"Skrip {_spoken(file)} tidak bisa dijalankan, {T}."


_which_script = {"files": [], "at": 0.0}     # "Ada beberapa ... Yang mana?" waits for its answer
WHICH_SECONDS = 30


def answer_which_script(said: str) -> str | None:
    """The answer to that question: "cek cpu", or only "cpu". None = it wasn't one (the sentence is treated as usual)."""
    files, _which_script["files"] = _which_script["files"], []
    if not files or time.time() - _which_script["at"] > WHICH_SECONDS:
        return None
    key = _key(said)
    found = [f for f in files if len(key) >= 2 and _key(f).endswith(key)]
    return _start_script(found[0]) if len(found) == 1 else None


def is_my_script(name: str) -> bool:
    """Exactly a script's name: "jalankan backup" needs no word "skrip" then."""
    return _key(name) in _my_scripts()


def close_script_window() -> str:
    """Close the window of the script manager or of a script Digidaw started ("tutup skrip"), never anything else."""
    windows = [c for c in ld._hypr_json("clients") or [] if SCRIPT_WINDOW_RE.fullmatch(str(c.get("title", "")))]
    for window in windows:
        ld.dispatch("closewindow", f"address:{window['address']}")
    return f"Jendela skrip saya tutup, {T}." if windows else f"Tidak ada jendela skrip yang terbuka, {T}."


def answer_about_script(said: str, last_reply: str) -> str | None:
    """Digidaw just asked something about a script ("Skrip yang mana?") and this is exactly a script's name: "atur power"
    is that script then, not a request for the power mode."""
    if "skrip" not in last_reply.lower() or not last_reply.rstrip().endswith("?") or not is_my_script(said):
        return None
    safety.cancel()
    return _start_script(_my_scripts()[_key(said)])


def run_script(name: str, unsure: bool = False) -> str:
    """Run one of the user's own scripts in ~/myskrip by name ("jalankan skrip cek cpu", "jalankan skrip backup"). It runs
    in a terminal window the user can see. Only for those scripts; other commands are run_command. name: only the
    script's name. unsure: the request itself may be misheard, ask before running."""
    name = SCRIPT_REQUEST_RE.sub("", name.strip())
    said = _key(name)
    if said == _key(config.SCRIPT_MANAGER):
        return open_script_manager()
    scripts = _my_scripts()
    file = scripts.get(said)
    if not file and len(said) >= 3:
        starting = [f for k, f in scripts.items() if k.startswith(said)]
        if len(starting) > 1:
            _which_script.update(files=starting, at=time.time())
            return f"Ada beberapa, {T}: {', '.join(_spoken(f) for f in starting)}. Yang mana?"
        file = starting[0] if starting else None
    if file and not unsure:
        return _start_script(file)
    if file:
        return safety.guard(f"menjalankan skrip {file}", _start_script, asking=f"Jalankan skrip {_spoken(file)}, {T}?", file=file)
    close = difflib.get_close_matches(said, scripts, n=1, cutoff=0.7) if said else []
    if close:      # a misheard name must not start a script by itself: the user says which
        file = scripts[close[0]]
        return safety.guard(f"menjalankan skrip {file}", _start_script, asking=f"Maksudnya skrip {_spoken(file)}?", file=file)
    return safety.guard("membuka daftar skrip", open_script_manager,
                        asking=f"Skrip {name} tidak ada, {T}. Mau saya bukakan daftarnya?")


AI_TOOLS = [click_here, wifi_power, wifi_connect, bluetooth, power_mode, do_not_disturb, audio_output, screen_power,
            fan_speed, open_script_manager, run_script, close_script_window]
