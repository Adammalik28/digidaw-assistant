"""
Linux desktop integration for Digidaw (Arch Linux + Hyprland on Wayland, PipeWire audio).

Everything Windows did through user32 / pycaw / SMTC / the Start Menu is done here with standard tools:
  windows ........ hyprctl (clients, focuswindow, closewindow, sendshortcut, a hidden "minimized" workspace)
  volume ......... wpctl (PipeWire), pactl as fallback; per-app ducking through pactl sink-inputs
  media sessions . playerctl (MPRIS: Spotify, Firefox, Chrome, mpv, VLC, ...)
  clipboard ...... wl-copy / wl-paste (the PRIMARY selection = the text the user has highlighted)
  screenshots .... grim
  apps ........... .desktop files (XDG application menu), launched with gio
  folders ........ xdg-user-dir, xdg-open, the FileManager1 D-Bus interface, gio trash
Every function fails softly (returns False / "" / []) when a tool is missing.
"""
import io
import os
import re
import json
import math
import shlex
import shutil
import struct
import hashlib
import threading
import time
import subprocess
from pathlib import Path

import config

QUIET = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
MINIMIZED_WS = "special:digidaw-minimized"


def _detached() -> dict:
    return {"start_new_session": True, "close_fds": True, **QUIET}


def run(args: list[str], timeout: float = 5, input: bytes | str | None = None) -> subprocess.CompletedProcess | None:
    """Run a tool and capture its output; None when the tool is missing or hangs."""
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, input=input,
                              text=not isinstance(input, bytes))
    except (OSError, subprocess.TimeoutExpired):
        return None


def out(args: list[str], timeout: float = 5) -> str:
    r = run(args, timeout)
    return r.stdout.strip() if r and r.returncode == 0 else ""


def spawn(args: list[str], cwd: str | None = None) -> bool:
    """Start a GUI program that outlives Digidaw."""
    try:
        subprocess.Popen(args, cwd=cwd, **_detached())
        return True
    except OSError:
        return False


def which_first(*names: str) -> str | None:
    return next((n for n in names if n and shutil.which(n)), None)


# ───────────────────────────── Hyprland windows ─────────────────────────────

def hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and bool(shutil.which("hyprctl"))


def _hypr_json(what: str):
    try:
        return json.loads(out(["hyprctl", "-j", what]) or "null")
    except ValueError:
        return None


def dispatch(*args: str) -> bool:
    r = run(["hyprctl", "dispatch", *args])
    return bool(r and r.returncode == 0 and "ok" in (r.stdout or "").lower())


def _process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _window(c: dict) -> dict:
    return {"hwnd": c.get("address", ""), "title": c.get("title", ""), "process": _process_name(c.get("pid", 0)),
            "pid": c.get("pid", 0), "class": c.get("class", ""), "workspace": (c.get("workspace") or {}).get("name", ""),
            "fullscreen": c.get("fullscreen", 0), "floating": c.get("floating", False)}


def windows() -> list[dict]:
    """Mapped app windows, most recently focused first (like Windows' Z-order)."""
    clients = _hypr_json("clients") or []
    clients = [c for c in clients if c.get("mapped", True) and c.get("title") and c.get("address")]
    clients.sort(key=lambda c: c.get("focusHistoryID", 999))
    return [_window(c) for c in clients]


def active_window() -> dict | None:
    c = _hypr_json("activewindow")
    return _window(c) if isinstance(c, dict) and c.get("address") else None


def active_address() -> str:
    c = _hypr_json("activewindow")
    return c.get("address", "") if isinstance(c, dict) else ""


def window_exists(address: str) -> dict | None:
    return next((w for w in windows() if w["hwnd"] == address), None)


def _active_workspace() -> str:
    ws = _hypr_json("activeworkspace") or {}
    return str(ws.get("id", "")) or "1"


def focus(address: str) -> bool:
    """Bring a window to the front (also back from the hidden 'minimized' workspace)."""
    w = window_exists(address)
    if w and w["workspace"].startswith("special:"):
        dispatch("movetoworkspacesilent", f"{_active_workspace()},address:{address}")
    return dispatch("focuswindow", f"address:{address}")


def minimize(address: str) -> bool:
    """Hyprland has no minimize: park the window on a hidden special workspace (focus() brings it back)."""
    return dispatch("movetoworkspacesilent", f"{MINIMIZED_WS},address:{address}")


def close(address: str) -> bool:
    """Ask the window to close, like clicking X (the app may still ask to save)."""
    return dispatch("closewindow", f"address:{address}")


def maximize(address: str) -> bool:
    focus(address)
    w = window_exists(address)
    if w and w["fullscreen"] == 1:              # fullscreenstate toggles when the state is already set
        return True
    return dispatch("fullscreenstate", "1 1")   # maximized: keeps bars and gaps


def restore(address: str) -> bool:
    focus(address)
    w = window_exists(address)
    if w and not w["fullscreen"]:
        return True
    return dispatch("fullscreenstate", "0 0")   # back to a normal window


def snap(address: str, side: str) -> bool:
    """Move a window to the left / right side of the screen."""
    focus(address)
    w = window_exists(address)
    if w and w["floating"]:
        dispatch("settiled", f"address:{address}")
    return dispatch("movewindow", "l" if side == "left" else "r")


def show_desktop() -> bool:
    """Switch to an empty workspace (Hyprland's equivalent of Win+D)."""
    return dispatch("workspace", "empty")


def workspace_ids() -> list[int]:
    """Numbered workspaces that exist now (special/hidden ones left out)."""
    return sorted(w["id"] for w in (_hypr_json("workspaces") or []) if w.get("id", 0) > 0)


def active_workspace_id() -> int:
    return (_hypr_json("activeworkspace") or {}).get("id", 0)


def switch_workspace(target: str) -> bool:
    """target: a number ('4'), or a relative step ('+1' / '-1')."""
    return dispatch("workspace", target)


def move_to_workspace(address: str, workspace: str, follow: bool = False) -> bool:
    """Move a window to another workspace; follow=True also switches there."""
    return dispatch("movetoworkspace" if follow else "movetoworkspacesilent", f"{workspace},address:{address}")


_HYPR_MODS = {"ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT", "win": "SUPER", "super": "SUPER",
              "windows": "SUPER", "meta": "SUPER", "cmd": "SUPER"}
_HYPR_KEYS = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
              "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "insert": "Insert",
              "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next", "pgup": "Prior",
              "pgdn": "Next", "left": "Left", "right": "Right", "up": "Up", "down": "Down",
              "printscreen": "Print", "prtsc": "Print", "capslock": "Caps_Lock", "menu": "Menu",
              "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume", "volumemute": "XF86AudioMute",
              "playpause": "XF86AudioPlay", "nexttrack": "XF86AudioNext", "prevtrack": "XF86AudioPrev",
              **{f"f{i}": f"F{i}" for i in range(1, 25)}}


def send_keys(keys: list[str], address: str = "") -> bool:
    """Press a shortcut (pyautogui-style names: 'ctrl', 'shift', 'w', 'pagedown', 'f5') in a window
    (default: the active one). Hyprland delivers it straight to that window."""
    keys = [k.strip().lower() for k in keys if k.strip()]
    if not keys or keys[-1] in _HYPR_MODS:       # empty or a lone modifier
        return False
    mods = [_HYPR_MODS[k] for k in keys[:-1] if k in _HYPR_MODS]
    key = _HYPR_KEYS.get(keys[-1], keys[-1].upper() if len(keys[-1]) == 1 and keys[-1].isalpha() else keys[-1])
    # Hyprland 0.56 requires the window argument
    return dispatch("sendshortcut", f"{' '.join(mods)}, {key}, {f'address:{address}' if address else 'activewindow'}")


# ───────────────────────────── clipboard ─────────────────────────────

def clipboard_get(primary: bool = False) -> str:
    r = run(["wl-paste", "--no-newline", *(["--primary"] if primary else [])], timeout=2)
    return r.stdout if r and r.returncode == 0 else ""


def clipboard_set(text: str) -> bool:
    try:
        subprocess.run(["wl-copy"], input=text, text=True, timeout=3, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def clipboard_set_image(png: bytes) -> bool:
    try:
        subprocess.run(["wl-copy", "--type", "image/png"], input=png, timeout=3, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ───────────────────────────── screen ─────────────────────────────

def grab_screen():
    """All monitors as one PIL image (grim; Pillow's own grabber only works on X11)."""
    from PIL import Image
    r = None
    if shutil.which("grim"):
        try:
            r = subprocess.run(["grim", "-t", "png", "-"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            r = None
    if r and r.returncode == 0 and r.stdout:
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    from PIL import ImageGrab       # X11 session / newer Pillow with Wayland support
    return ImageGrab.grab(all_screens=True)


# ───────────────────────────── volume & brightness ─────────────────────────────

SINK = "@DEFAULT_AUDIO_SINK@"


def get_volume() -> tuple[int, bool] | None:
    """(percent, muted) of the default speaker."""
    text = out(["wpctl", "get-volume", SINK])
    m = re.search(r"Volume:\s*([\d.]+)", text)
    if m:
        return round(float(m.group(1)) * 100), "MUTED" in text
    vol = re.search(r"(\d+)%", out(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]))
    if vol:
        return int(vol.group(1)), "yes" in out(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    return None


def set_volume(percent: int) -> bool:
    percent = max(0, min(100, int(percent)))
    if shutil.which("wpctl"):
        run(["wpctl", "set-mute", SINK, "0"])
        r = run(["wpctl", "set-volume", SINK, f"{percent / 100:.2f}"])
        return bool(r and r.returncode == 0)
    run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
    r = run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"])
    return bool(r and r.returncode == 0)


def set_mute(muted: bool) -> bool:
    if shutil.which("wpctl"):
        r = run(["wpctl", "set-mute", SINK, "1" if muted else "0"])
    else:
        r = run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])
    return bool(r and r.returncode == 0)


def set_brightness(percent: int) -> bool:
    if not shutil.which("brightnessctl"):
        return False
    r = run(["brightnessctl", "--quiet", "set", f"{max(1, min(100, int(percent)))}%"])
    return bool(r and r.returncode == 0)


# ── ducking: lower other apps' streams while Digidaw talks ──

APP_NAME = "Digidaw"     # Digidaw's own beeps / alarm sounds carry this name and are never ducked


def _sink_inputs() -> list[dict]:
    try:
        return json.loads(out(["pactl", "-f", "json", "list", "sink-inputs"]) or "[]")
    except ValueError:
        return []


def _input_percent(item: dict) -> int:
    values = [int(str(ch.get("value_percent", "100%")).rstrip("%")) for ch in (item.get("volume") or {}).values()]
    return max(values) if values else 100


def _is_child(pid) -> bool:
    """A sound player Digidaw started (its streams carry the player's pid, not Digidaw's)."""
    try:
        import psutil
        return psutil.Process(int(pid)).ppid() == os.getpid()
    except Exception:
        return False


def duck(level: float) -> dict[int, int]:
    """Lower every playing stream except Digidaw's own; returns {sink_input: original percent}."""
    me = str(os.getpid())
    ducked = {}
    for item in _sink_inputs():
        props = item.get("properties") or {}
        if item.get("corked") or props.get("application.process.id") == me or props.get("application.name") == APP_NAME \
                or props.get("media.role") in ("Notification", "Alarm", "Event") \
                or props.get("application.name") in ("pw-play", "paplay") or _is_child(props.get("application.process.id")):
            continue            # Digidaw's own beeps/alarms and system notifications are never lowered
        original = _input_percent(item)
        ducked[item["index"]] = original
        run(["pactl", "set-sink-input-volume", str(item["index"]), f"{max(1, int(original * level))}%"], timeout=2)
    return ducked


def unduck(ducked: dict[int, int]):
    for index, percent in ducked.items():
        run(["pactl", "set-sink-input-volume", str(index), f"{percent}%"], timeout=2)


# ───────────────────────────── sounds ─────────────────────────────

SOUND_DIRS = [Path("/usr/share/sounds/freedesktop/stereo"), Path("/usr/share/sounds/ocean/stereo")]
SOUNDS = {"alarm": ["alarm-clock-elapsed.oga", "phone-incoming-call.oga"],
          "timer": ["complete.oga", "alarm-clock-elapsed.oga"],
          "reminder": ["message-new-instant.oga", "bell.oga"]}


def _player() -> list[str] | None:
    if shutil.which("pw-play"):
        return ["pw-play", "--media-role", "Notification", "-P", f"{{ application.name = {APP_NAME} }}"]
    if shutil.which("paplay"):
        return ["paplay", f"--property=application.name={APP_NAME}"]
    return None


def play_file(path: str, wait: bool = True) -> subprocess.Popen | None:
    player = _player()
    if not player or not os.path.exists(path):
        return None
    try:
        proc = subprocess.Popen([*player, str(path)], **QUIET)
    except OSError:
        return None
    if wait:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc


def _tone_file(tones: list[tuple[int, int]]) -> Path:
    key = hashlib.md5(repr(tones).encode()).hexdigest()[:10]
    path = config.AUDIO_DIR / f"tone_{key}.wav"
    if path.exists():
        return path
    rate, frames = 44100, bytearray()
    for freq, ms in tones:
        n = int(rate * ms / 1000)
        fade = max(1, min(n // 6, int(rate * 0.006)))        # no clicks at the edges
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            frames += struct.pack("<h", int(0.35 * 32767 * env * math.sin(2 * math.pi * freq * i / rate)))
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE", b"fmt ", 16, 1, 1, rate,
                         rate * 2, 2, 16, b"data", len(frames))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(header + bytes(frames))
    tmp.replace(path)
    return path


def play_tones(tones: list[tuple[int, int]]):
    """Short beeps (winsound.Beep on Windows). Played through Digidaw's own audio stream (the one its voice uses)
    when it is open, so the beeps are always exactly as loud as Digidaw's voice."""
    try:
        path = _tone_file(tones)
    except OSError:
        return
    try:
        import pygame
        if pygame.mixer.get_init():
            sound = pygame.mixer.Sound(str(path))
            channel = pygame.mixer.find_channel()          # a free channel: never cuts Digidaw's voice
            if channel:
                channel.play(sound)
                time.sleep(sound.get_length())
                return
    except Exception:
        pass
    play_file(str(path))


def sound_for(kind: str) -> str | None:
    for name in SOUNDS.get(kind, SOUNDS["alarm"]):
        for folder in SOUND_DIRS:
            if (folder / name).exists():
                return str(folder / name)
    return None


class LoopingSound:
    """An alarm sound repeated until stop() (winsound SND_LOOP)."""

    def __init__(self, path: str | None):
        self._stop = threading.Event()
        self._proc = None
        threading.Thread(target=self._loop, args=(path,), daemon=True, name="alarm-sound").start()

    def _loop(self, path: str | None):
        while not self._stop.is_set():
            self._proc = play_file(path, wait=False) if path else None
            if not self._proc:
                play_tones([(988, 180), (0, 70), (988, 180), (0, 400)])   # no sound theme installed
                continue
            self._proc.wait()
            self._stop.wait(0.3)

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.kill()


# ───────────────────────────── media sessions (MPRIS) ─────────────────────────────

PLAYING, PAUSED, STOPPED = 4, 5, 2          # same numbers media.py uses for Windows' SMTC
_STATUS = {"Playing": PLAYING, "Paused": PAUSED, "Stopped": STOPPED}
# Chat apps register a media session for voice notes / calls; WasIstLos (WhatsApp) even reports "Playing"
# all the time. They are never what "putar / pause / lagu berikutnya" means, so they are left out.
NOT_MEDIA = re.compile(r"whatsapp|wasistlos|whatsie|zapzap|telegram|discord|vesktop|webcord|slack|signal|"
                       r"teams|skype|element|kdeconnect", re.I)
_FMT = "{{playerInstance}}\t{{status}}\t{{xesam:title}}\t{{xesam:artist}}\t{{position}}\t{{mpris:length}}"


def media_sessions(timeout: float = 3.0) -> list[dict] | None:
    """Every MPRIS player, playing ones first. None if playerctl is missing or hangs."""
    if not shutil.which("playerctl"):
        return None
    r = run(["playerctl", "--all-players", "metadata", "--format", _FMT], timeout)
    if r is None:
        return None
    sessions = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 6 or not parts[0] or NOT_MEDIA.search(parts[0]) or NOT_MEDIA.search(parts[2]):
            continue
        try:
            position = int(parts[4] or 0) / 1e6
            duration = int(parts[5] or 0) / 1e6
        except ValueError:
            position = duration = 0
        sessions.append({"aumid": parts[0], "title": parts[2], "artist": parts[3],
                         "status": _STATUS.get(parts[1], 0), "position": position, "duration": duration})
    sessions.sort(key=lambda s: s["status"] != PLAYING)
    return sessions


def media_action(instance: str, action: str) -> bool:
    op = {"pause": "pause", "play": "play", "toggle": "play-pause", "next": "next", "previous": "previous"}[action]
    r = run(["playerctl", "--player", instance, op], timeout=3)
    return bool(r and r.returncode == 0)


def media_key(action: str) -> bool:
    """The media-key fallback: playerctl's own pick, but never a chat app."""
    op = {"toggle": "play-pause", "next": "next", "previous": "previous", "pause": "pause", "play": "play"}[action]
    ignored = ",".join(sorted({line.split(".instance")[0] for line in out(["playerctl", "--list-all"]).splitlines()
                               if NOT_MEDIA.search(line)}))
    r = run(["playerctl", *(["--ignore-player", ignored] if ignored else []), op], timeout=3)
    return bool(r and r.returncode == 0)


def player_label(instance: str) -> str:
    low = instance.lower()
    for marker, label in (("spotify", "Spotify"), ("firefox", "Firefox"), ("brave", "Brave"), ("chrom", "Chrome"),
                          ("edge", "Edge"), ("vlc", "VLC"), ("mpv", "mpv"), ("rhythmbox", "Rhythmbox"),
                          ("elisa", "Elisa"), ("celluloid", "Celluloid"), ("kdeconnect", "KDE Connect")):
        if marker in low:
            return label
    return instance.split(".")[0].title() or "Media"


# ───────────────────────────── apps (.desktop files) ─────────────────────────────

def _application_dirs() -> list[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [data_home / "applications"] + [Path(d) / "applications" for d in data_dirs.split(":") if d]
    dirs += [data_home / "flatpak" / "exports" / "share" / "applications",
             Path("/var/lib/flatpak/exports/share/applications")]
    seen, result = set(), []
    for d in dirs:
        if d.is_dir() and d.resolve() not in seen:
            seen.add(d.resolve())
            result.append(d)
    return result


def _is_waydroid_app(entry: dict) -> bool:
    return "X-WayDroid-App" in entry.get("Categories", "") or entry["Exec"].startswith("waydroid app ")


def _parse_desktop(path: Path) -> dict | None:
    entry, in_main = {}, False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
            continue
        if in_main and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())
    if entry.get("Type") != "Application" or entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
        return None
    if not entry.get("Name") or not entry.get("Exec"):
        return None
    if _is_waydroid_app(entry):
        return None      # Android apps in Waydroid: "buka tiktok" must open the website, not the Android app
    return {"Name": entry["Name"], "NameID": entry.get("Name[id]", ""), "GenericName": entry.get("GenericName", ""),
            "AppID": path.stem, "Path": str(path), "Exec": entry["Exec"]}


def desktop_apps() -> list[dict]:
    """Apps in the application menu (the Start Menu list on Windows). The user's own entries win."""
    apps, ids = [], set()
    for folder in _application_dirs():
        for path in sorted(folder.rglob("*.desktop")):
            if path.stem in ids:
                continue
            app = _parse_desktop(path)
            if app:
                ids.add(path.stem)
                apps.append(app)
    return apps


def _exec_argv(exec_line: str) -> list[str]:
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        parts = exec_line.split()
    return [p for p in parts if not re.fullmatch(r"%[fFuUdDnNickvm]", p)]


def launch_app(app: dict) -> bool:
    """Start a .desktop application the way the app menu does."""
    if shutil.which("gio"):
        # no captured output: the app inherits gio's stdout, and a captured pipe would keep this call waiting
        # until the timeout (and then the app was started a second time)
        try:
            r = subprocess.run(["gio", "launch", app["Path"]], timeout=5, start_new_session=True, **QUIET)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True          # gio is still busy starting the app: it is coming
    argv = _exec_argv(app["Exec"])
    return bool(argv) and spawn(argv)


# ───────────────────────────── folders, files, editors, terminals ─────────────────────────────

_XDG_DIRS = {"documents": "DOCUMENTS", "downloads": "DOWNLOAD", "desktop": "DESKTOP", "pictures": "PICTURES",
             "music": "MUSIC", "videos": "VIDEOS"}


def known_folder(name: str) -> Path:
    path = out(["xdg-user-dir", _XDG_DIRS.get(name, name.upper())])
    if path and Path(path) != Path.home():
        return Path(path)
    return Path.home() / name.capitalize()


def file_manager() -> str:
    """Process name of the default file manager ('thunar', 'nautilus', 'dolphin', ...)."""
    desktop = out(["xdg-mime", "query", "default", "inode/directory"]).removesuffix(".desktop")
    return desktop.split(".")[-1].lower() if desktop else ""


def open_in_file_manager(target: str) -> bool:
    if target.startswith(("trash:", "computer:", "recent:")):
        return spawn(["gio", "open", target]) if shutil.which("gio") else spawn(["xdg-open", target])
    return spawn(["xdg-open", target])


def reveal_in_file_manager(path: str) -> bool:
    uri = Path(path).resolve().as_uri()
    r = run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
             "--object-path", "/org/freedesktop/FileManager1", "--method", "org.freedesktop.FileManager1.ShowItems",
             f"['{uri}']", ""], timeout=4)
    if r and r.returncode == 0:
        return True
    return open_in_file_manager(str(Path(path).parent))


TEXT_EDITORS = ("mousepad", "gnome-text-editor", "gedit", "kate", "kwrite", "xed", "pluma", "featherpad", "leafpad")


def text_editor() -> str | None:
    return which_first(*TEXT_EDITORS)


def open_text_editor(path: str) -> bool:
    editor = text_editor()
    return spawn([editor, path]) if editor else spawn(["xdg-open", path])


TERMINALS = ("kitty", "alacritty", "foot", "wezterm", "ghostty", "konsole", "gnome-terminal", "xfce4-terminal", "xterm")


def terminal() -> str | None:
    term = os.environ.get("TERMINAL", "")
    return which_first(term.split()[0] if term else "", *TERMINALS)


def terminal_argv(cwd: str, command: str = "", title: str = "") -> list[str] | None:
    """A terminal window in `cwd`, optionally running `command` and staying open afterwards."""
    term = terminal()
    if not term:
        return None
    shell = os.environ.get("SHELL", "/bin/bash")
    inner = [shell, "-c", f"{command}; exec {shlex.quote(shell)}"] if command else [shell]
    name = os.path.basename(term)
    title = title or "Digidaw"
    if name == "kitty":
        return [term, "--title", title, "--directory", cwd, *inner]
    if name == "alacritty":
        return [term, "--title", title, "--working-directory", cwd, "-e", *inner]
    if name == "foot":
        return [term, "--title", title, "--working-directory", cwd, *inner]
    if name == "wezterm":
        return [term, "start", "--cwd", cwd, "--", *inner]
    if name == "ghostty":
        return [term, f"--title={title}", f"--working-directory={cwd}", "-e", *inner]
    if name == "konsole":
        return [term, "--workdir", cwd, "-e", *inner]
    if name == "gnome-terminal":
        return [term, "--title", title, "--working-directory", cwd, "--", *inner]
    if name == "xfce4-terminal":
        return [term, "--title", title, "--working-directory", cwd, "-x", *inner]
    return [term, "-T", title, "-e", *inner]


def open_terminal(cwd: str) -> bool:
    argv = terminal_argv(cwd)
    return bool(argv) and spawn(argv, cwd=cwd)


# ───────────────────────────── trash ─────────────────────────────

def _trash_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash" / "files"]
    try:
        import psutil
        uid = os.getuid()
        for part in psutil.disk_partitions(all=False):
            candidate = Path(part.mountpoint) / f".Trash-{uid}" / "files"
            if candidate.is_dir():
                dirs.append(candidate)
    except Exception:
        pass
    return [d for d in dirs if d.is_dir()]


def trash_info() -> tuple[int, int]:
    """(items, bytes) in the trash of every mounted drive."""
    items = size = 0
    for folder in _trash_dirs():
        for entry in folder.iterdir():
            items += 1
            try:
                if entry.is_dir() and not entry.is_symlink():
                    size += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file() and not f.is_symlink())
                else:
                    size += entry.lstat().st_size
            except OSError:
                continue
    return items, size


def empty_trash() -> bool:
    if shutil.which("gio"):
        r = run(["gio", "trash", "--empty"], timeout=120)
        if r and r.returncode == 0:
            return True
    ok = True
    for folder in _trash_dirs():
        for entry in folder.iterdir():
            try:
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            except OSError:
                ok = False
        info = folder.parent / "info"
        for f in info.glob("*.trashinfo") if info.is_dir() else []:
            f.unlink(missing_ok=True)
    return ok


def move_to_trash(path: str) -> tuple[bool, str]:
    r = run(["gio", "trash", path], timeout=60)
    if r is None:
        return False, "gio tidak tersedia"
    return r.returncode == 0, (r.stderr or "").strip()


# ───────────────────────────── system ─────────────────────────────

def default_browser() -> str:
    """'firefox', 'chrome', 'brave', 'edge', 'chromium' or ''."""
    desktop = out(["xdg-settings", "get", "default-web-browser"]).lower()
    for marker, name in (("firefox", "firefox"), ("brave", "brave"), ("edge", "edge"), ("chromium", "chromium"),
                         ("chrome", "chrome"), ("opera", "opera"), ("vivaldi", "vivaldi")):
        if marker in desktop:
            return name
    return ""


BROWSER_COMMANDS = {
    "chrome": ("google-chrome-stable", "google-chrome", "chrome"),
    "edge": ("microsoft-edge-stable", "microsoft-edge"),
    "brave": ("brave", "brave-browser", "brave-browser-stable"),
    "chromium": ("chromium", "chromium-browser"),
    "firefox": ("firefox", "firefox-developer-edition", "librewolf"),
    "opera": ("opera",),
    "vivaldi": ("vivaldi-stable", "vivaldi"),
}


def browser_command(family: str) -> str | None:
    return which_first(*BROWSER_COMMANDS.get(family, ()))


def wifi_name() -> str | None:
    """SSID of the connected WiFi ('' = not on WiFi, None = unknown)."""
    ssid = out(["iwgetid", "-r"])
    if ssid:
        return ssid
    if shutil.which("nmcli"):
        for line in out(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
            if line.startswith("yes:"):
                return line[4:]
        return ""
    return None


def lock_screen() -> bool:
    import psutil
    if any(p.info["name"] == "hypridle" for p in psutil.process_iter(["name"])):
        r = run(["loginctl", "lock-session"])
        if r and r.returncode == 0:
            return True
    if shutil.which("hyprlock"):
        return spawn(["hyprlock"])
    r = run(["loginctl", "lock-session"])
    return bool(r and r.returncode == 0)


def log_out() -> bool:
    if shutil.which("uwsm") and os.environ.get("XDG_SESSION_DESKTOP", "").endswith("uwsm") or \
            "uwsm" in os.environ.get("DESKTOP_SESSION", ""):
        return spawn(["uwsm", "stop"])
    if hyprland():
        return dispatch("exit")
    session = os.environ.get("XDG_SESSION_ID")
    return spawn(["loginctl", "terminate-session", session]) if session else False


def notify(title: str, text: str, urgency: str = "normal") -> bool:
    return spawn(["notify-send", "-a", "Digidaw", "-u", urgency, title, text]) if shutil.which("notify-send") else False


class quiet_stderr:
    """PortAudio probes every ALSA/JACK plugin at start and the C libraries print dozens of harmless errors
    straight to file descriptor 2. Silence it for the duration of the block. (A ctypes handler for
    snd_lib_error_set_handler crashes PyAudio: the C handler is variadic.)"""

    def __enter__(self):
        try:
            self._saved = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
            os.close(devnull)
        except OSError:
            self._saved = None
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            os.dup2(self._saved, 2)
            os.close(self._saved)
