"""
Is the user at the computer? A butler doesn't report to an empty room: while you are away the small news (a finished
image, a full battery, new messages) waits, and when you are back Digidaw tells you what happened in one breath.

Away = no keyboard, mouse or touchpad input and no command (voice, HUD, phone) for config.AWAY_MINUTES.
- Linux: the keyboards and pointers in /dev/input are watched (the user is in the "input" group, as for the phone
  HUD's mouse). Only *that* something was pressed is noted, never which key: the bytes are read and thrown away.
- Windows: GetLastInputInfo.
- Without access to either, the user counts as always present, so nothing is ever held back.
Input while the screen is locked (typing the password) doesn't count as being back until the lock screen is gone.
"""
import ctypes
import os
import re
import select
import threading
import time

import config
import oscompat

POLL_SECONDS = 2
RESCAN_SECONDS = 30            # keyboards and mice come and go (USB receiver, Bluetooth)
MAX_HELD = 6
EV_REP = 1 << 20               # keyboards repeat keys; power buttons, lid switches and headset keys don't
LOCKERS = {"hyprlock", "swaylock", "gtklock", "waylock", "i3lock", "xsecurelock", "kscreenlocker_greet"}

_last_active = time.time()
_away_since = 0.0              # when the user left (the time of their last activity); 0 = here
_media_while_away = False      # something played meanwhile: maybe they were watching, not gone
_held: list[str] = []
_listeners: list = []          # fn(seconds_away, how, media_played, left_at); how = "input" | "command"
_lock = threading.Lock()
_watching = False              # input can be seen at all (otherwise nobody is ever "away")
_started = False


def on_return(fn):
    """fn(seconds_away, how, media_played, left_at) runs (on its own thread) when the user is back."""
    _listeners.append(fn)


def is_away() -> bool:
    return bool(_away_since)


def away_for() -> float:
    """Seconds since the user left (0 while they are here)."""
    return time.time() - _away_since if _away_since else 0.0


def idle_for() -> float:
    """Seconds since the last keyboard / mouse input or command."""
    return time.time() - _last_active


# ───────────────────────────── someone in the room, a bad moment to talk ─────────────────────────────

_voice_at = 0.0                # the microphone last heard someone talking (any phrase, for Digidaw or not)


def note_voice(at: float | None = None):
    """stt.py: the microphone picked up a phrase. Away from the laptop isn't out of the room: someone talking nearby
    (on the phone, with others, a TV the user watches) means the room isn't empty."""
    global _voice_at
    _voice_at = max(_voice_at, at or time.time())


def room_quiet_for() -> float:
    """Seconds since the microphone last heard anyone talk (since Digidaw started, when it never did)."""
    import events
    return time.time() - (_voice_at or events.state.get("started", time.time()))


CALL_CHECK_EVERY = 5
NOT_A_CALL = {"cava", "pavucontrol", "easyeffects", "pwvucontrol", "helvum", "qpwgraph", "wireplumber", "pipewire",
              "speech-dispatcher", "pulseaudio", "plasma-pa", "gnome-control-center", "waybar"}
_call = {"at": 0.0, "app": ""}


def in_call() -> str:
    """Another app is using the microphone right now (Discord, Meet or Zoom in the browser, a voice note): its name,
    or "". Digidaw's own microphone, the echo canceller and sound visualizers (which listen to the speakers) don't
    count."""
    if time.time() - _call["at"] < CALL_CHECK_EVERY:
        return _call["app"]
    app = ""
    if oscompat.IS_LINUX:
        import json
        import shutil
        import subprocess
        if shutil.which("pactl"):
            def listing(kind):
                out = subprocess.run(["pactl", "-f", "json", "list", kind], capture_output=True, text=True, timeout=3)
                return json.loads(out.stdout or "[]")
            try:
                sources = {str(x.get("index")): x.get("name", "") for x in listing("sources")}
                owners = {str(c.get("index")): str((c.get("properties") or {}).get("application.process.id", ""))
                          for c in listing("clients")}
                me = str(os.getpid())
                for stream in listing("source-outputs"):
                    props = stream.get("properties") or {}
                    client = stream.get("client")
                    if client is None or stream.get("corked") or owners.get(str(client)) == me:
                        continue
                    if sources.get(str(stream.get("source")), "").endswith(".monitor"):
                        continue                        # it listens to the speakers, not to the user
                    name = props.get("application.name") or props.get("application.process.binary") or ""
                    if name and name.lower() not in NOT_A_CALL:
                        app = name
                        break
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    _call.update(at=time.time(), app=app)
    return app


def _really_fullscreen(state) -> bool:
    """Hyprland: 1 = maximized (bars still there, the user just likes big windows), 2 = real fullscreen (a film, a
    game). Elsewhere a plain True."""
    if isinstance(state, bool):
        return state
    try:
        return int(state or 0) >= 2
    except (TypeError, ValueError):
        return False


def _is_digidaw(window: dict) -> bool:
    try:
        import window_manager
        return bool(window_manager._is_digidaw(window))
    except Exception:
        return "digidaw" in str(window.get("title", "")).lower()


def busy_reason(front: dict | None = None) -> str:
    """Why speaking up now would interrupt the user: "Do Not Disturb", "layar penuh" (a film or a game), "sedang
    panggilan" (another app has the microphone), or "" when it's fine to talk."""
    try:
        import notifications
        if notifications._do_not_disturb():
            return "Do Not Disturb"
    except Exception:
        pass
    if front is None:
        try:
            import screen_context
            front = screen_context.foreground()
        except Exception:
            front = None
    if front and _really_fullscreen(front.get("fullscreen")) and not _is_digidaw(front):
        return "layar penuh"            # (Digidaw's own HUD shown fullscreen is no film)
    app = in_call()
    return f"sedang panggilan di {app}" if app else ""


def touch(how: str = "command"):
    """The user did something Digidaw saw (a voice, HUD or phone command): they are here."""
    _notify(_activity(time.time(), how))


def hold(text: str):
    """Keep a piece of news for when the user is back."""
    with _lock:
        _held.append(text)
        del _held[:-MAX_HELD]


def take_held() -> list[str]:
    with _lock:
        items = _held[:]
        _held.clear()
    return items


# ───────────────────────────── state ─────────────────────────────

def _activity(now: float, how: str, locked=None):
    """Note activity. Returns (seconds_away, how, media_played, left_at) when this is the user coming back."""
    global _last_active, _away_since
    with _lock:
        _last_active = now
        if not _away_since:
            return None
    if how == "input" and (locked if locked is not None else _locked()):
        return None                          # typing the password on the lock screen: not back yet
    with _lock:
        left_at, _away_since = _away_since, 0.0
        if not left_at:
            return None                      # another thread saw the return first
        return now - left_at, how, _media_while_away, left_at


def _check(now: float, playing=None):
    """Called every poll: has the user been gone long enough to count as away?"""
    global _away_since, _media_while_away
    limit = config.AWAY_MINUTES * 60
    with _lock:
        if not _away_since and limit > 0 and _watching and now - _last_active >= limit:
            _away_since, _media_while_away = _last_active, False
        away = bool(_away_since)
    if away and not _media_while_away and (playing if playing is not None else _media_playing()):
        _media_while_away = True


def _notify(info):
    if not info:
        return
    for fn in list(_listeners):
        threading.Thread(target=fn, args=info, daemon=True, name="presence-return").start()


def _media_playing() -> bool:
    try:
        import media
        return media.is_playing()
    except Exception:
        return False


def _locked() -> bool:
    if oscompat.IS_WINDOWS:
        return False                         # Windows doesn't report input made on the lock screen at all
    try:
        import psutil
        return any(p.info["name"] in LOCKERS for p in psutil.process_iter(["name"]))
    except Exception:
        return False


# ───────────────────────────── watching the input devices ─────────────────────────────

def input_devices(listing: str | None = None) -> list[str]:
    """/dev/input paths of the keyboards and pointers (from /proc/bus/input/devices): a "mouse" handler (mouse,
    touchpad), or a "kbd" handler on a device that repeats keys (a real keyboard, not a power button)."""
    if listing is None:
        try:
            with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as f:
                listing = f.read()
        except OSError:
            return []
    paths = []
    for block in listing.split("\n\n"):
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        bits = re.search(r"^B: EV=([0-9a-f]+)\s*$", block, re.M)
        names = handlers[1].split() if handlers else []
        event = next((n for n in names if re.fullmatch(r"event\d+", n)), None)
        if not event:
            continue
        repeats = bool(int(bits[1], 16) & EV_REP) if bits else False
        if any(n.startswith("mouse") for n in names) or ("kbd" in names and repeats):
            paths.append(f"/dev/input/{event}")
    return paths


def _watch_linux():
    global _watching
    fds: dict[str, int] = {}
    rescan = 0.0
    while True:
        now = time.time()
        if now >= rescan:
            rescan = now + RESCAN_SECONDS
            wanted = set(input_devices())
            for path in [p for p in fds if p not in wanted]:
                os.close(fds.pop(path))
            for path in wanted - fds.keys():
                try:
                    fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:
                    pass                     # no permission (not in the "input" group): this device isn't watched
            _watching = bool(fds)
        try:
            ready = select.select(list(fds.values()), [], [], 0)[0] if fds else []
        except (OSError, ValueError):
            ready, rescan = [], 0
        for fd in ready:
            try:
                while os.read(fd, 4096):     # only *that* there was input matters, so the events are dropped
                    pass
            except BlockingIOError:
                pass
            except OSError:
                rescan = 0                   # unplugged
        if ready:
            _notify(_activity(now, "input"))
        _check(now)
        time.sleep(POLL_SECONDS)


def _watch_windows():
    global _watching

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    last = None
    _watching = True
    while True:
        now = time.time()
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            if last is not None and info.dwTime != last:
                _notify(_activity(now, "input"))
            last = info.dwTime
        _check(now)
        time.sleep(POLL_SECONDS)


def start():
    global _started
    if _started:
        return
    _started = True
    target = _watch_windows if oscompat.IS_WINDOWS else _watch_linux
    threading.Thread(target=target, daemon=True, name="presence").start()
