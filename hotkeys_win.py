"""Global hotkeys that work from any app (talk / interrupt Digidaw, show the HUD)."""
import ctypes
import threading
import time
from ctypes import wintypes

import config

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
SPECIAL_KEYS = {"space": 0x20, "enter": 0x0D, "pause": 0x13, "home": 0x24, "end": 0x23,
                **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def parse(combo: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    key = parts[-1]
    mods = MOD_NOREPEAT
    for p in parts[:-1]:
        mods |= MODIFIERS[p]
    vk = SPECIAL_KEYS.get(key) or ord(key.upper())
    return mods, vk


def _fullscreen_foreground() -> bool:
    """Is a game / full-screen video in front? Esc belongs to it then."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    monitor = user32.MonitorFromWindow(hwnd, 2)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    covers = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    WS_CAPTION = 0x00C00000
    style = user32.GetWindowLongW(hwnd, -16)
    return covers and (style & WS_CAPTION) != WS_CAPTION     # maximized windows keep their title bar


def watch_escape(callback, mode: str = "single") -> bool:
    """Call callback when Esc is pressed anywhere (double = two presses within 0.45 s). The key is
    only watched, never taken: Esc keeps working normally in every app. Ignored while a game or
    full-screen video is in front, and when combined with Ctrl/Alt/Shift/Win."""
    if mode not in ("single", "double"):
        return False
    user32 = ctypes.windll.user32
    VK_ESCAPE, MODS = 0x1B, (0x10, 0x11, 0x12, 0x5B, 0x5C)

    def loop():
        down, last_press = False, 0.0
        while True:
            pressed = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
            if pressed and not down:
                now = time.time()
                modified = any(user32.GetAsyncKeyState(k) & 0x8000 for k in MODS)
                if not modified and not _fullscreen_foreground():
                    if mode == "single" or now - last_press < 0.45:
                        last_press = 0.0
                        threading.Thread(target=callback, daemon=True).start()
                    else:
                        last_press = now
            down = pressed
            time.sleep(0.02)

    threading.Thread(target=loop, daemon=True, name="digidaw-esc").start()
    return True


def stop():
    """Windows frees RegisterHotKey registrations when the process exits."""


def start(callback, combo: str | None = None) -> bool:
    """Register the hotkey on a background thread. Returns False if another app already owns it."""
    combo = combo or config.PUSH_TO_TALK_HOTKEY
    try:
        mods, vk = parse(combo)
    except (KeyError, TypeError):
        return False
    registered = threading.Event()
    result = {"ok": False}

    def loop():
        user32 = ctypes.windll.user32
        result["ok"] = bool(user32.RegisterHotKey(None, 1, mods, vk))
        registered.set()
        if not result["ok"]:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                threading.Thread(target=callback, daemon=True).start()

    threading.Thread(target=loop, daemon=True, name="digidaw-hotkey").start()
    registered.wait(2)
    return result["ok"]
