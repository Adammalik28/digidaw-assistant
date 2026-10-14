"""Window management skills on Linux (Hyprland): switch to, minimize, maximize, snap and close windows.

Same functions and window dicts as window_manager_win.py; "hwnd" holds the Hyprland window address.
"""
import os
import re
import time

import config
import linux_desktop as ld

T = config.USER_TITLE
DIGIDAW_MARKERS = ("DIGIDAW",)   # console + HUD windows are skipped for "active window" actions
IGNORED_TITLES = {"", "Desktop"}


def _windows() -> list[dict]:
    """App windows, most recently used first."""
    return [w for w in ld.windows() if w["title"] not in IGNORED_TITLES]


def _is_digidaw(w: dict) -> bool:
    return w["pid"] == os.getpid() or any(m in w["title"] for m in DIGIDAW_MARKERS)


def _names(w: dict) -> tuple[str, str]:
    return w["process"].lower(), w.get("class", "").lower()


def _find(name: str) -> dict | None:
    import system_control
    needle = name.lower().strip()
    # Digidaw's own windows are skipped, unless the user names them ("pindahkan digidaw ke workspace 3")
    wins = [w for w in _windows() if not _is_digidaw(w) or re.search(r"digidaw|\bhud\b", needle)]
    if re.search(r"digidaw|\bhud\b", needle):
        own = [w for w in wins if _is_digidaw(w)]
        if own:
            return own[0]
    key = system_control._match_key(needle, system_control.PROCESS_ALIASES)
    target = system_control.PROCESS_ALIASES[key] if key else needle.replace(" ", "")
    for w in wins:
        if target in _names(w):
            return w
    for w in wins:
        if needle in w["title"].lower():
            return w
    for w in wins:
        if len(target) >= 3 and any(target in n for n in _names(w)):
            return w
    return None


def _target(name: str) -> dict | None:
    if name and name.strip():
        return _find(name)
    return next((w for w in _windows() if not _is_digidaw(w)), None)


def _force_foreground(hwnd):
    ld.focus(hwnd)


def foreground_id():
    return ld.active_address()


def minimize(hwnd):
    ld.minimize(hwnd)


def close_windows_of(pids: set[int]) -> list[str]:
    """Send a normal close request (like clicking X) to every window owned by these processes."""
    closed = []
    for w in _windows():
        if w["pid"] in pids and not _is_digidaw(w):
            ld.close(w["hwnd"])
            closed.append(w["title"])
    return closed


def focus_window(name: str) -> str:
    """Switch to / bring to front an open window, matched by app or title
    (e.g. 'chrome', 'firefox', 'code', 'discord', 'spotify', 'youtube')."""
    w = _find(name)
    if not w:
        import screen_context            # "pindah ke YouTube" is often a browser tab, not a window
        if screen_context._browser_windows():
            result = screen_context.switch_tab(name)
            if not result.startswith("Tidak ada"):
                return result
        return f"Tidak ada jendela '{name}' yang terbuka, {T}."
    _force_foreground(w["hwnd"])
    return f"Beralih ke {w['title'][:60]}, {T}."


def window_action(action: str, name: str = "") -> str:
    """Manage a window. action: 'minimize', 'maximize', 'restore', 'close', 'snap_left',
    'snap_right', 'minimize_all' (show desktop). name: app or window title; empty = active window."""
    action = action.lower().strip()
    if action in ("minimize_all", "show_desktop", "desktop"):
        ld.show_desktop()
        return f"Pindah ke workspace kosong, {T}."

    w = _target(name)
    if not w:
        return f"Jendela '{name or 'aktif'}' tidak ditemukan, {T}."
    hwnd, title = w["hwnd"], w["title"][:60]
    if action == "minimize":
        ld.minimize(hwnd)
        return f"{title} disembunyikan, {T}. Panggil namanya untuk memunculkannya lagi."
    if action == "maximize":
        ld.maximize(hwnd)
        return f"{title} dimaksimalkan, {T}."
    if action == "restore":
        ld.restore(hwnd)
        return f"{title} dikembalikan, {T}."
    if action == "close":
        ld.close(hwnd)
        for _ in range(25):                      # gone within 2.5 s?
            time.sleep(0.1)
            if not ld.window_exists(hwnd):
                return f"{title} sudah ditutup, {T}."
        # still there: the app is asking something ("save changes?") -> close it without asking
        import psutil
        import system_control
        try:
            proc = psutil.Process(w["pid"])
        except psutil.Error:
            return f"{title} sudah ditutup, {T}."
        if system_control._protected(proc.name()):
            return f"{title} tidak mau tertutup, {T}."
        alive, _ = system_control._quit_processes([proc])
        return f"{title} {'tidak mau tertutup' if alive else 'sudah ditutup paksa tanpa konfirmasi'}, {T}."
    if action in ("snap_left", "snap_right"):
        ld.snap(hwnd, "left" if action == "snap_left" else "right")
        return f"{title} dipindah ke {'kiri' if action == 'snap_left' else 'kanan'} layar, {T}."
    return f"Aksi jendela '{action}' tidak dikenal, {T}."


def list_windows() -> str:
    """List the currently open windows."""
    wins = [w for w in _windows() if not _is_digidaw(w)]
    if not wins:
        return f"Tidak ada jendela aplikasi yang terbuka, {T}."
    return "Jendela terbuka: " + "; ".join(f"{w['title'][:50]} ({w['process'] or w['class']})" for w in wins[:12])


def _app_label(w: dict) -> str:
    """'Firefox' rather than the page title ('Restore Session — Mozilla Firefox')."""
    if _is_digidaw(w):
        return "Digidaw"
    name = (w.get("class") or w["process"] or w["title"]).split(".")[-1]
    return name[:1].upper() + name[1:]


def closable_windows() -> list[dict]:
    """App windows 'tutup semua aplikasi' closes: not Digidaw's HUD/console, not its music player."""
    return [w for w in _windows() if not _is_digidaw(w) and w.get("class") != "DigidawPlayer"
            and not w["workspace"].startswith("special:")]


def close_all_windows() -> str:
    wins = closable_windows()
    for w in wins:
        ld.close(w["hwnd"])
    time.sleep(2.5)
    left = [w for w in closable_windows() if w["hwnd"] in {x["hwnd"] for x in wins}]
    if left:
        return (f"{len(wins) - len(left)} jendela ditutup, {T}. Masih terbuka: "
                f"{', '.join(sorted({_app_label(w) for w in left}))} (mungkin menunggu konfirmasi simpan).")
    return f"Semua aplikasi sudah ditutup, {T}."


def _workspace_arg(workspace) -> str | None:
    w = str(workspace).strip().lower()
    if w in ("next", "berikutnya", "selanjutnya", "+1"):
        return "+1"
    if w in ("previous", "prev", "sebelumnya", "-1"):
        return "-1"
    return w if w.isdigit() and int(w) > 0 else None


def switch_workspace(workspace: str) -> str:
    """Switch to another workspace (virtual desktop). workspace: its number ('4'), or 'next' / 'previous'."""
    target = _workspace_arg(workspace)
    if not target:
        return f"Workspace '{workspace}' tidak dikenal, {T}."
    if target.isdigit() and ld.active_workspace_id() == int(target):
        return f"Anda sudah di workspace {target}, {T}."
    if not ld.switch_workspace(target):
        return f"Gagal pindah workspace, {T}."
    return f"Pindah ke workspace {ld.active_workspace_id() or target}, {T}."


def move_to_workspace(workspace: str, name: str = "", follow: bool = False) -> str:
    """Move an app window to another workspace. workspace: its number ('3'), or 'next' / 'previous'.
    name: app or window title (empty = the active window). follow: also switch to that workspace."""
    target = _workspace_arg(workspace)
    if not target:
        return f"Workspace '{workspace}' tidak dikenal, {T}."
    w = _target(name)
    if not w:
        return f"Jendela '{name or 'aktif'}' tidak ditemukan, {T}."
    if target.isdigit() and w["workspace"] == target:
        return f"{w['title'][:60]} sudah ada di workspace {target}, {T}."
    label = _app_label(w)
    if not ld.move_to_workspace(w["hwnd"], target, follow):
        return f"Gagal memindahkan {label}, {T}."
    return f"{label} dipindah ke workspace {target}, {T}."


AI_TOOLS = [focus_window, window_action, list_windows, switch_workspace, move_to_workspace]
