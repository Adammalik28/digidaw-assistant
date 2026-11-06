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
import diag
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


def _paint_out_digidaw(image, monitor: dict):
    """Digidaw's own windows painted over: what it wrote there isn't what the user wants clicked. "klik baca komik" was clicked three times: the words of the command, in the HUD.
    Hyprland gives windows in layout coordinates, the picture is in pixels. Not when another window is fullscreen: the HUD is then behind it, and the paint would hide that one."""
    import window_manager
    from PIL import ImageDraw
    workspace = (monitor.get("activeWorkspace") or {}).get("id")
    clients = [c for c in (ld._hypr_json("clients") or []) if (c.get("workspace") or {}).get("id") == workspace and c.get("mapped", True) and not c.get("hidden")]
    own = [c for c in clients if window_manager._is_digidaw({"pid": c.get("pid"), "title": c.get("title") or ""})]
    if not own or any(c.get("fullscreen") and c not in own for c in clients):
        return image
    scale = image.width / (monitor["width"] / (monitor.get("scale") or 1))
    draw = ImageDraw.Draw(image)
    for c in own:
        (x, y), (w, h) = c.get("at") or (0, 0), c.get("size") or (0, 0)
        left, top = (x - monitor.get("x", 0)) * scale, (y - monitor.get("y", 0)) * scale
        draw.rectangle((left, top, left + w * scale, top + h * scale), fill=(24, 24, 24))
    return image


def focused_picture(hide_own: bool = False):
    """(the focused monitor as a picture at full size, that monitor)."""
    monitor = focused_monitor()
    image = _grab(monitor)
    if hide_own:
        try:
            image = _paint_out_digidaw(image, monitor)
        except Exception as e:
            diag.warn_once("pc.paint", "jendela Digidaw tidak bisa ditutupi di gambar layar", e)
    return image, monitor


def capture(max_width: int = CAPTURE_WIDTH, hide_own: bool = False) -> tuple[bytes, dict]:
    """(JPEG of the focused monitor, that monitor). Only this monitor: the fewer other things it shows, the better."""
    image, monitor = focused_picture(hide_own)
    _latest["picture"] = image
    _latest["hide_own"] = hide_own
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
    monitor = focused_monitor()
    now = _grab(monitor)
    if _latest.get("hide_own"):
        try:
            now = _paint_out_digidaw(now, monitor)     # (or the same two pictures differ where the paint is)
        except Exception:
            pass
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
    if action == "status":
        powered = re.search(r"Powered:\s*yes", ld.out(["bluetoothctl", "show"], timeout=5) or "")
        if not powered:
            return f"Bluetooth mati, {T}."
        joined = [line.split(" ", 2)[2] for line in (ld.out(["bluetoothctl", "devices", "Connected"], timeout=5) or "").splitlines()
                  if line.startswith("Device ") and len(line.split(" ", 2)) == 3]
        return f"Bluetooth menyala, {T}, " + (f"tersambung ke {', '.join(joined)}." if joined else "belum tersambung ke perangkat apa pun.")
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
    if action == "connect" and not device and len(devices) != 1:     # one known device needs no name
        names = ", ".join(devices)
        return f"Disambungkan ke yang mana, {T}? Yang dikenal: {names}." if names else f"Belum ada perangkat Bluetooth yang dikenal, {T}."
    match = next(iter(devices)) if action == "connect" and not device else _closest(device, list(devices))
    if not match:
        names = ", ".join(devices) or "belum ada"
        return f"Perangkat {device} tidak ada di daftar Bluetooth, {T}. Yang dikenal: {names}."
    ld.run(["bluetoothctl", "power", "on"], timeout=8)
    if _ok(ld.run(["bluetoothctl", action, devices[match]], timeout=15)):
        return f"{match} {'tersambung' if action == 'connect' else 'terputus'}, {T}."
    return f"{match} tidak bisa di{'sambungkan' if action == 'connect' else 'putuskan'}. Pastikan perangkatnya menyala, {T}."


POWER_MODES = {"power-saver": ("hemat", "irit", "power saver"), "balanced": ("seimbang", "normal", "biasa", "balanced"),
               "performance": ("performa", "kencang", "cepat", "game", "gaming", "performance")}
POWER_SAY = {"power-saver": "hemat daya", "balanced": "seimbang", "performance": "performa tinggi"}


def power_mode(mode: str = "") -> str:
    """The laptop's power mode: "hemat" (saves battery), "seimbang" or "performa" (for games and heavy work). Only the
    mode the user said: with none said ("atur power"), leave it empty and Digidaw asks; never pick one for them."""
    said = mode.lower()
    if not said:
        return f"Mode dayanya mau hemat, seimbang, atau performa, {T}?"
    profile = next((p for p, words in POWER_MODES.items() if any(w in said for w in words)), "")
    if not profile:
        return f"Mode daya {mode} belum saya kenal, {T}. Pilihannya hemat, seimbang, atau performa."
    if not _ok(ld.run(["powerprofilesctl", "set", profile], timeout=8)):
        return f"Mode daya tidak bisa diubah, {T}."
    return f"Mode daya sekarang {POWER_SAY[profile]}, {T}."


def answer_about_power(said: str, last_reply: str) -> str | None:
    """The answer to power_mode's question: "performa" on its own is otherwise Digidaw's own performance report."""
    if "mode daya" not in last_reply.lower() or not last_reply.rstrip().endswith("?"):
        return None
    words = said.strip(" .,!?").lower().split()
    known = any(words and words[0] in names for names in POWER_MODES.values())
    return power_mode(words[0]) if known and len(words) <= 2 else None


def do_not_disturb(on: bool = True) -> str:
    """Do Not Disturb: notifications stay quiet ("aktifkan jangan ganggu"); Digidaw keeps small news to itself too."""
    for tool, args in (("swaync-client", ["-dn" if on else "-df"]),
                       ("makoctl", ["mode", "-a" if on else "-r", "do-not-disturb"]),
                       ("dunstctl", ["set-paused", "true" if on else "false"])):
        if shutil.which(tool) and _ok(ld.run([tool, *args], timeout=5)):
            return f"Mode jangan ganggu {'aktif' if on else 'dimatikan'}, {T}."
    return f"Mode jangan ganggu tidak bisa diubah di sini, {T}."


def _sinks() -> list[dict]:
    """The sound outputs of this PC."""
    return json.loads(ld.out(["pactl", "-f", "json", "list", "sinks"]) or "[]")


def audio_output(name: str) -> str:
    """Where the sound comes out: part of the device's name ("pindahkan suara ke speaker laptop", "suara ke HDMI",
    "ke speaker USB", "ke headset")."""
    sinks = _sinks()
    said =re.sub(r"\b(?:laptop|keluaran|output|suara)\b", "", name.lower()).strip() or name.lower()
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
        return safety.guard("mengatur kipas laptop ke paling pelan", _set_fan, percent=0)
    return _set_fan(percent)


def fan_is_manual() -> bool:
    """Set by hand with nbfc (it then doesn't follow the temperature)."""
    return bool(shutil.which("nbfc")) and re.search(r"Auto Control Enabled\s*:\s*false", ld.out(["nbfc", "status"], timeout=8) or "") is not None


def _set_fan(percent: int) -> str:
    if _ok(ld.run(["nbfc", "set", "-s", str(percent)], timeout=8)):
        if percent == 0:                 # (measured 5 Nov: about 2480 RPM at 0, the laptop doesn't let it stop)
            return f"Kipas saya atur ke paling pelan, {T}. Di laptop ini kipasnya tetap berputar."
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


WINDOW_APPEARS_SECONDS = 8


def _open_and_wait(name: str) -> dict | None:
    """The window of this app, opening the app if needed and waiting until its window is there."""
    import system_control as sc
    import window_manager as wm
    found = wm._find(name)
    if found:
        return found
    sc.open_app_or_web(name)
    until = time.time() + WINDOW_APPEARS_SECONDS
    while time.time() < until:
        time.sleep(0.25)
        found = wm._find(name)
        if found:
            return found
    return None


def _make_tiled(window: dict):
    """A window that floats or fills the screen can't take its place next to another."""
    if window.get("fullscreen"):
        ld.focus(window["hwnd"])
        ld.dispatch("fullscreenstate", "0 0")
    if window.get("floating"):
        ld.dispatch("settiled", f"address:{window['hwnd']}")


def _left_edge(address: str) -> int:
    client = next((c for c in ld._hypr_json("clients") or [] if c.get("address") == address), {})
    return (client.get("at") or [0])[0]


def side_by_side(left: str, right: str) -> str:
    """Two apps next to each other on one screen, opening whichever isn't open ('bagi layar firefox dan terminal',
    'firefox kiri terminal kanan'). left, right: the app names. Other windows on this workspace stay where they are: the two go to
    an empty workspace then."""
    import window_manager as wm
    first, second = _open_and_wait(left), _open_and_wait(right)
    if not first or not second:
        return f"{left if not first else right} tidak bisa dibuka, {T}."
    if first["hwnd"] == second["hwnd"]:
        return f"{left} dan {right} ada di jendela yang sama, {T}, jadi tidak bisa dibagi."
    here = str(ld.active_workspace_id())
    pair = {first["hwnd"], second["hwnd"]}
    if any(w["workspace"] == here and w["hwnd"] not in pair for w in wm._windows()):
        ld.dispatch("workspace", "empty")
        here = str(ld.active_workspace_id())
    for window in (first, second):
        if window["workspace"] != here:
            ld.move_to_workspace(window["hwnd"], here)
        _make_tiled(window)
    time.sleep(0.25)                     # (Hyprland has laid them out only after that)
    if _left_edge(first["hwnd"]) > _left_edge(second["hwnd"]):
        ld.focus(first["hwnd"])
        ld.dispatch("swapwindow", "l")
    ld.focus(first["hwnd"])
    return f"{wm._app_label(first)} di kiri, {wm._app_label(second)} di kanan, {T}."


def place_window(name: str, side: str) -> str:
    """Put an app's window on the left or right of the screen, opening it if needed ('taruh firefox di kiri', 'buka terminal di
    kanan'). side: 'kiri' or 'kanan'. name: empty = the active window."""
    import window_manager as wm
    side = "kiri" if "kiri" in side.lower() or side.lower() == "left" else "kanan"
    window = _open_and_wait(name) if name else wm._target("")
    if not window:
        return f"Jendela {name or 'aktif'} tidak ditemukan, {T}."
    _make_tiled(window)
    ld.snap(window["hwnd"], "left" if side == "kiri" else "right")
    return f"{wm._app_label(window)} di {side}, {T}."


def swap_windows() -> str:
    """Swap the two sides ('tukar posisi jendela', 'tukar kiri kanan')."""
    return f"Posisinya saya tukar, {T}." if ld.dispatch("layoutmsg", "swapsplit") else f"Posisi jendela tidak bisa ditukar, {T}."


def tidy_windows() -> str:
    """Tidy this workspace: floating or full-screen windows take their place in the layout ('rapikan jendela', 'susun jendela')."""
    import window_manager as wm
    here = str(ld.active_workspace_id())
    windows = [w for w in wm.closable_windows() if w["workspace"] == here]
    for window in windows:
        _make_tiled(window)
    return f"Jendela di workspace ini sudah saya rapikan, {T}." if windows else f"Tidak ada jendela di workspace ini, {T}."


def focus_previous() -> str:
    """Back to the window used before this one ('kembali ke aplikasi tadi', 'jendela sebelumnya')."""
    return f"Kembali ke jendela tadi, {T}." if ld.dispatch("focuscurrentorlast") else f"Tidak ada jendela sebelumnya, {T}."


PLACE_SECONDS = 60          # slow apps: Discord shows an updater first, its real window can take this long
SITE_WINDOW_SECONDS = 15
_placing: list[dict] = []
_placing_lock = threading.Lock()


def _place_loop():
    """Every window of an app being opened goes to its workspace as soon as it appears, also the ones that come later (Discord's
    updater, then Discord), without moving the screen. Each window once: where the user puts it afterwards stays."""
    import window_manager as wm
    while True:
        time.sleep(0.15)
        with _placing_lock:
            _placing[:] = [p for p in _placing if time.time() < p["until"]]
            if not _placing:
                _place_thread["alive"] = False
                return
            items = list(reversed(_placing))     # the latest request first
        windows = wm._windows()
        if not windows:
            continue
        here = {w["hwnd"] for w in windows}
        for item in items:
            item["done"] &= here                 # Hyprland gives a closed window's address to the next one (updater, then Discord)
        for window in windows:
            if window["workspace"].startswith("special:") or wm._is_digidaw(window):
                continue
            for item in items:
                if window["hwnd"] not in item["done"] and wm.is_app_window(window, item["name"]):
                    for other in items:
                        other["done"].add(window["hwnd"])
                    if window["workspace"] != item["workspace"]:
                        ld.move_to_workspace(window["hwnd"], item["workspace"])
                    if item["view"] and window["workspace"] != item["view"] and ld.active_workspace() == window["workspace"]:
                        ld.switch_workspace(item["view"])    # Firefox's new window pulled the screen to its other window
                    if item["once"]:
                        item["until"] = 0
                    break


_place_thread = {"alive": False}


def place_on_workspace(name: str, workspace: str, move_existing: bool = False, once: bool = False, keep_view: str = "",
                       seconds: float = 0):
    """From now on, for a while, windows of this app go to this workspace. move_existing: the one already open.
    once: only one window, for an app already open or a site: a window the user opens after that is theirs to place.
    keep_view: the workspace the screen should stay on if the window pulls it away."""
    import window_manager as wm
    present = [w for w in wm._windows() if wm.is_app_window(w, name)]
    keep_one = present[:1] if move_existing else []
    done = {w["hwnd"] for w in present if w not in keep_one}
    with _placing_lock:
        _placing.append({"name": name, "workspace": str(workspace), "until": time.time() + (seconds or PLACE_SECONDS), "done": done,
                         "once": once, "view": keep_view})
        if not _place_thread["alive"]:
            _place_thread["alive"] = True
            threading.Thread(target=_place_loop, daemon=True, name="place-windows").start()


def _site_on_workspace(name: str, url: str, workspace: str) -> str:
    """A site on that workspace: a tab if the browser is there already, otherwise a window of its own there (so another site
    can have another workspace). The window finds its place by itself."""
    import screen_context
    import system_control as sc
    browsers = screen_context._browser_windows()
    browser = ld.default_browser() or "firefox"
    if any(w["workspace"] == workspace for w in browsers):
        return sc.open_website(url or name)
    place_on_workspace(browser, workspace, once=True, keep_view=ld.active_workspace(), seconds=SITE_WINDOW_SECONDS)
    if not browsers:
        return sc.open_website(url or name)
    command = ld.browser_command(browser)
    if not command or not ld.spawn([command, "--new-window", url]):
        return f"{name} tidak bisa dibuka di workspace {workspace}, {T}."
    return f"{name} terbuka di workspace {workspace}, {T}."


def open_on_workspace(name: str, workspace: str) -> str:
    """Open an app or a website on a workspace; an app already open moves there ('buka whatsapp di workspace 1', 'buka youtube di
    workspace 2'). On its own the screen goes there too; in a routine it stays where the user is. name: an app, a site, or an address."""
    import resolver
    import site_finder
    import system_control as sc
    import window_manager as wm
    workspace = str(workspace)
    if not workspace.isdigit():
        return f"Workspace '{workspace}' tidak dikenal, {T}."
    in_routine = ld.opening_quietly()
    if not in_routine:
        ld.switch_workspace(workspace)
    with ld.quiet_opening(workspace):
        key = sc._match_key(name, sc.WEBSITES)
        if key:
            return _site_on_workspace(name, sc.WEBSITES[key], workspace)
        if site_finder.is_address(name):
            return _site_on_workspace(name, site_finder.find(name), workspace)
        window = wm.find_app(name)
        if window:
            place_on_workspace(name, workspace, move_existing=True, once=True)
            return f"{wm._app_label(window)} ada di workspace {workspace}, {T}."
        if resolver.resolve(name, ("app",), sure_only=True)[0] != "sure":
            found = site_finder.find(name)
            if found:
                return _site_on_workspace(name, found, workspace)
        place_on_workspace(name, workspace)
        return sc.open_app_or_web(name)


def move_all_to_workspace(workspace: str) -> str:
    """Move every window of this workspace to another one and go along ('pindahkan semua ke workspace 2')."""
    import window_manager as wm
    if not str(workspace).isdigit():
        return f"Workspace '{workspace}' tidak dikenal, {T}."
    here = str(ld.active_workspace_id())
    if here == str(workspace):
        return f"Anda sudah di workspace {workspace}, {T}."
    windows = [w for w in wm.closable_windows() if w["workspace"] == here]
    if not windows:
        return f"Tidak ada jendela di workspace ini, {T}."
    for window in windows:
        ld.move_to_workspace(window["hwnd"], str(workspace))
    ld.switch_workspace(str(workspace))
    return f"{len(windows)} jendela dipindah ke workspace {workspace}, {T}."


def _close_windows(addresses: list[str]) -> str:
    for address in addresses:
        ld.close(address)
    time.sleep(2)
    left = [a for a in addresses if ld.window_exists(a)]
    return f"Sudah saya tutup, {T}." if not left else f"{len(left)} jendela masih terbuka, mungkin menanyakan sesuatu, {T}."


def close_others(keep: str) -> str:
    """Close every app window except these ('tutup semua kecuali firefox', 'tutup semua kecuali firefox dan terminal'). keep: the
    app names. Digidaw itself and its music player stay. Asks for confirmation first."""
    import window_manager as wm
    kept = set()
    names = [n.strip() for n in re.split(r"\s*(?:,|\bdan\b|\bsama\b)\s*", keep) if n.strip()]
    for name in names:
        found = wm._find(name)
        if not found:
            return f"Tidak ada jendela {name}, {T}, jadi belum ada yang saya tutup."
        kept.add(found.get("class") or found["process"])
    windows = [w for w in wm.closable_windows() if (w.get("class") or w["process"]) not in kept]
    if not names or not windows:
        return f"Tidak ada jendela lain yang perlu ditutup, {T}."
    apps = sorted({wm._app_label(w) for w in windows})
    return safety.guard(f"menutup {len(windows)} jendela ({', '.join(apps[:5])}), kecuali {keep}", _close_windows,
                        addresses=[w["hwnd"] for w in windows])


def _terminal_folder(folder: str) -> str | None:
    """"project", "downloads", "digidaw": where the user means, also a folder directly in home or in the project folder."""
    import system_control as sc
    said = re.sub(r"^(?:folder|direktori|dir)\s+", "", folder.strip().lower())
    target = sc._resolve_folder(said)
    if os.path.isdir(target):
        return target
    home = os.path.expanduser("~")
    wanted = said.replace(" ", "")
    candidates = []
    for parent in (os.path.join(home, "Project"), os.path.join(home, "Projects"), home):
        try:
            candidates += [os.path.join(parent, e) for e in os.listdir(parent) if e.lower() == wanted and os.path.isdir(os.path.join(parent, e))]
        except OSError:
            continue
    # the name as it was said first ("digidaw" and not "DIGIDAW"), the project folders before home
    exact = [c for c in candidates if os.path.basename(c) == wanted]
    return (exact or candidates or [None])[0]


def open_terminal_in(folder: str) -> str:
    """Open a terminal in a folder ('buka terminal di folder project', 'buka terminal di downloads')."""
    path = _terminal_folder(folder)
    if not path:
        return f"Folder {folder} tidak ditemukan, {T}."
    # (its title lower-case: a window with "DIGIDAW" in its title counts as Digidaw's own, which nothing closes)
    argv = ld.terminal_argv(path, title=(os.path.basename(path.rstrip("/")) or path).lower())
    if not argv or not ld.spawn(argv, cwd=path):
        return f"Terminal tidak bisa dibuka, {T}."
    return f"Terminal terbuka di {os.path.basename(path.rstrip('/')) or path}, {T}."


APP_AUDIO_ALIASES = {
    "discord": ("discord", "vesktop", "webcord", "webrtc"),
    "whatsapp": ("whatsapp", "whatsie"),
    "wa": ("whatsapp", "whatsie"),
    "bisnis": ("wasistlos",),
    "browser": ("firefox", "chrome", "chromium", "brave", "vivaldi", "zen"),
    "youtube": ("firefox", "chrome", "chromium", "brave", "mpv"),
    "musik": ("spotify", "mpv", "vlc", "rhythmbox", "amberol"),
    "lagu": ("spotify", "mpv", "vlc", "rhythmbox", "amberol"),
}


def _stream_Matches(item: dict, wanted: str) -> bool:
    props = item.get("properties") or {}
    fields = [ld._app_of(item), str(props.get("application.name", "")).lower(),
              str(props.get("application.process.binary", "")).lower(),
              str(props.get("node.name", "")).lower(), str(props.get("media.name", "")).lower()]
    needles = APP_AUDIO_ALIASES.get(wanted, (wanted,))
    return any(n in f for n in needles for f in fields if f)


def playing_audio_apps() -> list[str]:
    """Names of apps currently playing sound (excluding Digidaw's own voice)."""
    import persona
    names = []
    for item in ld._sink_inputs():
        if ld._own_stream(item):
            continue
        app = ld._app_of(item)
        if app:
            label = persona.brand(app)
            if label not in names:
                names.append(label)
    return names


def app_volume(app: str, level: int = -1, action: str = "") -> str:
    """Control or check the sound volume of one specific app ('kecilkan suara discord ke 20%', 'bisukan suara firefox',
    'naikkan volume spotify', 'nyalakan suara discord'). app: app name. level: 0-150 (or -1 when using action).
    action: 'mute', 'unmute', 'up', 'down', or '' (set to level, or check current volume when level is -1)."""
    import media
    import persona
    wanted = re.sub(r"^(?:aplikasi|app|suara|volume|audio)\s+", "", (app or "").strip().lower())
    if not wanted:
        return f"Aplikasi mana yang ingin diatur suaranya, {T}?"
    inputs = [i for i in ld._sink_inputs() if not ld._own_stream(i)]
    matched = [i for i in inputs if _stream_Matches(i, wanted)]
    if not matched:
        playing = playing_audio_apps()
        extra = f" Yang sedang bersuara: {', '.join(playing)}." if playing else " Belum ada aplikasi yang sedang memutar suara."
        return f"Tidak ada aliran suara dari {persona.brand(wanted)} saat ini, {T}.{extra}"
    label = persona.brand(ld._app_of(matched[0]) or wanted)
    act = (action or "").strip().lower()
    if act in ("mute", "bisu", "bisukan", "off", "mati", "matikan"):
        for item in matched:
            ld.run(["pactl", "set-sink-input-mute", str(item["index"]), "1"])
        return f"Suara {label} dibisukan, {T}."
    if act in ("unmute", "on", "nyala", "nyalakan", "hidupkan", "bunyikan"):
        for item in matched:
            ld.run(["pactl", "set-sink-input-mute", str(item["index"]), "0"])
        return f"Suara {label} dinyalakan kembali, {T}."
    app_key = ld._app_of(matched[0])
    cur = media._ducked.get(app_key) if app_key in media._ducked else ld._input_percent(matched[0])
    if level < 0 and act not in ("up", "down", "naik", "turun"):
        muted = bool(matched[0].get("mute"))
        pct = cur if cur is not None else 100
        return f"Volume {label} {pct} persen{', sedang dibisukan' if muted else ''}, {T}."
    if level >= 0:
        target = max(0, min(150, int(level)))
    else:
        step = 20 if act in ("up", "naik") else -20
        target = max(0, min(150, (cur if cur is not None else 50) + step))
    for item in matched:
        idx = str(item["index"])
        ld.run(["pactl", "set-sink-input-mute", idx, "0"])
        ld.run(["pactl", "set-sink-input-volume", idx, f"{target}%"])
        key = ld._app_of(item)
        if key and key in media._ducked:
            media._ducked[key] = target
    if media._ducked:
        media._keep_ducked()
    return f"Volume {label} diatur ke {target} persen, {T}."


def _set_mic_mute(muted: bool) -> bool:
    flag = "1" if muted else "0"
    return _ok(ld.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", flag] if shutil.which("wpctl")
                      else ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", flag]))


def _mic_is_muted() -> bool:
    if shutil.which("wpctl"):
        return "MUTED" in ld.out(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"]).upper()
    return "yes" in ld.out(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"]).lower()


def _mute_mic() -> str:
    if not _set_mic_mute(True):
        return f"Mikrofon tidak bisa dibisukan, {T}."
    return f"Mikrofon dibisukan, {T}. Tekan {config.PUSH_TO_TALK_HOTKEY.upper()} kalau ingin bicara lagi."


def unmute_for_talking() -> bool:
    """The talk hotkey / HUD mic was pressed: whoever does that wants to be heard. True when the mic was muted and now isn't."""
    if not _mic_is_muted() or not _set_mic_mute(False):
        return False
    print("[mikrofon] tombol bicara ditekan: mikrofon dinyalakan lagi")
    return True


def mic_control(action: str = "status", level: int = -1) -> str:
    """Mute, unmute, set volume, or check status of the system microphone ('bisukan mikrofon', 'nyalakan mikrofon',
    'volume mikrofon 80 persen', 'status mikrofon'). action: 'mute', 'unmute', 'status'. level: 0-100 (optional)."""
    act = (action or "status").strip().lower()
    if level >= 0:
        pct = max(0, min(100, int(level)))
        _set_mic_mute(False)
        r = ld.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{pct}%"] if shutil.which("wpctl")
                   else ["pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{pct}%"])
        if not _ok(r):
            return f"Volume mikrofon tidak bisa diatur, {T}."
        return f"Volume mikrofon diatur ke {pct} persen, {T}."
    if act in ("mute", "off", "bisu", "bisukan", "matikan"):
        # always asked: Digidaw hears through this microphone, so a misheard "matikan mic" would leave it deaf with no
        # way back by voice
        return safety.guard("membisukan mikrofon", _mute_mic, always=True,
                            asking=f"Kalau mikrofon dibisukan saya tidak bisa mendengar sampai {config.PUSH_TO_TALK_HOTKEY.upper()} ditekan. Bisukan, {T}?")
    if act in ("unmute", "on", "nyala", "nyalakan", "hidupkan"):
        if not _set_mic_mute(False):
            return f"Mikrofon tidak bisa dinyalakan, {T}."
        return f"Mikrofon sistem dinyalakan kembali, {T}."
    out = ld.out(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"]) if shutil.which("wpctl") else ""
    if out:
        muted = "MUTED" in out.upper()
        m = re.search(r"(\d+(?:\.\d+)?)", out)
        pct = round(float(m[1]) * 100) if m else 100
        return f"Mikrofon {'sedang dibisukan' if muted else 'aktif'}, volume {pct} persen, {T}."
    vol_out = ld.out(["pactl", "get-source-volume", "@DEFAULT_SOURCE@"])
    mute_out = ld.out(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"])
    m = re.search(r"(\d+)\s*%", vol_out)
    pct = int(m[1]) if m else 100
    muted = "yes" in mute_out.lower()
    return f"Mikrofon {'sedang dibisukan' if muted else 'aktif'}, volume {pct} persen, {T}."


AI_TOOLS = [click_here, wifi_power, wifi_connect, bluetooth, power_mode, do_not_disturb, audio_output, screen_power,
            fan_speed, open_script_manager, run_script, close_script_window, side_by_side, place_window, swap_windows,
            tidy_windows, focus_previous, open_on_workspace, move_all_to_workspace, close_others, open_terminal_in,
            app_volume, mic_control]
