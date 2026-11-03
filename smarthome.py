"""
Smart home: Bardi (Tuya) lamps on the home Wi-Fi, controlled directly (no cloud): "nyalakan lampu",
"redupkan lampu 30 persen", "lampu warna merah", "lampu putih hangat", "lampu mode santai".

The devices are listed in smarthome.json (name, other names, Tuya device id, IP, protocol version); each device's
Local Key (its encryption key, from the Tuya developer platform) stays in .env as TUYA_KEY_<device id>. When a lamp
got another IP from the router it is found again by its id and the new IP is saved.

Speed: opening a connection costs 1-2.5 s (protocol 3.4/3.5 negotiates a session key over several round trips, more
on a weak Wi-Fi link), a command on an open one ~0.02 s. So each lamp keeps one connection open from Digidaw's start,
with a heartbeat every KEEPALIVE seconds (the lamp drops idle ones after ~30 s), and a broken one is reopened.
A change is sent as one message with every value it needs (on + mode + brightness + colour), never as several
commands one after the other.

Bardi RGBWW bulbs use Tuya's "type B" data points: 20 on/off, 21 mode (white / colour / scene), 22 white brightness
(10-1000), 23 white temperature (0 = warm .. 1000 = cool), 24 colour as hex HHHHSSSSVVVV (hue 0-360, saturation and
value 0-1000), 25 scene data. The lamp's last known state is kept here and shown on the HUD and the phone
(events.state["lamp"]).
"""
import colorsys
import json
import os
import re
import threading
import time

import config
import metrics
import storage
import events

T = config.USER_TITLE
DEVICES_FILE = config.BASE_DIR / "smarthome.json"
TIMEOUT = 3                      # seconds per attempt: a lamp answers in well under one on the home Wi-Fi
KEEPALIVE = 10                   # seconds between heartbeats on the open connection
STATUS_EVERY = 60                # the full state this often (it also catches changes made in the Smart Life app)
SCAN_SECONDS = 6                 # listening for a lamp's broadcast (it sends one every ~5 s)
REOPEN_AFTER = 60                # seconds to wait before opening again a connection to a lamp that didn't answer
FRESH = 90                       # the kept state counts as current this long (a brightness change needs the mode)
DP_SWITCH, DP_MODE, DP_BRIGHT, DP_TEMP, DP_COLOUR, DP_SCENE = "20", "21", "22", "23", "24", "25"
# colours by their spoken name -> (r, g, b); the white ones use the lamp's own white LEDs (warmth 0 = warm .. 100 = cool)
COLOURS = {"merah": (255, 0, 0), "hijau": (0, 255, 0), "biru": (0, 0, 255), "kuning": (255, 200, 0),
           "oranye": (255, 110, 0), "jingga": (255, 110, 0), "ungu": (150, 0, 255), "pink": (255, 60, 150),
           "merah muda": (255, 60, 150), "toska": (0, 255, 200), "cyan": (0, 255, 255), "biru muda": (60, 170, 255)}
WHITES = {"putih": 50, "putih hangat": 0, "hangat": 0, "kuning hangat": 0, "putih dingin": 100, "dingin": 100,
          "netral": 50}
# lighting moods: "lampu mode santai". white = (brightness %, warmth %); colour = (r, g, b) at brightness %
SCENES = {
    "baca": {"white": (100, 60), "say": "terang untuk membaca"},
    "kerja": {"white": (100, 70), "say": "terang untuk bekerja"},
    "fokus": {"white": (100, 85), "say": "putih terang untuk fokus"},
    "santai": {"white": (40, 0), "say": "hangat dan lembut"},
    "malam": {"white": (25, 0), "say": "redup hangat"},
    "tidur": {"white": (3, 0), "say": "lampu tidur, sangat redup"},
    "film": {"colour": (40, 70, 255), "brightness": 10, "say": "redup kebiruan untuk menonton"},
    "romantis": {"colour": (255, 40, 70), "brightness": 30, "say": "merah lembut"},
    "pesta": {"party": True, "say": "warna-warni"},
}
SCENE_WORDS = {"membaca": "baca", "belajar": "fokus", "konsentrasi": "fokus", "nonton": "film", "menonton": "film",
               "bioskop": "film", "movie": "film", "rileks": "santai", "relax": "santai", "chill": "santai",
               "santuy": "santai", "party": "pesta", "disko": "pesta", "disco": "pesta", "warna warni": "pesta",
               "pelangi": "pesta", "tidur": "tidur", "kerja": "kerja", "work": "kerja", "malam": "malam",
               "romantis": "romantis", "romance": "romantis", "dinner": "romantis"}
# the lamp's own colour show for "pesta" (it runs by itself: no command every second over the Wi-Fi). Tuya scene data:
# scene number, then per colour: switch interval, change time, mode (01 = jump), H S V, white brightness, temperature
PARTY_SCENE = "05" + "".join(f"464601{h:04x}03e803e800000000" for h in (0, 120, 240, 60, 280, 180))

_lock = threading.Lock()
_conns: dict[str, tuple] = {}        # device id -> ((ip, version), connected bulb)
_down_since: dict[str, float] = {}   # device id -> when it last failed (the keep-alive leaves it alone for a while)
_state: dict[str, dict] = {}         # device id -> last known state (see _parse)
_effects: dict[str, int] = {}        # device id -> generation: a new command stops a running effect (flash, sunrise)
_last_user_change: dict[str, float] = {}   # device id -> when the user last changed it (automations stay away)
_seen: dict[str, float] = {}         # device id -> when it last answered (online on the HUD)
_last_user_off: dict[str, float] = {}      # device id -> when the user last turned it off (it stays off for them)


def _devices() -> list[dict]:
    try:
        return json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def has_lamps() -> bool:
    return bool(_devices())


def _save(devices: list[dict]):
    storage.save_json(DEVICES_FILE, devices, backup=True, mode=storage.PRIVATE, indent=2)


def _pick(name: str = "") -> dict | None:
    """The device named in `name` ("lampu kamar"), else the first one."""
    devices = _devices()
    said = (name or "").lower()
    for d in devices:
        if any(n and n in said for n in [d["name"], *d.get("aliases", [])]):
            return d
    return devices[0] if devices else None


def _label(d: dict) -> str:
    """How to say the device: "lampu kamar", not "lampu lampu kamar"."""
    return d["name"] if d["name"].lower().startswith("lampu") else f"lampu {d['name']}"


def _bulb(d: dict):
    import tinytuya
    bulb = tinytuya.BulbDevice(d["id"], d["ip"], os.environ.get(f"TUYA_KEY_{d['id']}", ""), version=d.get("version", 3.4))
    bulb.set_socketTimeout(TIMEOUT)
    bulb.set_socketRetryLimit(1)
    bulb.set_socketPersistent(True)
    # Bardi RGBWW bulbs use Tuya's type B data points (20 switch, 21 mode, 22 brightness, 24 colour): set it now,
    # else a new connection first asks the lamp for its status to guess the type (one more round trip, and
    # "Bulb not configured" when that answer is lost)
    bulb.set_bulb_type(d.get("bulb_type", "B"))
    bulb.tried_status = True
    return bulb


def _conn(d: dict):
    """The device's open connection (made now if there is none or its address changed). Call with _lock held."""
    where = (d["ip"], d.get("version", 3.4))
    saved = _conns.get(d["id"])
    if saved and saved[0] == where:
        return saved[1]
    _drop(d["id"])
    bulb = _bulb(d)
    _conns[d["id"]] = (where, bulb)
    return bulb


def _drop(device_id: str):
    saved = _conns.pop(device_id, None)
    if saved:
        try:
            saved[1].close()
        except Exception:
            pass


DOWN_MEMORY = 300                # a lamp that didn't answer this recently gets one quick try, not the whole round
ANNOUNCE = None                  # set by main (core.announce): "the lamp answers again, done"
_rescues: dict[str, dict] = {}   # device id -> the command waiting for the lamp to come back
_hint_told: set = set()          # devices whose "switch it off and on" hint was said (once per time it hangs)


def _call(name: str, action, device: dict | None = None) -> tuple[dict | None, str]:
    """Run action(bulb) on the device. Returns (device, error text or ""). Never makes the user wait long: one or
    two quick tries (a few seconds); if the lamp doesn't answer, the answer says so at once and the lamp is looked
    for in the background (a new IP after a power cut) — the command then still happens, and Digidaw says so."""
    d = device or _pick(name)
    if not d:
        return None, f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    if not os.environ.get(f"TUYA_KEY_{d['id']}"):
        return d, f"{_label(d).capitalize()} belum bisa dikendalikan: Local Key-nya belum diisi, {T}."
    with _lock:
        down_since = _down_since.get(d["id"], 0)
        recently_down = time.time() - down_since < DOWN_MEMORY
        tries = 1 if recently_down or d["id"] not in _conns else 2   # an open connection may be stale: one fresh try
        for attempt in range(tries):
            if _run(d, action) is None:
                _rescues.pop(d["id"], None)
                return d, ""
        if not recently_down:
            _down_since[d["id"]] = time.time()
    metrics.count("lampu_gagal")
    _publish(d)
    _rescue_later(d, action)
    return d, f"{_label(d).capitalize()} belum menjawab, {T}. Saya terus mencoba di belakang; perintahnya saya jalankan " \
              f"begitu lampunya tersambung lagi."


def _rescue_later(d: dict, action):
    """Background: find the lamp (its broadcast says its current IP), then run the waiting command (the newest one
    wins). Tells the user how it ended: done, or what to do (switch the lamp off and on)."""
    waiting = _rescues.get(d["id"])
    _rescues[d["id"]] = {"action": action, "at": time.time()}
    if waiting:
        return                                         # a search is already running: it takes the newest command

    def run():
        try:
            new_ip, version = _find_ip(d["id"])
            with _lock:
                job = _rescues.get(d["id"])
                if not job or time.time() - job["at"] > 120:
                    return                             # too long ago: the user has moved on
                if new_ip and (new_ip != d["ip"] or (version and version != d.get("version"))):
                    devices = _devices()
                    for x in devices:
                        if x["id"] == d["id"]:
                            x["ip"] = d["ip"] = new_ip
                            if version:
                                x["version"] = d["version"] = version
                    _save(devices)
                done = _run(d, job["action"]) is None
                if done:
                    _down_since.pop(d["id"], None)
                    _hint_told.discard(d["id"])
            _publish(d)
            if done:
                _say(f"{_label(d).capitalize()} sudah tersambung lagi, {T}. Perintah tadi sudah saya jalankan.")
            elif new_ip and d["id"] not in _hint_told:
                _hint_told.add(d["id"])
                _say(f"{_label(d).capitalize()} terlihat di Wi-Fi tapi tidak mau menerima perintah, {T}. Biasanya Wi-Fi "
                     f"lampunya macet: matikan saklarnya sekitar sepuluh detik, lalu nyalakan lagi.")
        finally:
            _rescues.pop(d["id"], None)
    threading.Thread(target=run, daemon=True, name="smarthome-rescue").start()


def _say(text: str):
    print(f"[lampu] {text}")
    if ANNOUNCE:
        try:
            ANNOUNCE(text, "info", push=False)
        except Exception:
            pass


def _run(d: dict, action) -> dict | None:
    """action(bulb) on the open connection: None when done, else the error (and the connection is dropped)."""
    try:
        result = action(_conn(d))
    except Exception as e:
        result = {"Err": "900", "Error": str(e)}
    if isinstance(result, dict) and "Err" in result:
        _drop(d["id"])
        return result
    _down_since.pop(d["id"], None)
    _seen[d["id"]] = time.time()
    if isinstance(result, dict) and isinstance(result.get("dps"), dict):
        _remember(d, result["dps"])                      # the lamp answers a change with its new values
    return None


def _find_ip(device_id: str) -> tuple[str, float | None]:
    """The device's current IP and protocol version, from its Wi-Fi broadcast ("", None if not heard)."""
    try:
        from tinytuya import scanner
        found = scanner.devices(verbose=False, scantime=SCAN_SECONDS, poll=False, byID=True, wantids=(device_id,),
                                maxdevices=1).get(device_id) or {}
        version = found.get("version")
        return found.get("ip") or "", float(version) if version else None
    except Exception:
        return "", None


# ───────────────────────────── state ─────────────────────────────

def _hsv(value: str) -> tuple[int, int, int] | None:
    """'00f003e803e8' -> (240, 1000, 1000)."""
    try:
        return int(value[0:4], 16), int(value[4:8], 16), int(value[8:12], 16)
    except (TypeError, ValueError):
        return None


def _hsv_hex(h: float, s: float, v: float) -> str:
    return f"{int(round(h)) % 361:04x}{max(0, min(1000, int(round(s)))):04x}{max(10, min(1000, int(round(v)))):04x}"


def _percent(raw: int) -> int:
    """Brightness 10-1000 -> 1-100 % (the exact inverse of _raw)."""
    return max(1, min(100, round(1 + (int(raw) - 10) / 990 * 99)))


def _raw(percent: float) -> int:
    """Brightness 1-100 % -> 10-1000."""
    return int(round(10 + (max(1, min(100, percent)) - 1) / 99 * 990))


def _parse(dps: dict) -> dict:
    """The lamp's data points as {on, mode, brightness %, warmth %, rgb}."""
    state = {"on": bool(dps.get(DP_SWITCH, dps.get("1"))), "mode": dps.get(DP_MODE, "white")}
    if DP_BRIGHT in dps:
        state["brightness"] = _percent(dps[DP_BRIGHT])
    if DP_TEMP in dps:
        state["warmth"] = round(int(dps[DP_TEMP]) / 10)
    hsv = _hsv(dps.get(DP_COLOUR, ""))
    if hsv:
        r, g, b = colorsys.hsv_to_rgb(hsv[0] / 360, hsv[1] / 1000, 1)
        state["rgb"] = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        if state["mode"] == "colour":
            state["brightness"] = max(1, round(hsv[2] / 10))
    return state


def _remember(d: dict, dps: dict):
    """Merge new data points into the kept state (partial answers only carry what changed)."""
    kept = _state.setdefault(d["id"], {"dps": {}, "at": 0.0})
    kept["dps"].update({str(k): v for k, v in dps.items()})
    kept["at"] = time.time()
    _publish(d)


def _snapshot(d: dict) -> dict:
    kept = _state.get(d["id"]) or {}
    online = d["id"] in _seen and d["id"] not in _down_since
    info = {"name": d["name"], "label": _label(d).capitalize(), "online": online}
    if kept.get("dps"):
        info.update(_parse(kept["dps"]))
    return info


_published: dict[str, dict] = {}


def _publish(d: dict):
    """Show the lamp's state on the HUD and the phone (only when it changed)."""
    info = _snapshot(d)
    if _published.get(d["id"]) == info:
        return
    _published[d["id"]] = info
    first = _devices()[:1]
    if first and first[0]["id"] == d["id"]:
        events.state["lamp"] = info
        events.emit("lamp", **info)


def _read_status(d: dict) -> tuple[dict, str]:
    """(data points, error) as the lamp reports them now. On the kept connection the first message read can be the
    lamp's own late notice about the previous change (only the values that changed, e.g. just "25"): then it is read
    once more, so "on/off" is never guessed from a partial answer."""
    box = {}
    _, error = _call("", lambda b: box.setdefault("s", b.status()), device=d)
    dps = (box.get("s") or {}).get("dps") or {}
    if not error and dps and DP_SWITCH not in dps:
        _remember(d, dps)
        box.clear()
        _, error = _call("", lambda b: box.setdefault("s", b.status()), device=d)
        dps = (box.get("s") or {}).get("dps") or {}
    if dps:
        _remember(d, dps)
    return dict((_state.get(d["id"]) or {}).get("dps") or {}), error


def state(name: str = "", fresh: bool = False) -> dict | None:
    """The lamp's state {on, mode, brightness, warmth, rgb, online}; fresh=True asks the lamp itself when the kept one
    is old. None when there is no lamp or it can't be reached."""
    d = _pick(name)
    if not d:
        return None
    kept = _state.get(d["id"]) or {}
    if fresh and time.time() - kept.get("at", 0) > FRESH:
        _, error = _read_status(d)
        if error:
            return None
    return _snapshot(d)


def _current(d: dict) -> dict:
    """The data points as they are now (asked from the lamp when the kept ones are old)."""
    kept = _state.get(d["id"]) or {}
    if time.time() - kept.get("at", 0) > FRESH or DP_SWITCH not in kept.get("dps", {}):
        _read_status(d)
    return dict((_state.get(d["id"]) or {}).get("dps") or {})


# ───────────────────────────── sending a change ─────────────────────────────

def _send(d: dict, dps: dict, by_user: bool = True) -> str:
    """Send these data points in one message. Returns the error text or ""."""
    if by_user:
        _effects[d["id"]] = _effects.get(d["id"], 0) + 1    # a running flash / sunrise stops here
        _last_user_change[d["id"]] = time.time()
    _, error = _call("", lambda b: b.set_multiple_values(dps), device=d)
    if not error:
        _remember(d, dps)
    return error


def _white(brightness: float, warmth: float) -> dict:
    return {DP_SWITCH: True, DP_MODE: "white", DP_BRIGHT: _raw(brightness), DP_TEMP: int(max(0, min(100, warmth)) * 10)}


def _colour(rgb: tuple, brightness: float = 100) -> dict:
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    return {DP_SWITCH: True, DP_MODE: "colour", DP_COLOUR: _hsv_hex(h * 360, s * 1000, max(1, min(100, brightness)) * 10)}


def _with_brightness(dps: dict, percent: float) -> dict:
    """The change that sets this brightness in the lamp's current mode (a coloured lamp stays that colour)."""
    if dps.get(DP_MODE) == "colour" and _hsv(dps.get(DP_COLOUR, "")):
        h, s, _ = _hsv(dps[DP_COLOUR])
        return {DP_SWITCH: True, DP_MODE: "colour", DP_COLOUR: _hsv_hex(h, s, max(1, min(100, percent)) * 10)}
    warmth = round(int(dps.get(DP_TEMP, 0)) / 10) if DP_TEMP in dps else None
    change = {DP_SWITCH: True, DP_MODE: "white", DP_BRIGHT: _raw(percent)}
    if warmth is not None:
        change[DP_TEMP] = warmth * 10
    return change


def set_state(name: str = "", on: bool | None = None, brightness: float | None = None, warmth: float | None = None,
              rgb: tuple | None = None, by_user: bool = False) -> str:
    """For the automations (home_auto.py): one change, quietly. Returns the error text or ""."""
    d = _pick(name)
    if not d:
        return "tidak ada lampu"
    if on is False:
        return _send(d, {DP_SWITCH: False}, by_user)
    if rgb is not None:
        return _send(d, _colour(rgb, brightness or 100), by_user)
    if warmth is not None:
        return _send(d, _white(brightness or 100, warmth), by_user)
    if brightness is not None:
        return _send(d, _with_brightness(_current(d), brightness), by_user)
    return _send(d, {DP_SWITCH: True}, by_user)


def reachable(name: str = "", within: float = 300) -> bool:
    """Not known to be out of reach lately (the automations then leave it alone instead of searching the Wi-Fi)."""
    d = _pick(name)
    return bool(d) and time.time() - _down_since.get(d["id"], 0) > within


def changed_by_user_since(t: float, name: str = "") -> bool:
    d = _pick(name)
    return bool(d) and _last_user_change.get(d["id"], 0) > t


def turned_off_by_user_since(t: float, name: str = "") -> bool:
    d = _pick(name)
    return bool(d) and _last_user_off.get(d["id"], 0) > t


# ───────────────────────────── skills ─────────────────────────────

def lamp_power(on: bool = True, name: str = "") -> str:
    """Turn a smart lamp (Bardi) on or off: "nyalakan lampu", "matikan lampu kamar".
    name: which lamp (empty = the main one)."""
    d = _pick(name)
    if not d:
        return f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    error = _send(d, {DP_SWITCH: bool(on)})
    if not error and not on:
        _last_user_off[d["id"]] = time.time()
    return error or f"{_label(d).capitalize()} {'menyala' if on else 'dimatikan'}, {T}."


def lamp_brightness(percent: int, name: str = "", relative: bool = False) -> str:
    """Set a smart lamp's brightness in percent (1-100): "redupkan lampu 30 persen", "terangkan lampu".
    relative=True: percent is a change ("lebih terang" = +30, "lebih redup" = -30)."""
    d = _pick(name)
    if not d:
        return f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    dps = _current(d)
    if relative:
        now = _parse(dps).get("brightness", 50) if dps.get(DP_SWITCH) else 0
        percent = now + int(percent)
    percent = max(1, min(100, int(percent)))
    error = _send(d, _with_brightness(dps, percent))
    return error or f"Kecerahan {_label(d)} {percent} persen, {T}."


def lamp_color(color: str, name: str = "") -> str:
    """Change a smart lamp's colour: "lampu warna merah", "lampu putih hangat", "lampu biru".
    color: merah, hijau, biru, kuning, oranye, ungu, pink, toska, putih, putih hangat, putih dingin."""
    color = re.sub(r"\s+", " ", (color or "").lower()).strip()
    d = _pick(name)
    if not d:
        return f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    if color in WHITES:
        kept = _parse(_state.get(d["id"], {}).get("dps") or {})
        brightness = kept.get("brightness", 100) if kept.get("on") and kept.get("mode") == "white" else 100
        error = _send(d, _white(brightness, WHITES[color]))
    elif color in COLOURS:
        error = _send(d, _colour(COLOURS[color]))
    else:
        return f"Warna {color} belum saya kenal, {T}. Coba merah, hijau, biru, kuning, ungu, pink, atau putih hangat."
    return error or f"{_label(d).capitalize()} sekarang {color}, {T}."


def scene_key(said: str) -> str | None:
    said = re.sub(r"\s+", " ", (said or "").lower()).strip()
    if said in SCENES:
        return said
    for word, key in SCENE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", said):
            return key
    return None


def lamp_scene(scene: str, name: str = "") -> str:
    """Set a lighting mood on the smart lamp: "lampu mode santai", "suasana film", "lampu untuk membaca".
    scene: baca, kerja, fokus, santai, malam, tidur (very dim night light), film, romantis, pesta (colour show)."""
    key = scene_key(scene)
    d = _pick(name)
    if not d:
        return f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    if not key:
        return f"Suasana {scene} belum saya kenal, {T}. Coba baca, santai, film, tidur, atau pesta."
    preset = SCENES[key]
    if preset.get("party"):
        change = {DP_SWITCH: True, DP_MODE: "scene", DP_SCENE: PARTY_SCENE}
    elif "colour" in preset:
        change = _colour(preset["colour"], preset["brightness"])
    else:
        change = _white(*preset["white"])
    error = _send(d, change)
    return error or f"{_label(d).capitalize()} mode {key}: {preset['say']}, {T}."


def lamp_status(name: str = "") -> str:
    """Is the smart lamp on, and how bright: "lampu nyala nggak", "status lampu"."""
    d = _pick(name)
    if not d:
        return f"Belum ada lampu yang disambungkan ke Digidaw, {T}."
    dps, error = _read_status(d)
    if error:
        return error
    if DP_SWITCH not in dps:
        return f"{_label(d).capitalize()} terhubung, {T}."
    s = _parse(dps)
    if not s["on"]:
        return f"{_label(d).capitalize()} sedang mati, {T}."
    if s["mode"] == "colour":
        how = f"berwarna, kecerahan {s.get('brightness', 100)} persen"
    elif s["mode"] == "scene":
        how = "dalam mode suasana"
    else:
        how = f"putih {'hangat' if s.get('warmth', 50) < 35 else 'dingin' if s.get('warmth', 50) > 65 else 'netral'}, " \
              f"kecerahan {s.get('brightness', 100)} persen"
    return f"{_label(d).capitalize()} sedang menyala, {how}, {T}."


# ───────────────────────────── effects ─────────────────────────────

def flash(times: int = 2, name: str = ""):
    """A reminder or timer is due: the lamp blinks (only when it is on; never in the dark). Runs in the background."""
    d = _pick(name)
    if not d or not os.environ.get(f"TUYA_KEY_{d['id']}"):
        return

    def run():
        kept = _parse(_current(d))
        if not kept.get("on"):
            return
        gen = _effects.get(d["id"], 0)
        for _ in range(max(1, times)):
            for on in (False, True):
                if _effects.get(d["id"], 0) != gen:
                    return                           # the user changed the lamp meanwhile: it stays as they set it
                if _send(d, {DP_SWITCH: on}, by_user=False):
                    return
                time.sleep(0.45)
    threading.Thread(target=run, daemon=True, name="lamp-flash").start()


def sunrise(seconds: float = 90, name: str = ""):
    """A wake-up alarm: the lamp comes on dim and warm and slowly gets bright, like a sunrise. Stops as soon as the
    user changes the lamp. Runs in the background."""
    d = _pick(name)
    if not d or not os.environ.get(f"TUYA_KEY_{d['id']}"):
        return

    def run():
        kept = _parse(_current(d))
        if kept.get("on") and kept.get("brightness", 0) >= 60:
            return                                   # already bright
        gen = _effects.get(d["id"], 0) + 1
        _effects[d["id"]] = gen
        steps = 12
        for i in range(steps + 1):
            if _effects.get(d["id"], 0) != gen:
                return
            percent = 2 + (100 - 2) * (i / steps) ** 1.6
            warmth = 0 + 40 * (i / steps)
            if _send(d, _white(percent, warmth), by_user=False):
                return
            time.sleep(seconds / steps)
    threading.Thread(target=run, daemon=True, name="lamp-sunrise").start()


# ───────────────────────────── keeping the connection ─────────────────────────────

def _keepalive():
    """Open each lamp's connection early and keep it open, so a spoken command doesn't wait for the handshake. The
    full state is read every STATUS_EVERY seconds (changes made in the Smart Life app show up on the HUD too)."""
    last_status: dict[str, float] = {}
    while True:
        for d in _devices():
            if not os.environ.get(f"TUYA_KEY_{d['id']}"):
                continue
            if time.time() - _down_since.get(d["id"], 0) < REOPEN_AFTER:
                continue                                 # unplugged: commands still try, the keep-alive waits
            with _lock:
                try:
                    bulb = _conn(d)
                    full = bulb.socket is None or time.time() - last_status.get(d["id"], 0) >= STATUS_EVERY
                    result = bulb.status() if full else bulb.heartbeat(nowait=False)
                    failed = isinstance(result, dict) and "Err" in result
                    if not failed:
                        _seen[d["id"]] = time.time()
                    if full and not failed and isinstance(result, dict) and isinstance(result.get("dps"), dict):
                        last_status[d["id"]] = time.time()
                        _remember(d, result["dps"])
                except Exception:
                    failed = True
                if failed:
                    _drop(d["id"])
                    _down_since[d["id"]] = time.time()
            _publish(d)
        time.sleep(KEEPALIVE)


_started = False


def start():
    global _started
    devices = _devices()
    if not _started and devices:
        _started = True
        _publish(devices[0])                         # the HUD shows the lamp (offline until it answers)
        threading.Thread(target=_keepalive, name="smarthome-keepalive", daemon=True).start()


def handle_action(action: str, value=None) -> str:
    """Buttons on the HUD and the phone: on, off, toggle, brightness (value %), color (value name), scene (value)."""
    if action == "toggle":
        kept = state() or {}
        return lamp_power(not kept.get("on"))
    if action in ("on", "off"):
        return lamp_power(action == "on")
    if action == "brightness":
        return lamp_brightness(int(float(value or 100)))
    if action == "color":
        return lamp_color(str(value or ""))
    if action == "scene":
        return lamp_scene(str(value or ""))
    if action == "refresh":
        return lamp_status()
    return f"Tombol lampu '{action}' tidak dikenal."


AI_TOOLS = [lamp_power, lamp_brightness, lamp_color, lamp_scene, lamp_status]
