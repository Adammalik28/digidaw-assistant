"""Window management skills: switch to, minimize, maximize, snap and gracefully close windows."""
import os
import time
import ctypes

import psutil
import win32api
import win32con
import win32gui
import win32process

import config

T = config.USER_TITLE
DIGIDAW_MARKERS = ("DIGIDAW",)   # console + HUD windows are skipped for "active window" actions
IGNORED_TITLES = {"Program Manager", "Windows Input Experience", "Settings", "Pengaturan"}


def _is_cloaked(hwnd) -> bool:
    """UWP apps keep invisible 'cloaked' windows around; skip them."""
    cloaked = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return cloaked.value != 0


def _windows() -> list[dict]:
    """Visible top-level app windows in Z-order (front-most first)."""
    found = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title or title in IGNORED_TITLES:
            return True
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return True
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER) or _is_cloaked(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid).name()
        except psutil.Error:
            proc = ""
        found.append({"hwnd": hwnd, "title": title, "process": proc, "pid": pid})
        return True

    win32gui.EnumWindows(callback, None)
    return found


def _is_digidaw(w: dict) -> bool:
    return w["pid"] == os.getpid() or any(m in w["title"] for m in DIGIDAW_MARKERS)


def _find(name: str) -> dict | None:
    import system_control
    needle = name.lower().strip()
    wins = [w for w in _windows() if not _is_digidaw(w)]
    key = system_control._match_key(needle, system_control.PROCESS_ALIASES)
    proc_target = system_control.PROCESS_ALIASES[key] if key else needle.replace(" ", "") + ".exe"
    for w in wins:
        if w["process"].lower() == proc_target:
            return w
    for w in wins:
        if needle in w["title"].lower():
            return w
    stem = proc_target.removesuffix(".exe")
    for w in wins:
        if len(stem) >= 3 and stem in w["process"].lower():
            return w
    return None


def _target(name: str) -> dict | None:
    if name and name.strip():
        return _find(name)
    return next((w for w in _windows() if not _is_digidaw(w)), None)


def _force_foreground(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    fg = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    me = win32api.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and ctypes.windll.user32.AttachThreadInput(fg_thread, me, True))
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Windows blocks focus stealing; a synthetic Alt tap lifts the restriction
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        if attached:
            ctypes.windll.user32.AttachThreadInput(fg_thread, me, False)


def foreground_id():
    return win32gui.GetForegroundWindow()


def minimize(hwnd):
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)


def close_windows_of(pids: set[int]) -> list[str]:
    """Send a normal close request (like clicking X) to every window owned by these processes."""
    closed = []
    for w in _windows():
        if w["pid"] in pids and not _is_digidaw(w):
            win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            closed.append(w["title"])
    return closed


def focus_window(name: str) -> str:
    """Switch to / bring to front an open window, matched by app or title
    (e.g. 'chrome', 'word', 'whatsapp', 'spotify', 'notepad', 'youtube')."""
    w = _find(name)
    if not w:
        return f"Tidak ada jendela '{name}' yang terbuka, {T}."
    _force_foreground(w["hwnd"])
    return f"Beralih ke {w['title'][:60]}, {T}."


def window_action(action: str, name: str = "") -> str:
    """Manage a window. action: 'minimize', 'maximize', 'restore', 'close', 'snap_left',
    'snap_right', 'minimize_all' (show desktop). name: app or window title; empty = active window."""
    action = action.lower().strip()
    if action in ("minimize_all", "show_desktop", "desktop"):
        import pyautogui
        pyautogui.hotkey("win", "d")
        return f"Semua jendela diminimalkan, {T}."

    w = _target(name)
    if not w:
        return f"Jendela '{name or 'aktif'}' tidak ditemukan, {T}."
    hwnd, title = w["hwnd"], w["title"][:60]
    if action == "minimize":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return f"{title} diminimalkan, {T}."
    if action == "maximize":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        _force_foreground(hwnd)
        return f"{title} dimaksimalkan, {T}."
    if action == "restore":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        _force_foreground(hwnd)
        return f"{title} dikembalikan, {T}."
    if action == "close":
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return f"Menutup {title}, {T}."
    if action in ("snap_left", "snap_right"):
        import pyautogui
        _force_foreground(hwnd)
        time.sleep(0.25)
        pyautogui.hotkey("win", "left" if action == "snap_left" else "right")
        time.sleep(0.2)
        pyautogui.press("esc")   # dismiss Snap Assist suggestions
        return f"{title} dipindah ke {'kiri' if action == 'snap_left' else 'kanan'} layar, {T}."
    return f"Aksi jendela '{action}' tidak dikenal, {T}."


def list_windows() -> str:
    """List the currently open windows."""
    wins = [w for w in _windows() if not _is_digidaw(w)]
    if not wins:
        return f"Tidak ada jendela aplikasi yang terbuka, {T}."
    return "Jendela terbuka: " + "; ".join(f"{w['title'][:50]} ({w['process'].removesuffix('.exe')})"
                                            for w in wins[:12])


AI_TOOLS = [focus_window, window_action, list_windows]
