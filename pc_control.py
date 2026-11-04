"""
Digidaw's hands on this PC beyond opening apps: the mouse and the system's settings (Linux with Hyprland).

The mouse: on Wayland no program may move the real pointer, but Hyprland moves it on request (movecursor) and a virtual
input device can click. Screen captures come from grim. Coordinates: screen_agent.py gets them relative to the picture
of the focused monitor; to_layout() turns them into Hyprland's coordinates (monitor position and scale included).

Settings: Wi-Fi, Bluetooth, power mode, Do Not Disturb, the audio output, the screen. Turning Wi-Fi off asks first:
it also takes away Digidaw's own hearing and voice (both use the internet).
"""
import difflib
import io
import json
import re
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
_mouse_lock = threading.Lock()


# ───────────────────────────── seeing ─────────────────────────────

def focused_monitor() -> dict:
    """The monitor in front: {name, x, y, width, height, scale} (position in Hyprland's layout)."""
    monitors = json.loads(ld.out(["hyprctl", "monitors", "-j"]) or "[]")
    if not monitors:
        raise RuntimeError("monitor tidak terbaca (bukan Hyprland?)")
    return next((m for m in monitors if m.get("focused")), monitors[0])


def capture(max_width: int = CAPTURE_WIDTH) -> tuple[bytes, dict]:
    """(JPEG of the focused monitor, that monitor). Only this monitor: the fewer other things it shows, the better."""
    from PIL import Image
    monitor = focused_monitor()
    r = ld.run_bytes(["grim", "-o", monitor["name"], "-t", "png", "-"], timeout=10)
    if not r:
        raise RuntimeError("layar tidak bisa dipotret (grim)")
    image = _latest["picture"] = Image.open(io.BytesIO(r)).convert("RGB")
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
    """Turn the screen off or on without sleeping ("matikan layar"); moving the mouse brings it back."""
    threading.Timer(1.5 if not on else 0, lambda: ld.dispatch("dpms", "on" if on else "off")).start()
    return f"Layar saya {'nyalakan' if on else 'matikan'}, {T}."


AI_TOOLS = [click_here, wifi_power, wifi_connect, bluetooth, power_mode, do_not_disturb, audio_output, screen_power]
