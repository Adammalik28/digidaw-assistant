"""Global hotkeys on Linux (Hyprland): talk / interrupt Digidaw, show the HUD.

Wayland doesn't let an app grab keys, so Digidaw asks Hyprland for the binds (hyprctl keyword bind). A bind
runs this file as a tiny client that sends the hotkey's name to Digidaw over a Unix socket. Binds made with
hyprctl disappear when the Hyprland config is reloaded, so a watchdog puts them back.

Run as a script:  python hotkeys_linux.py ctrl+alt+d     (what the Hyprland bind executes)
"""
import os
import sys
import socket
import tempfile
from pathlib import Path

SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "digidaw-hotkeys.sock"


def _send(name: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(name.encode(), str(SOCKET))
        return True
    except OSError:
        return False


if __name__ == "__main__":                 # the Hyprland bind: stay tiny and fast
    sys.exit(0 if len(sys.argv) > 1 and _send(sys.argv[1]) else 1)


import json
import shlex
import threading
import time

import config
import linux_desktop as ld

MODIFIERS = {"alt": "ALT", "ctrl": "CTRL", "control": "CTRL", "shift": "SHIFT", "win": "SUPER", "super": "SUPER"}
SPECIAL_KEYS = {"space": "space", "enter": "Return", "pause": "Pause", "home": "Home", "end": "End",
                **{f"f{i}": f"F{i}" for i in range(1, 13)}}
MARKER = "hotkeys_linux.py"

_callbacks: dict[str, object] = {}
_binds: dict[str, tuple[str, str]] = {}
_lock = threading.Lock()
_started = {"server": False, "watchdog": False}


def parse(combo: str) -> tuple[str, str]:
    """'ctrl+alt+d' -> ('CTRL ALT', 'D'). Raises KeyError for an unknown modifier / key."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    key = parts[-1]
    mods = " ".join(MODIFIERS[p] for p in parts[:-1])
    if key in SPECIAL_KEYS:
        return mods, SPECIAL_KEYS[key]
    if len(key) != 1 or not key.isalnum():
        raise KeyError(key)
    return mods, key.upper()


def _name(combo: str) -> str:
    return "+".join(p.strip().lower() for p in combo.split("+") if p.strip())


def _bind(name: str, mods: str, key: str) -> bool:
    ld.run(["hyprctl", "keyword", "unbind", f"{mods}, {key}"])
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} {shlex.quote(name)}"
    r = ld.run(["hyprctl", "keyword", "bind", f"{mods}, {key}, exec, {command}"])
    return bool(r and r.returncode == 0 and "ok" in (r.stdout or ""))


def _serve():
    SOCKET.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(SOCKET))
    os.chmod(SOCKET, 0o600)
    while True:
        try:
            name = srv.recv(256).decode(errors="replace").strip()
        except OSError:
            time.sleep(1)
            continue
        callback = _callbacks.get(name)
        if callback:
            threading.Thread(target=callback, daemon=True).start()


def _ours_present() -> set[str]:
    try:
        binds = json.loads(ld.out(["hyprctl", "-j", "binds"]) or "[]")
    except ValueError:
        return set()
    return {b.get("arg", "").rsplit(" ", 1)[-1].strip("'") for b in binds if MARKER in b.get("arg", "")}


def _watchdog():
    """Put the binds back after a Hyprland config reload."""
    while True:
        time.sleep(30)
        with _lock:
            wanted = dict(_binds)
        if not wanted:
            continue
        present = _ours_present()
        for name, (mods, key) in wanted.items():
            if name not in present:
                _bind(name, mods, key)


def start(callback, combo: str | None = None) -> bool:
    """Register the hotkey with Hyprland. Returns False when not on Hyprland or the combo is invalid."""
    combo = combo or config.PUSH_TO_TALK_HOTKEY
    try:
        mods, key = parse(combo)
    except (KeyError, TypeError, IndexError):
        return False
    if not ld.hyprland():
        return False
    name = _name(combo)
    with _lock:
        _callbacks[name] = callback
        if not _started["server"]:
            _started["server"] = True
            threading.Thread(target=_serve, daemon=True, name="digidaw-hotkeys").start()
    if not _bind(name, mods, key):
        return False
    with _lock:
        _binds[name] = (mods, key)
        if not _started["watchdog"]:
            _started["watchdog"] = True
            threading.Thread(target=_watchdog, daemon=True, name="digidaw-hotkeys-watchdog").start()
    return True


def stop():
    """Remove Digidaw's binds from Hyprland (on exit), so the keys don't point at a closed Digidaw."""
    with _lock:
        binds = dict(_binds)
        _binds.clear()
    for mods, key in binds.values():
        ld.run(["hyprctl", "keyword", "unbind", f"{mods}, {key}"])
    SOCKET.unlink(missing_ok=True)


def watch_escape(callback, mode: str = "single") -> bool:
    """Not available on Wayland (apps can't watch keys of other apps); Esc-to-talk works in the HUD window."""
    return False
