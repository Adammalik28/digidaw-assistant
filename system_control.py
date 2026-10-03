"""
DIGIDAW system skills.

Every public function here is a "skill". Functions listed in AI_TOOLS (bottom of file)
are exposed to Gemini through function calling, so their docstrings and type hints
act as the tool description — keep them accurate.
"""
import os
import re
import json
import time
import ctypes
import shutil
import socket
import difflib
import datetime
import threading
import subprocess
import webbrowser
import urllib.parse
from pathlib import Path

import psutil
import requests

import config
import memory
import oscompat
import safety

IS_WINDOWS = oscompat.IS_WINDOWS
if IS_WINDOWS:
    from ctypes import wintypes
else:
    import shlex
    import linux_desktop as ld

T = config.USER_TITLE
NO_WINDOW = oscompat.NO_WINDOW

# Virtual key codes for multimedia
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]


# ───────────────────────────── helpers ─────────────────────────────

def _press_key(hex_key_code):
    """Simulate a multimedia key press (Windows user32; on Linux the same action through PipeWire/MPRIS)."""
    if IS_WINDOWS:
        ctypes.windll.user32.keybd_event(hex_key_code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(hex_key_code, 0, 2, 0)
        return
    linux = {VK_VOLUME_UP: ["wpctl", "set-volume", "-l", "1.0", ld.SINK, "2%+"],
             VK_VOLUME_DOWN: ["wpctl", "set-volume", ld.SINK, "2%-"],
             VK_VOLUME_MUTE: ["wpctl", "set-mute", ld.SINK, "toggle"],
             VK_MEDIA_NEXT_TRACK: ["playerctl", "next"], VK_MEDIA_PREV_TRACK: ["playerctl", "previous"],
             VK_MEDIA_PLAY_PAUSE: ["playerctl", "play-pause"]}.get(hex_key_code)
    if linux:
        ld.run(linux)


def _ps(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet and capture UTF-8 output."""
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW,
    )


def _shell(command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command in the system shell: PowerShell on Windows, bash on Linux."""
    if IS_WINDOWS:
        return _ps(command, timeout)
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


SHELL_NAME = "PowerShell" if IS_WINDOWS else "bash"
ADMIN_PROMPT = "muncul jendela izin Windows" if IS_WINDOWS else "muncul jendela password administrator"
ACCESS_DENIED_RE = re.compile(r"access is denied|akses ditolak|permissiondenied|unauthorizedaccess|"
                              r"requires elevation|run as administrator|administrator privileges|"
                              r"permission denied|operation not permitted|must be (?:run as )?root|are you root|"
                              r"requires root|izin ditolak", re.IGNORECASE)


def run_elevated(script: str, timeout: int = 180) -> tuple[bool, str]:
    """Run a script as administrator. Windows shows its UAC prompt, Linux the polkit password prompt
    (pkexec); returns (ok, output). ok is False when the user declines the prompt."""
    if not IS_WINDOWS:
        if not shutil.which("pkexec"):
            return False, "pkexec tidak tersedia"
        try:
            r = subprocess.run(["pkexec", "bash", "-c", script], capture_output=True, text=True, errors="replace",
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "melebihi batas waktu"
        if r.returncode in (126, 127):          # dismissed / not authorized
            return False, "izin administrator tidak diberikan"
        return True, (r.stdout + r.stderr).strip()
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="digidaw-admin-"))
    body, out = work / "task.ps1", work / "out.txt"
    body.write_text("$ErrorActionPreference='Continue'\n& {\n" + script + f"\n}} *>&1 | Out-File -Encoding utf8 '{out}'\n",
                    encoding="utf-8-sig")
    launcher = (f"try {{ Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                f"'-NoProfile','-ExecutionPolicy','Bypass','-File','\"{body}\"' -ErrorAction Stop; exit 0 }} "
                f"catch {{ exit 5 }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", launcher], capture_output=True, text=True,
                           timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "melebihi batas waktu"
    output = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else ""
    shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        return False, "izin administrator tidak diberikan"
    return True, output


def _known_folder(name: str) -> Path:
    """Resolve a known folder (Windows: handles OneDrive redirection; Linux: XDG user dirs)."""
    if not IS_WINDOWS:
        return ld.known_folder(name)
    try:
        from win32com.shell import shell, shellcon
        fid = {
            "documents": shellcon.FOLDERID_Documents,
            "downloads": shellcon.FOLDERID_Downloads,
            "desktop": shellcon.FOLDERID_Desktop,
            "pictures": shellcon.FOLDERID_Pictures,
            "music": shellcon.FOLDERID_Music,
            "videos": shellcon.FOLDERID_Videos,
        }[name]
        return Path(shell.SHGetKnownFolderPath(fid))
    except Exception:
        return Path.home() / name.capitalize()


def _fmt_size(num_bytes: float) -> str:
    for unit in ["byte", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "byte" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ── bring what we launch to the front ──
# Windows' focus-stealing protection keeps windows opened by a background program (Digidaw) behind the
# current one: Notepad opened, but you don't see it. After launching, wait for the new window and
# bring it forward the same way focus_window does.

def _hwnds() -> set[int]:
    import window_manager
    return {w["hwnd"] for w in window_manager._windows()}


def _default_browser_process() -> str:
    if not IS_WINDOWS:
        return {"edge": "msedge"}.get(ld.default_browser(), ld.default_browser())
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\https\UserChoice") as key:
            prog = winreg.QueryValueEx(key, "ProgId")[0].lower()
    except OSError:
        return ""
    for marker, proc in (("firefox", "firefox"), ("chrome", "chrome"), ("msedge", "msedge"), ("brave", "brave"),
                         ("opera", "opera")):
        if marker in prog:
            return proc
    return ""


def bring_forward(before: set[int], hint: str = "", timeout: float = 8.0, allow_existing: bool = True):
    """In the background: wait for a new window (preferring one whose process / title matches hint) and
    bring it to the front. With allow_existing, an already open window of that app is used when no new
    window appears (single-window apps, a new browser tab)."""
    hint = (hint or "").lower().removesuffix(".exe").strip(":/ ")

    def worker():
        import window_manager
        start = time.time()
        while time.time() - start < timeout:
            wins = [w for w in window_manager._windows() if not window_manager._is_digidaw(w)]
            new = [w for w in wins if w["hwnd"] not in before]
            matching = [w for w in new if hint and (hint in w["process"].lower() or hint in w["title"].lower()
                                                    or hint in w.get("class", "").lower())]
            target = matching[0] if matching else (new[0] if new and not hint else None)
            if not target and new and time.time() - start > 2.5:
                target = new[0]                 # nothing matched the hint, but something new did open
            if not target and allow_existing and hint and time.time() - start > 1.2:
                target = next((w for w in wins if hint in w["process"].lower()
                               or hint in w.get("class", "").lower()), None)
            if target:
                time.sleep(0.15)                # let the window finish appearing
                for _ in range(3):
                    window_manager._force_foreground(target["hwnd"])
                    time.sleep(0.2)
                    if window_manager.foreground_id() == target["hwnd"]:
                        return
                return
            time.sleep(0.15)

    threading.Thread(target=worker, daemon=True, name="bring-forward").start()


def launch_on_default_desktop(cmd_or_url: str):
    """Launch a URL, file, program or shell: URI on the interactive desktop."""
    if cmd_or_url.startswith(("http://", "https://")):
        webbrowser.open(cmd_or_url)
        return True
    if not IS_WINDOWS:
        return _linux_launch(cmd_or_url)
    try:
        os.startfile(cmd_or_url)
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(f'cmd.exe /c start "" {cmd_or_url}', shell=True, creationflags=NO_WINDOW)
        return True
    except Exception as e:
        print(f"[Launch Error] {e}")
        return False


# ───────────────────────── apps & websites ─────────────────────────

WEBSITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "github": "https://www.github.com",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "tiktok": "https://www.tiktok.com",
    "netflix": "https://www.netflix.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "whatsapp web": "https://web.whatsapp.com",
    "shopee": "https://shopee.co.id",
    "tokopedia": "https://www.tokopedia.com",
    "wikipedia": "https://id.wikipedia.org",
    "translate": "https://translate.google.com",
    "google translate": "https://translate.google.com",
}

APPS = {
    "spotify": "spotify:",
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "catatan": "notepad",
    "kalkulator": "calc",
    "calculator": "calc",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "explorer": "explorer",
    "file explorer": "explorer",
    "file manager": "explorer",
    "task manager": "taskmgr",
    "terminal": "wt",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "settings": "ms-settings:",
    "pengaturan": "ms-settings:",
    "control panel": "control",
    "panel kontrol": "control",
    "paint": "mspaint",
    "word": "winword",
    "microsoft word": "winword",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpnt",
    "kamera": "microsoft.windows.camera:",
    "camera": "microsoft.windows.camera:",
    "jam": "ms-clock:",
    "alarm": "ms-clock:",
    "clock": "ms-clock:",
    "microsoft store": "ms-windows-store:",
    "store": "ms-windows-store:",
    "snipping tool": "ms-screenclip:",
    "whatsapp": "whatsapp:",
    "recycle bin": "shell:RecycleBinFolder",
    "tempat sampah": "shell:RecycleBinFolder",
}

# friendly name -> process image name, for close_application
PROCESS_ALIASES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe",
    "microsoft edge": "msedge.exe", "firefox": "firefox.exe", "notepad": "notepad.exe",
    "catatan": "notepad.exe", "kalkulator": "calculatorapp.exe", "calculator": "calculatorapp.exe",
    "vs code": "code.exe", "vscode": "code.exe", "visual studio code": "code.exe",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "spotify": "spotify.exe", "paint": "mspaint.exe", "discord": "discord.exe",
    "telegram": "telegram.exe", "whatsapp": "whatsapp.exe", "task manager": "taskmgr.exe",
    "terminal": "windowsterminal.exe", "cmd": "cmd.exe", "steam": "steam.exe",
    "zoom": "zoom.exe", "obs": "obs64.exe", "vlc": "vlc.exe",
}

PROTECTED_PROCESSES = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "smss.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe",
    "python.exe", "pythonw.exe", "registry", "memcompression",
}

if not IS_WINDOWS:
    # Linux: each app is a list of candidates, the first one installed is used.
    # "TERM:cmd" runs a console program in a terminal window; URIs / URLs are opened with xdg-open.
    _EDITORS = list(ld.TEXT_EDITORS)
    _FILE_MANAGERS = ["thunar", "nautilus", "dolphin", "nemo", "pcmanfm-qt", "pcmanfm", "caja"]
    _TERMINAL = ld.terminal() or "xterm"
    APPS = {
        "spotify": ["spotify", "spotify-launcher"],
        "chrome": ["google-chrome-stable", "google-chrome", "chromium"],
        "google chrome": ["google-chrome-stable", "google-chrome", "chromium"],
        "chromium": ["chromium"],
        "firefox": ["firefox"],
        "edge": ["microsoft-edge-stable", "microsoft-edge"],
        "microsoft edge": ["microsoft-edge-stable", "microsoft-edge"],
        "brave": ["brave", "brave-browser"],
        "notepad": _EDITORS, "catatan": _EDITORS, "text editor": _EDITORS, "editor teks": _EDITORS,
        "kalkulator": ["qalculate-gtk", "gnome-calculator", "kcalc", "galculator", "mate-calc"],
        "calculator": ["qalculate-gtk", "gnome-calculator", "kcalc", "galculator", "mate-calc"],
        "vs code": ["code", "code-oss", "codium"], "vscode": ["code", "code-oss", "codium"],
        "visual studio code": ["code", "code-oss", "codium"],
        "explorer": _FILE_MANAGERS, "file explorer": _FILE_MANAGERS, "file manager": _FILE_MANAGERS,
        "task manager": ["missioncenter", "gnome-system-monitor", "plasma-systemmonitor", "TERM:btop", "TERM:htop"],
        "system monitor": ["missioncenter", "gnome-system-monitor", "plasma-systemmonitor", "TERM:btop", "TERM:htop"],
        "terminal": [_TERMINAL], "cmd": [_TERMINAL], "command prompt": [_TERMINAL], "powershell": [_TERMINAL],
        "settings": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "nwg-look"],
        "pengaturan": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "nwg-look"],
        "control panel": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "nwg-look"],
        "panel kontrol": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "nwg-look"],
        "pengaturan suara": ["pavucontrol", "pwvucontrol"], "sound settings": ["pavucontrol", "pwvucontrol"],
        "paint": ["pinta", "kolourpaint", "drawing", "gimp"],
        "word": ["libreoffice --writer", "onlyoffice-desktopeditors", "wps"],
        "microsoft word": ["libreoffice --writer", "onlyoffice-desktopeditors", "wps"],
        "excel": ["libreoffice --calc", "onlyoffice-desktopeditors", "et"],
        "microsoft excel": ["libreoffice --calc", "onlyoffice-desktopeditors", "et"],
        "powerpoint": ["libreoffice --impress", "onlyoffice-desktopeditors", "wpp"],
        "kamera": ["snapshot", "cheese", "kamoso", "guvcview"], "camera": ["snapshot", "cheese", "kamoso", "guvcview"],
        "jam": ["gnome-clocks"], "clock": ["gnome-clocks"],
        "microsoft store": ["gnome-software", "plasma-discover", "pamac-manager", "octopi"],
        "store": ["gnome-software", "plasma-discover", "pamac-manager", "octopi"],
        "snipping tool": ["flameshot gui", "spectacle", "satty", "swappy"],
        "whatsapp": ["zapzap", "whatsapp-for-linux", "whatsie", "https://web.whatsapp.com"],
        "discord": ["discord", "vesktop", "webcord"], "telegram": ["telegram-desktop", "Telegram"],
        "steam": ["steam"], "obs": ["obs"], "vlc": ["vlc"], "zoom": ["zoom"], "gimp": ["gimp"],
        "recycle bin": ["trash:///"], "tempat sampah": ["trash:///"], "trash": ["trash:///"],
    }
    _editor_proc = os.path.basename(ld.text_editor() or "mousepad")
    _fm_proc = ld.file_manager() or "thunar"
    PROCESS_ALIASES = {
        "chrome": "chrome", "google chrome": "chrome", "chromium": "chromium", "edge": "msedge",
        "microsoft edge": "msedge", "firefox": "firefox", "brave": "brave", "notepad": _editor_proc,
        "catatan": _editor_proc, "text editor": _editor_proc, "editor teks": _editor_proc,
        "kalkulator": "qalculate-gtk", "calculator": "qalculate-gtk",
        "vs code": "code", "vscode": "code", "visual studio code": "code",
        "word": "soffice.bin", "excel": "soffice.bin", "powerpoint": "soffice.bin", "libreoffice": "soffice.bin",
        "spotify": "spotify", "paint": "pinta", "discord": "discord", "telegram": "telegram-desktop",
        "whatsapp": "zapzap", "task manager": "missioncenter", "terminal": os.path.basename(_TERMINAL),
        "steam": "steam", "zoom": "zoom", "obs": "obs", "vlc": "vlc", "gimp": "gimp",
        "file manager": _fm_proc, "explorer": _fm_proc, "file explorer": _fm_proc,
    }
    PROTECTED_PROCESSES = {
        "systemd", "init", "kthreadd", "hyprland", "xwayland", "pipewire", "pipewire-pulse", "wireplumber",
        "dbus-daemon", "dbus-broker", "dbus-broker-launch", "sddm", "gdm", "login", "agetty", "uwsm",
        "polkitd", "networkmanager", "systemd-logind", "xdg-desktop-portal", "xdg-desktop-portal-hyprland",
        "python", "python3", "python3.12", "sshd",
    }


def _protected(process_name: str) -> bool:
    name = process_name.lower()
    return name in PROTECTED_PROCESSES or (IS_WINDOWS and name + ".exe" in PROTECTED_PROCESSES)


def _linux_launch(target: str, cwd: str | None = None) -> str | None:
    """Start one Linux app candidate; returns the program that was started (None if not installed)."""
    if target.startswith(("http://", "https://")):
        webbrowser.open(target)
        return "browser"
    if re.match(r"^[a-z][\w+.-]*:", target) and " " not in target:      # trash:///, spotify:search:..., mailto:
        return target if (ld.open_in_file_manager(target) if target.startswith("trash:") else
                          ld.spawn(["xdg-open", target])) else None
    if target.startswith("TERM:"):
        program = target[5:]
        argv = ld.terminal_argv(cwd or str(Path.home()), program, title=program) if shutil.which(program) else None
        return program if argv and ld.spawn(argv) else None
    if os.path.exists(target):
        return target if ld.spawn(["xdg-open", target]) else None
    argv = shlex.split(target)
    if not argv or not shutil.which(argv[0]):
        return None
    return os.path.basename(argv[0]) if ld.spawn(argv, cwd=cwd) else None


_start_apps_cache: list[dict] | None = None


def _match_key(text: str, mapping: dict) -> str | None:
    """Find the longest mapping key that appears as a whole word/phrase in text."""
    text = text.lower().strip()
    for key in sorted(mapping, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            return key
    return None


def _start_apps() -> list[dict]:
    """All apps visible in the Start Menu (desktop + Store apps) / the Linux application menu, cached."""
    global _start_apps_cache
    if _start_apps_cache is None and not IS_WINDOWS:
        try:
            _start_apps_cache = ld.desktop_apps()
        except Exception:
            _start_apps_cache = []
    if _start_apps_cache is None:
        try:
            r = _ps("Get-StartApps | ConvertTo-Json -Compress", timeout=20)
            data = json.loads(r.stdout or "[]")
            _start_apps_cache = [
                a for a in (data if isinstance(data, list) else [data])
                if a.get("Name") and not a["Name"].startswith("==")
            ]
        except Exception:
            _start_apps_cache = []
    return _start_apps_cache


GENERIC_APP_WORDS = {"aplikasi", "aplikasinya", "app", "apps", "program", "software", "aplikasi itu", "itu", "ini"}


def _prefer_used(matches: list[dict]) -> dict:
    """Several apps fit: the one the user opens most (habits), else the first."""
    import habits
    return max(matches, key=lambda a: habits.usage_count("app", a["Name"]))


def _find_start_app(name: str) -> dict | None:
    """Exact name > name starting with it > a word starting with it > a close spelling. Short or generic names
    ('ho', 'aplikasi') only match exactly, so a misheard fragment never opens some random app."""
    name = name.lower().strip()
    if not name or name in GENERIC_APP_WORDS:
        return None
    apps = _start_apps()
    lowered = [(a["Name"].lower(), a) for a in apps
               if "uninstall" not in a["Name"].lower() or "uninstall" in name]
    lowered += [(a["NameID"].lower(), a) for a in apps if a.get("NameID")]      # Linux: Indonesian app names
    exact = [a for n, a in lowered if n == name]
    if exact:
        return exact[0]
    if len(name) < 3:
        return None
    for tier in ([a for n, a in lowered if n.startswith(name)],
                 [a for n, a in lowered if re.search(rf"\b{re.escape(name)}", n)]):
        if tier:
            return _prefer_used(tier)
    if len(name) >= 4:
        close = difflib.get_close_matches(name, [n for n, _ in lowered], n=1, cutoff=0.75)
        if close:
            return next(a for n, a in lowered if n == close[0])
    return None


def _suggestions(name: str) -> str:
    """' Maksud Anda X atau Y?' for a name that wasn't found (empty when nothing is similar)."""
    names = sorted({a["Name"] for a in _start_apps()})
    close = difflib.get_close_matches(name.lower().strip(), [n.lower() for n in names], n=2, cutoff=0.55)
    if not close:
        return ""
    pretty = [next(n for n in names if n.lower() == c) for c in close]
    return f" Maksud Anda {' atau '.join(pretty)}?"


def open_application(name: str) -> str:
    """Open any installed application on the PC by name (e.g. 'notepad', 'chrome', 'spotify',
    'word', 'steam', 'kalkulator', a game name). Searches the Start Menu if not a common app."""
    if not IS_WINDOWS:
        return _open_application_linux(name)
    key = _match_key(name, APPS)
    if key:
        before = _hwnds()
        ok = launch_on_default_desktop(APPS[key])
        if ok:
            proc_key = _match_key(key, PROCESS_ALIASES)
            bring_forward(before, PROCESS_ALIASES[proc_key] if proc_key else APPS[key])
        return f"Membuka {key}, {T}." if ok else f"Gagal membuka {key}, {T}."

    app = _find_start_app(name)
    if app:
        before = _hwnds()
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"], creationflags=NO_WINDOW)
        target = app["AppID"].split("\\")[-1].split("!")[0]
        bring_forward(before, target if target.lower().endswith(".exe") else app["Name"].split()[0])
        return f"Membuka {app['Name']}, {T}."

    exe = name.strip()
    if shutil.which(exe):
        before = _hwnds()
        launch_on_default_desktop(exe)
        bring_forward(before, exe)
        return f"Menjalankan {exe}, {T}."
    return f"Aplikasi '{name}' tidak ditemukan di komputer ini, {T}."


def _open_application_linux(name: str) -> str:
    key = _match_key(name, APPS)
    if key:
        before = _hwnds()
        for candidate in APPS[key]:
            started = _linux_launch(candidate)
            if started:
                proc_key = _match_key(key, PROCESS_ALIASES)
                bring_forward(before, PROCESS_ALIASES[proc_key] if proc_key else started)
                return f"Membuka {key}, {T}."
        if not _find_start_app(name):
            return f"Aplikasi {key} belum terpasang di komputer ini, {T}."

    app = _find_start_app(name)
    if app:
        before = _hwnds()
        if not ld.launch_app(app):
            return f"Gagal membuka {app['Name']}, {T}."
        program = os.path.basename((ld._exec_argv(app["Exec"]) or [""])[0])
        bring_forward(before, program if program not in ("env", "flatpak", "gtk-launch") else app["Name"].split()[0])
        return f"Membuka {app['Name']}, {T}."

    exe = name.strip()
    if shutil.which(exe):
        before = _hwnds()
        _linux_launch(exe)
        bring_forward(before, exe)
        return f"Menjalankan {exe}, {T}."
    if _match_key(name, WEBSITES):
        return open_website(name)      # "buka tiktok" with no desktop app -> the website
    return f"Aplikasi '{name}' tidak ditemukan di komputer ini, {T}.{_suggestions(name)}"


BROWSER_EXES = {"chrome": "chrome", "google chrome": "chrome", "firefox": "firefox", "mozilla": "firefox",
                "edge": "msedge", "microsoft edge": "msedge", "brave": "brave", "opera": "opera"}
_LINUX_BROWSER_FAMILY = {"chrome": "chrome", "firefox": "firefox", "msedge": "edge", "brave": "brave", "opera": "opera"}


def _open_url(url: str, browser: str = ""):
    exe = BROWSER_EXES.get(browser.lower().strip()) if browser else None
    bring_forward(_hwnds(), exe or _default_browser_process())
    if exe and not IS_WINDOWS:
        command = ld.browser_command(_LINUX_BROWSER_FAMILY[exe])
        if command and ld.spawn([command, url]):
            return
        exe = None
    if exe:
        # "start" resolves the browser through Windows' App Paths registry
        r = subprocess.run(["cmd", "/c", "start", "", exe, url], capture_output=True, creationflags=NO_WINDOW)
        if r.returncode == 0:
            return
    webbrowser.open(url)


def open_website(site: str, browser: str = "") -> str:
    """Open a website in the browser. Accepts a known name (youtube, gmail, instagram...),
    a domain/URL (e.g. 'detik.com'), or otherwise performs a Google search. browser = 'chrome',
    'firefox', 'edge'... when the user names one (empty = default browser)."""
    s = site.strip()
    where = f" di {browser.title()}" if browser else ""
    key = _match_key(s, WEBSITES)
    if key:
        _open_url(WEBSITES[key], browser)
        return f"Membuka {key}{where}, {T}."
    if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", s):
        url = s if s.startswith("http") else "https://" + s
        _open_url(url, browser)
        return f"Membuka {s}{where}, {T}."
    return google_search(s, browser)


def open_app_or_web(target: str) -> str:
    """Local-router helper: try app, then website, then web search."""
    if _match_key(target, WEBSITES):
        return open_website(target)
    result = open_application(target)
    if "tidak ditemukan" in result:
        return open_website(target)
    return result


BROWSER_PROCESSES = ("firefox", "chrome", "chromium", "brave", "msedge", "opera", "vivaldi")


def close_many(names: list[str]) -> str:
    """Close several running apps at once, e.g. ['firefox', 'mousepad'] ('tutup firefox dan mousepad').
    'browser' closes whichever web browser is running."""
    results = []
    for name in (n.strip() for n in names if n.strip()):
        if re.fullmatch(r"(?:web )?browser|peramban", name.lower()):
            running = [b for b in BROWSER_PROCESSES if _matching_processes(b)]
            if not running:
                results.append(f"Tidak ada browser yang sedang berjalan, {T}.")
            for b in running:
                results.append(close_application(b))
            continue
        results.append(close_application(name))
    return " ".join(results) or f"Tidak ada yang ditutup, {T}."


def close_all_apps() -> str:
    """Close every open app window ('tutup semua aplikasi'); Digidaw itself and its music player stay.
    Asks for confirmation first."""
    import window_manager
    wins = window_manager.closable_windows()
    if not wins:
        return f"Tidak ada aplikasi yang terbuka, {T}."
    apps = sorted({w.get("class") or w["process"] for w in wins})
    return safety.guard(f"menutup semua aplikasi ({len(wins)} jendela: {', '.join(apps[:5])})",
                        window_manager.close_all_windows)


def open_many(names: list[str]) -> str:
    """Open several apps and/or websites at once, e.g. ['terminal', 'mousepad'] or ['firefox', 'youtube']."""
    results = []
    for i, name in enumerate(n.strip() for n in names if n.strip()):
        if i:
            time.sleep(0.7)      # let the previous window map first so focus lands on the right one
        results.append(open_app_or_web(name))
    return " ".join(results) or f"Tidak ada yang dibuka, {T}."


# Digidaw's own media players (separate browser profiles): "tutup firefox" means the user's browser, not these
PLAYER_MARKERS = ("browser_profile_firefox", "browser_profile_linux", "browser_profile")


def _closable(p: psutil.Process) -> bool:
    """Only the user's own programs: never kernel threads (no command line), other users' or system processes,
    and never Digidaw's media player."""
    try:
        cmdline = p.cmdline()
        if not cmdline:
            return False
        if not IS_WINDOWS and p.uids().real != os.getuid():
            return False
        for proc in [p] + p.parents():           # the player's tab processes don't carry the profile path
            args = " ".join(proc.cmdline())
            if any(marker in args for marker in PLAYER_MARKERS):
                return False
        return True
    except psutil.Error:
        return False


def _matching_processes(name: str) -> list[psutil.Process]:
    key = _match_key(name, PROCESS_ALIASES)
    target = PROCESS_ALIASES[key] if key else name.lower().replace(" ", "")
    if IS_WINDOWS:
        target = target if target.endswith(".exe") else target + ".exe"
    stem = target.removesuffix(".exe")
    procs = []
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info["name"] or "").lower()
        if _protected(pname) or p.info["pid"] == os.getpid():
            continue
        base = pname.removesuffix(".exe")
        # exact name, or the name starts with it ("fire" -> firefox); a fragment inside a word ("gra" in
        # "migration") never matches
        if pname == target or (len(stem) >= 3 and base.startswith(stem)) or \
                (len(stem) >= 5 and re.search(rf"(?:^|[-_.]){re.escape(stem)}", base)):
            if _closable(p):
                procs.append(p)
    return procs


CLOSE_GRACE_SECONDS = 3.0
WINDOW_CLOSE_FIRST = {"firefox", "firefox-bin", "librewolf", "thunderbird", "zen", "floorp"}      # how long an app may take to quit before it is stopped forcibly


def _quit_processes(procs: list[psutil.Process]) -> tuple[list[psutil.Process], list[psutil.Process]]:
    """Close apps without letting them ask anything ("tutup" is absolute): Linux sends SIGTERM to the app's
    main processes (apps quit without dialogs; Chromium browsers still save their session cleanly); Windows
    closes the windows like clicking X. Whatever is still running after CLOSE_GRACE_SECONDS (a "save
    changes?" dialog, a hung app) is killed. Returns (still_alive, access_denied)."""
    pids = {p.pid for p in procs}
    denied = []
    if IS_WINDOWS:
        import window_manager
        window_manager.close_windows_of(pids)
    else:
        def parent(p):
            try:
                return p.ppid()
            except psutil.Error:
                return None
        mains = [p for p in procs if parent(p) not in pids]         # main processes; children follow them
        gentle = [p for p in mains if (p.info.get("name") or "").lower() in WINDOW_CLOSE_FIRST]
        if gentle:
            # Firefox treats SIGTERM as a crash ("Restore Session" next start): close its windows like clicking X
            import window_manager
            window_manager.close_windows_of({p.pid for p in gentle})
            psutil.wait_procs(gentle, timeout=CLOSE_GRACE_SECONDS)
        for p in mains:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                denied.append(p)
    _, alive = psutil.wait_procs(procs, timeout=CLOSE_GRACE_SECONDS)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            if p not in denied:
                denied.append(p)
    _, alive = psutil.wait_procs(alive, timeout=2)
    return alive, denied


def close_application(name: str, force: bool = False) -> str:
    """Close a running application by name (e.g. 'chrome', 'notepad', 'spotify') completely and without
    questions: the app gets no chance to ask "save changes?" or "close all tabs?"; if it doesn't quit within
    a few seconds it is stopped forcibly. force=True kills it immediately (frozen apps, 'paksa tutup')."""
    procs = _matching_processes(name)
    if not procs:
        import window_manager
        w = window_manager._find(name)   # e.g. Store apps whose window has another process
        if not w:
            return f"Tidak ada aplikasi '{name}' yang sedang berjalan, {T}."
        try:
            procs = [psutil.Process(w["pid"])]
            procs[0].info = {"name": procs[0].name(), "pid": w["pid"]}
        except psutil.Error:
            return f"Tidak ada aplikasi '{name}' yang sedang berjalan, {T}."
        if _protected(procs[0].info["name"]):
            return f"{procs[0].info['name']} adalah proses sistem; tidak saya tutup, {T}."
    names = ", ".join(sorted({p.info["name"].removesuffix(".exe") for p in procs}))

    if not force:
        alive, denied = _quit_processes(procs)
        if denied:
            pids = sorted({p.pid for p in denied})
            label = ", ".join(sorted({p.info["name"].removesuffix(".exe") for p in denied}))
            return safety.guard(f"menghentikan {label} sebagai administrator ({ADMIN_PROMPT})",
                                _kill_elevated, pids=pids, label=label)
        if alive:
            return f"{names} tidak mau berhenti, {T}. Kemungkinan prosesnya sedang macet di sistem."
        return f"{names} sudah ditutup, {T}."

    denied = []
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied.append(p)
    if denied:
        pids = sorted({p.pid for p in denied})
        label = ", ".join(sorted({p.info["name"].removesuffix(".exe") for p in denied}))
        return safety.guard(f"menghentikan {label} sebagai administrator ({ADMIN_PROMPT})",
                            _kill_elevated, pids=pids, label=label)
    return f"{names} dihentikan paksa, {T}."


def _kill_elevated(pids: list[int], label: str) -> str:
    ok, out = run_elevated(("Stop-Process -Force -Id " + ",".join(str(p) for p in pids)) if IS_WINDOWS
                           else "kill -9 " + " ".join(str(p) for p in pids))
    if not ok:
        return f"{label} tidak dihentikan karena {out}, {T}."
    alive = [p for p in pids if psutil.pid_exists(p)]
    if alive:
        return f"{label} tetap berjalan walau sebagai administrator; kemungkinan ini proses sistem yang dilindungi, {T}."
    return f"{label} sudah dihentikan sebagai administrator, {T}."


def list_running_apps() -> str:
    """List the applications currently using the most memory."""
    usage: dict[str, float] = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            n = (p.info["name"] or "").removesuffix(".exe")
            if not n or _protected(n):
                continue
            usage[n] = usage.get(n, 0) + p.info["memory_info"].rss
        except Exception:
            continue
    top = sorted(usage.items(), key=lambda x: x[1], reverse=True)[:8]
    return "Aplikasi terberat saat ini: " + ", ".join(f"{n} ({_fmt_size(m)})" for n, m in top)


# ─────────────────────────── web & media ───────────────────────────

def google_search(query: str, browser: str = "") -> str:
    """Search Google in the browser (browser = 'chrome', 'firefox', 'edge'... or empty for the default)."""
    _open_url("https://www.google.com/search?q=" + urllib.parse.quote(query), browser)
    return f"Mencari '{query}' di Google{' lewat ' + browser.title() if browser else ''}, {T}."


def search_youtube(query: str) -> str:
    """Show YouTube search results for a query (does not auto-play)."""
    webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
    return f"Menampilkan hasil pencarian YouTube untuk {query}, {T}."


def play_youtube(query: str, mix: bool = False) -> str:
    """Backwards-compatible alias; the real player lives in media.py."""
    import media
    return media.play_youtube(query, mix)


def play_spotify(query: str) -> str:
    """Search a song/artist/playlist in the Spotify desktop app."""
    launch_on_default_desktop("spotify:search:" + urllib.parse.quote(query))
    return f"Mencari {query} di Spotify, {T}."


def media_play_pause() -> str:
    import media
    return media.media_control("toggle")


def media_next() -> str:
    import media
    return media.media_control("next")


def media_prev() -> str:
    import media
    return media.media_control("previous")


# ────────────────────────── volume & display ──────────────────────────

def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def set_volume(level: int) -> str:
    """Set the master volume to an exact percentage 0-100."""
    level = max(0, min(100, int(level)))
    if not IS_WINDOWS:
        if not ld.set_volume(level):
            return f"Gagal mengatur volume (wpctl/pactl tidak merespons), {T}."
        return f"Volume diatur ke {level} persen, {T}."
    try:
        ev = _endpoint_volume()
        ev.SetMute(0, None)
        ev.SetMasterVolumeLevelScalar(level / 100, None)
    except Exception as e:
        return f"Gagal mengatur volume: {e}"
    return f"Volume diatur ke {level} persen, {T}."


def get_volume() -> int:
    if not IS_WINDOWS:
        state = ld.get_volume()
        return state[0] if state else -1
    try:
        return round(_endpoint_volume().GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return -1


def volume_up(amount: int = 10) -> str:
    """Increase the volume by some percent (default 10)."""
    current = get_volume()
    if current < 0:
        for _ in range(max(1, amount // 2)):
            _press_key(VK_VOLUME_UP)
        return f"Volume dinaikkan, {T}."
    return set_volume(current + amount)


def volume_down(amount: int = 10) -> str:
    """Decrease the volume by some percent (default 10)."""
    current = get_volume()
    if current < 0:
        for _ in range(max(1, amount // 2)):
            _press_key(VK_VOLUME_DOWN)
        return f"Volume diturunkan, {T}."
    return set_volume(current - amount)


def volume_mute() -> str:
    """Toggle mute / unmute of the speakers."""
    if not IS_WINDOWS:
        state = ld.get_volume()
        if state is None:
            _press_key(VK_VOLUME_MUTE)
            return f"Mute diubah, {T}."
        muted = not state[1]
        ld.set_mute(muted)
        return f"Suara {'dimatikan' if muted else 'dinyalakan kembali'}, {T}."
    try:
        ev = _endpoint_volume()
        muted = not ev.GetMute()
        ev.SetMute(int(muted), None)
        return f"Suara {'dimatikan' if muted else 'dinyalakan kembali'}, {T}."
    except Exception:
        _press_key(VK_VOLUME_MUTE)
        return f"Mute diubah, {T}."


def set_brightness(level: int) -> str:
    """Set the laptop screen brightness to a percentage 0-100."""
    level = max(0, min(100, int(level)))
    if not IS_WINDOWS:
        if not ld.set_brightness(level):
            return f"Layar ini tidak mendukung pengaturan kecerahan lewat software (butuh brightnessctl), {T}."
        return f"Kecerahan layar diatur ke {level} persen, {T}."
    r = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
            f" | Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness={level}}}")
    if r.returncode != 0:
        return f"Layar ini tidak mendukung pengaturan kecerahan lewat software, {T}."
    return f"Kecerahan layar diatur ke {level} persen, {T}."


def _copy_image_to_clipboard(image):
    import io
    buf = io.BytesIO()
    if not IS_WINDOWS:
        image.save(buf, "PNG")
        if not ld.clipboard_set_image(buf.getvalue()):
            raise OSError("wl-copy gagal")
        return
    import win32clipboard
    image.convert("RGB").save(buf, "BMP")
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, buf.getvalue()[14:])  # strip BMP file header
    finally:
        win32clipboard.CloseClipboard()


def take_screenshot(open_after: bool = False) -> str:
    """Take a screenshot of all screens, save it to the screenshot folder and copy it to the
    clipboard (ready to paste). open_after=True also opens the image."""
    folder = Path(config.SCREENSHOT_DIR) if config.SCREENSHOT_DIR else _known_folder("pictures") / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Digidaw_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    image = oscompat.grab_screen()
    image.save(path)
    copied = ""
    try:
        _copy_image_to_clipboard(image)
        copied = " dan sudah disalin ke clipboard"
    except Exception:
        pass
    if open_after:
        oscompat.open_path(path)
    return f"Screenshot tersimpan di folder {folder.name}{copied}, {T}."


# ───────────────────────── keyboard & clipboard ─────────────────────────

TERMINAL_CLASSES = ("kitty", "alacritty", "foot", "wezterm", "ghostty", "konsole", "gnome-terminal", "xfce4-terminal",
                    "xterm", "terminator", "tilix")


def _paste_keys() -> list[str]:
    """Ctrl+V, or Ctrl+Shift+V when the active window is a terminal."""
    w = ld.active_window()
    terminal = w and any(t in (w["class"] + " " + w["process"]).lower() for t in TERMINAL_CLASSES)
    return ["ctrl", "shift", "v"] if terminal else ["ctrl", "v"]


def type_text(text: str, press_enter: bool = False) -> str:
    """Type text into the currently focused window (like dictation into any app).
    press_enter=True also presses Enter afterwards ('ketik ls lalu enter')."""
    text = text.rstrip("\n")
    if not IS_WINDOWS:
        previous = ld.clipboard_get()
        ld.clipboard_set(text)
        time.sleep(0.2)
        ld.send_keys(_paste_keys())
        time.sleep(0.3)
        if press_enter:
            ld.send_keys(["enter"])
        if previous:
            ld.clipboard_set(previous)
        return f"Teks sudah saya ketik{' dan Enter ditekan' if press_enter else ''}, {T}."
    import pyautogui
    import pyperclip
    previous = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    if press_enter:
        pyautogui.press("enter")
    pyperclip.copy(previous)
    return f"Teks sudah saya ketik{' dan Enter ditekan' if press_enter else ''}, {T}."


def press_keys(keys: str) -> str:
    """Press a keyboard shortcut, keys joined with '+'. Examples: 'ctrl+c', 'alt+tab',
    'win+d' (show desktop), 'ctrl+w' (close tab), 'alt+f4' (close window), 'enter', 'f5'."""
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    aliases = {"windows": "win", "control": "ctrl", "return": "enter", "escape": "esc"}
    parts = [aliases.get(k, k) for k in parts]
    if not IS_WINDOWS:
        # Hyprland owns these shortcuts, so do what they mean instead of sending them to a window
        combo = "+".join(parts)
        if combo == "win+d":
            return show_desktop()
        if combo == "alt+f4":
            import window_manager
            return window_manager.window_action("close")
        if combo in ("alt+tab", "alt+shift+tab"):
            ld.dispatch("cyclenext", *(["prev"] if "shift" in parts else []))
            ld.dispatch("bringactivetotop")
            return f"Pindah jendela, {T}."
        if not ld.send_keys(parts):
            return f"Tombol {combo} tidak bisa ditekan, {T}."
        return f"Menekan {combo}, {T}."
    import pyautogui
    pyautogui.hotkey(*parts)
    return f"Menekan {'+'.join(parts)}, {T}."


def show_desktop() -> str:
    """Minimize all windows and show the desktop."""
    if not IS_WINDOWS:
        import window_manager
        return window_manager.window_action("minimize_all")
    return press_keys("win+d")


def get_clipboard() -> str:
    """Read the text currently in the clipboard (e.g. to summarize/translate what the user copied)."""
    import pyperclip
    text = pyperclip.paste()
    return f"Isi clipboard: {text[:4000]}" if text else f"Clipboard kosong, {T}."


def set_clipboard(text: str) -> str:
    """Put text into the clipboard so the user can paste it (e.g. a translation or summary)."""
    import pyperclip
    pyperclip.copy(text)
    return f"Sudah saya salin ke clipboard, {T}."


def start_dictation() -> str:
    """Start voice dictation: everything the user says next is typed into the active window
    (Word, WhatsApp, browser...) until they say 'selesai dikte'. Voice mode only."""
    import events
    if not events.state.get("voice_enabled"):
        return f"Mode dikte hanya tersedia di mode suara, {T}."
    events.set_dictation(True)
    return (f"Mode dikte aktif, {T}. Klik dulu tempat mengetik, lalu bicaralah. "
            f"Ucapkan 'selesai dikte' untuk berhenti.")


# ─────────────────────────── notes & files ───────────────────────────

def _notes_dir() -> Path:
    folder = Path(config.NOTES_DIR) if config.NOTES_DIR else _known_folder("documents") / "Digidaw Notes"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _project_dir() -> Path | None:
    """The focus project folder from the Control Panel, else the folder Digidaw builds projects in."""
    folder = _focus_project_dir()
    if folder:
        return folder
    builder = Path(config.PROJECTS_ROOT) if config.PROJECTS_ROOT else Path.home() / "Projects"
    return builder if builder.is_dir() else None


def _focus_project_dir() -> Path | None:
    return Path(config.PROJECT_DIR) if config.PROJECT_DIR and os.path.isdir(config.PROJECT_DIR) else None


def _latest_note() -> Path | None:
    notes = sorted(_notes_dir().glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return notes[0] if notes else None


def _open_note(path: Path):
    before = _hwnds()
    oscompat.open_text_file(path)
    bring_forward(before, "notepad" if IS_WINDOWS else os.path.basename(ld.text_editor() or path.name))


def write_note(content: str, title: str = "") -> str:
    """Write a note: saves the text to a .txt file and opens it in Notepad (a text editor on Linux).
    Use for 'buka notepad dan catat ...', 'tulis catatan ...', 'catat ini ...'."""
    safe_title = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:60]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = _notes_dir() / f"{safe_title or 'Catatan'}_{stamp}.txt"
    path.write_text(content.strip() + "\n", encoding="utf-8")
    _open_note(path)
    return f"Catatan sudah saya tulis dan buka di {oscompat.TEXT_EDITOR_NAME}, {T}."


def append_to_last_note(content: str) -> str:
    """Add more text to the most recent note and reopen it in Notepad."""
    note = _latest_note()
    if not note:
        return write_note(content)
    with open(note, "a", encoding="utf-8") as f:
        f.write(content.strip() + "\n")
    _open_note(note)
    return f"Sudah saya tambahkan ke catatan {note.stem}, {T}."


def read_last_note() -> str:
    """Read out the content of the most recent note."""
    note = _latest_note()
    if not note:
        return f"Belum ada catatan, {T}."
    return f"Catatan terakhir ({note.stem}): {note.read_text(encoding='utf-8')[:800]}"


FOLDER_ALIASES = {
    "downloads": "downloads", "download": "downloads", "unduhan": "downloads",
    "documents": "documents", "dokumen": "documents", "desktop": "desktop",
    "pictures": "pictures", "gambar": "pictures", "foto": "pictures",
    "music": "music", "musik": "music", "videos": "videos", "video": "videos",
}


def _resolve_folder(name: str) -> str:
    n = name.lower().strip()
    key = _match_key(n, FOLDER_ALIASES)
    if key:
        return str(_known_folder(FOLDER_ALIASES[key]))
    if n in ("recycle bin", "sampah", "tempat sampah", "trash"):
        return "shell:RecycleBinFolder" if IS_WINDOWS else "trash:///"
    if n in ("this pc", "komputer", "my computer", "pc"):
        return "shell:MyComputerFolder" if IS_WINDOWS else "/"
    if n in ("catatan", "notes", "digidaw notes"):
        return str(_notes_dir())
    if n in ("project", "proyek", "project saya", "proyek saya", "folder project", "folder proyek"):
        project = _project_dir()
        return str(project) if project else str(Path.home())
    m = re.fullmatch(r"(?:drive|disk|local disk)?\s*([a-z])(?::)?", n)
    if m and IS_WINDOWS:
        return f"{m.group(1).upper()}:\\"
    if n in ("home", "rumah", "folder home", "folder pengguna"):
        return str(Path.home())
    return os.path.expandvars(os.path.expanduser(name))


SPECIAL_FOLDERS = ("shell:", "trash:")


def open_folder(name: str) -> str:
    """Open a folder in the file manager. Accepts 'downloads', 'documents', 'desktop', 'pictures',
    'music', 'videos', 'recycle bin', 'this pc', 'catatan', 'project', a drive letter like 'D', or a path."""
    target = _resolve_folder(name)
    if not target.startswith(SPECIAL_FOLDERS) and not os.path.isdir(target):
        return f"Folder '{name}' tidak ditemukan, {T}."
    before = _hwnds()
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", target])
    else:
        ld.open_in_file_manager(target)
    bring_forward(before, "explorer" if IS_WINDOWS else ld.file_manager(), allow_existing=False)
    return f"Membuka folder {name}, {T}."


def create_folder(name: str, location: str = "") -> str:
    """Create a new folder. Default location: the user's focus project folder (or the desktop
    if none is set). location may be 'desktop', 'documents', 'project', a drive or a path."""
    location = location or ("project" if _focus_project_dir() else "desktop")
    path = Path(_resolve_folder(location)) / re.sub(r'[\\/:*?"<>|]+', "", name)
    path.mkdir(parents=True, exist_ok=True)
    return f"Folder {path.name} dibuat di {path.parent}, {T}."


def find_files(name: str, location: str = "") -> str:
    """Search for files/folders whose name contains the given text. Without a location it searches
    the user's focus project folder first, then the whole home folder. location may be 'project',
    'downloads', 'documents', a drive letter, or a path."""
    if not location and _project_dir():
        in_project = find_files(name, str(_project_dir()))
        if in_project.startswith("Ditemukan"):
            return in_project
    root = Path(_resolve_folder(location)) if location else Path.home()
    needle = name.lower()
    found: list[str] = []
    deadline = time.time() + 10
    skip = {"appdata", "node_modules", ".git", "$recycle.bin", "windows", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
        for f in filenames + dirnames:
            if needle in f.lower():
                found.append(os.path.join(dirpath, f))
        if len(found) >= 8 or time.time() > deadline:
            break
    if not found:
        return f"Tidak ada file bernama '{name}' di {root}, {T}."
    return "Ditemukan: " + "; ".join(found[:8])


def open_project(app: str = "") -> str:
    """Open the user's focus project folder (set in the Control Panel) in VS Code, File Explorer
    or a terminal. app: 'vscode', 'explorer', 'terminal' or empty for the user's preferred app."""
    project = _project_dir()
    if not project:
        return f"Belum ada folder project, {T}. Pilih di Control Panel, bagian Folder & Project, atau buat project dulu."
    app = (app or config.PROJECT_EDITOR).lower()
    if "code" in app and not shutil.which("code"):
        app = "explorer"
    before = _hwnds()
    if not IS_WINDOWS:
        if "code" in app:
            ld.spawn(["code", str(project)])
        elif "terminal" in app or app in ("cmd", "powershell"):
            ld.open_terminal(str(project))
        else:
            ld.open_in_file_manager(str(project))
        bring_forward(before, "code" if "code" in app else
                      (os.path.basename(ld.terminal() or "") if "terminal" in app else ld.file_manager()))
        return f"Membuka project {project.name}, {T}."
    if "code" in app:
        subprocess.Popen(f'code "{project}"', shell=True, creationflags=NO_WINDOW)
    elif "terminal" in app or app in ("cmd", "powershell"):
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-d", str(project)])
        else:
            subprocess.Popen(["cmd", "/c", "start", "powershell", "-NoExit", "-Command", f"Set-Location '{project}'"])
    else:
        subprocess.Popen(["explorer.exe", str(project)])
    bring_forward(before, "code" if "code" in app else ("windowsterminal" if "terminal" in app else "explorer"))
    return f"Membuka project {project.name}, {T}."


def project_location() -> str:
    """Where the user's projects are ('di mana lokasi project saya', 'project saya disimpan di mana')."""
    import project_builder
    focus = _focus_project_dir()
    root = project_builder._projects_root()
    built = project_builder.all_projects()
    text = f"Project yang saya buat disimpan di {root}"
    if built:
        text += f" ({len(built)} project, terbaru {built[0].get('name', Path(built[0]['folder']).name)})"
    text += "."
    if focus and focus != root:
        text += f" Folder project fokus Anda: {focus}."
    return text + f" Ucapkan 'buka folder project' untuk membukanya, {T}."


def open_file(path: str) -> str:
    """Open a file with its default application (full path, e.g. from find_files)."""
    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.exists(p):
        return f"File {path} tidak ditemukan, {T}."
    before = _hwnds()
    oscompat.open_path(p)
    bring_forward(before, os.path.splitext(os.path.basename(p))[0])     # window titles show the file name
    return f"Membuka {os.path.basename(p)}, {T}."


LINUX_SYSTEM_DIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/proc", "/root", "/run",
                     "/sbin", "/srv", "/sys", "/usr", "/var", "/home", "/mnt", "/media")


def delete_file(path: str) -> str:
    """Move a file or folder to the Recycle Bin / Trash (recoverable). Needs a full path;
    use find_files first if the user only gives a name."""
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.exists(p):
        return f"File {path} tidak ditemukan, {T}."
    if not IS_WINDOWS:
        real = os.path.realpath(p)
        protected = {str(Path.home()), str(config.BASE_DIR), *LINUX_SYSTEM_DIRS}
        if real in protected or p in protected or \
                any(real.startswith(d + "/") for d in LINUX_SYSTEM_DIRS if d not in ("/", "/home", "/mnt", "/media")):
            return f"Demi keamanan, saya tidak akan menghapus {p}, {T}."
        ok, error = ld.move_to_trash(p)
        if not ok:
            return f"Gagal memindahkan {os.path.basename(p)} ke Tempat Sampah ({error or 'tidak diketahui'}), {T}."
        return f"{os.path.basename(p)} sudah dipindahkan ke Tempat Sampah, {T}."
    forbidden = {str(Path.home()).lower(), str(config.BASE_DIR).lower(),
                 os.environ.get("WINDIR", r"C:\Windows").lower()}
    if p.lower() in forbidden or re.fullmatch(r"[a-zA-Z]:\\?", p) or \
            p.lower().startswith(os.environ.get("WINDIR", r"C:\Windows").lower()):
        return f"Demi keamanan, saya tidak akan menghapus {p}, {T}."
    from win32com.shell import shell, shellcon
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    result, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, p, None, flags, None, None))
    if result != 0 or aborted:
        return f"Gagal menghapus {os.path.basename(p)} (kode {result}), {T}."
    return f"{os.path.basename(p)} sudah dipindahkan ke Recycle Bin, {T}."


# ───────────────────────── cleanup & recycle bin ─────────────────────────

BIN = oscompat.TRASH_NAME

if IS_WINDOWS:
    class _SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                    ("i64NumItems", ctypes.c_longlong)]


def _recycle_bin_info() -> tuple[int, int]:
    if not IS_WINDOWS:
        return ld.trash_info()
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    return info.i64NumItems, info.i64Size


def _do_empty_recycle_bin() -> str:
    items, size = _recycle_bin_info()
    if not IS_WINDOWS:
        if not ld.empty_trash():
            return f"Sebagian isi {BIN} tidak bisa dihapus, {T}."
    else:
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)
    return f"{BIN} sudah dikosongkan. {items} item, {_fmt_size(size)} dibersihkan, {T}."


def empty_recycle_bin() -> str:
    """Permanently empty the Recycle Bin / Trash (asks the user for confirmation)."""
    items, size = _recycle_bin_info()
    if items == 0:
        return f"Tidak ada yang dihapus: {BIN} memang sudah kosong (0 item), {T}."
    return safety.guard(f"kosongkan {BIN} ({items} item, {_fmt_size(size)})", _do_empty_recycle_bin)


def recycle_bin_status() -> str:
    """Report how many items and how much space is in the Recycle Bin / Trash."""
    items, size = _recycle_bin_info()
    return f"{BIN} berisi {items} item dengan total {_fmt_size(size)}, {T}."


# Linux /tmp entries that belong to running sessions (sockets, tmux, ssh agents, systemd) are never touched
_KEEP_TEMP = re.compile(r"^(?:\.|tmux-|ssh-|systemd-|pulse-|claude|hypr|xdg-|krb5|dbus-|digidaw)", re.IGNORECASE)


def _newest_mtime(item: Path) -> float:
    newest = item.lstat().st_mtime
    if item.is_dir() and not item.is_symlink():
        for f in item.rglob("*"):
            try:
                newest = max(newest, f.lstat().st_mtime)
            except OSError:
                continue
    return newest


def _do_clean_temp_files() -> str:
    import tempfile
    temp = Path(os.environ.get("TEMP", Path.home() / "AppData/Local/Temp")) if IS_WINDOWS else Path(tempfile.gettempdir())
    cutoff = time.time() - 24 * 3600  # keep anything touched in the last day
    freed, count = 0, 0
    uid = None if IS_WINDOWS else os.getuid()
    for item in temp.iterdir():
        try:
            if not IS_WINDOWS:
                st = item.lstat()
                if st.st_uid != uid or _KEEP_TEMP.match(item.name) or \
                        not (item.is_file() or item.is_dir()) or item.is_symlink() or _newest_mtime(item) > cutoff:
                    continue
            if item.stat().st_mtime > cutoff:
                continue
            if item.is_dir():
                size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                shutil.rmtree(item)
            else:
                size = item.stat().st_size
                item.unlink()
            freed += size
            count += 1
        except Exception:
            continue  # file in use or access denied
    return f"Pembersihan selesai: {count} item sementara dihapus, {_fmt_size(freed)} ruang dibebaskan, {T}."


def clean_temp_files() -> str:
    """Delete old temporary files (%TEMP% on Windows, your own files in /tmp on Linux) to free disk space
    (asks for confirmation)."""
    return safety.guard("hapus file sementara (temp) yang berumur lebih dari 1 hari", _do_clean_temp_files)


def _do_system_cleanup() -> str:
    temp = _do_clean_temp_files().removesuffix(f", {T}.")
    items, _ = _recycle_bin_info()
    bin_result = _do_empty_recycle_bin() if items else f"{BIN} sudah kosong."
    return f"Clean Slate selesai. {temp}. {bin_result}"


def system_cleanup() -> str:
    """'Clean Slate': delete old temp files AND empty the Recycle Bin / Trash in one go (one confirmation)."""
    items, size = _recycle_bin_info()
    return safety.guard(f"bersihkan file temp dan kosongkan {BIN} ({items} item, {_fmt_size(size)})",
                        _do_system_cleanup)


# ─────────────────────────── system & power ───────────────────────────

def get_system_status() -> str:
    """Report CPU, RAM, disk and battery status."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(oscompat.system_disk())
    battery = psutil.sensors_battery()
    status = (f"Diagnostik sistem: CPU {cpu:.0f} persen, RAM terpakai {mem.percent:.0f} persen "
              f"dengan {mem.available / 1024**3:.1f} GB tersedia, {oscompat.SYSTEM_DISK_NAME} tersisa "
              f"{disk.free / 1024**3:.0f} GB.")
    if battery:
        plug = "sedang dicas" if battery.power_plugged else "memakai baterai"
        status += f" Baterai {battery.percent:.0f} persen, {plug}."
    return status


def get_network_info() -> str:
    """Report WiFi network name, local IP and public IP."""
    parts = []
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW, errors="replace").stdout
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            ssid = m.group(1).strip() if m else ""
        else:
            ssid = ld.wifi_name()
        if ssid is not None:
            parts.append(f"terhubung ke WiFi {ssid}" if ssid else "tidak terhubung ke WiFi")
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        parts.append(f"IP lokal {s.getsockname()[0]}")
        s.close()
        parts.append(f"IP publik {requests.get('https://api.ipify.org', timeout=5).text}")
    except Exception:
        parts.append("tidak ada koneksi internet")
    return "Jaringan: " + ", ".join(parts) + "."


def get_time() -> str:
    """Current time."""
    now = datetime.datetime.now()
    return f"Sekarang pukul {now:%H.%M}, {T}."


def get_date() -> str:
    """Current day and date."""
    now = datetime.datetime.now()
    return f"Hari ini {HARI[now.weekday()]}, {now.day} {BULAN[now.month - 1]} {now.year}, {T}."


def lock_workstation() -> str:
    """Lock the PC (lock screen)."""
    if not IS_WINDOWS:
        if not ld.lock_screen():
            return f"Layar tidak bisa dikunci (hyprlock / loginctl tidak merespons), {T}."
        return f"Mengunci komputer sekarang, {T}."
    ctypes.windll.user32.LockWorkStation()
    return f"Mengunci komputer sekarang, {T}."


POWER_UNIT = "digidaw-power"      # Linux: the delayed shutdown/restart is a cancellable systemd user timer


def _do_power_linux(action: str) -> str:
    if action in ("shutdown", "restart"):
        ld.run(["systemctl", "--user", "stop", f"{POWER_UNIT}.timer"])
        ld.run(["systemctl", "--user", "reset-failed", f"{POWER_UNIT}.timer", f"{POWER_UNIT}.service"])
        r = ld.run(["systemd-run", "--user", f"--unit={POWER_UNIT}", "--on-active=15", "--timer-property=AccuracySec=1s",
                    "systemctl", "poweroff" if action == "shutdown" else "reboot"])
        if not r or r.returncode != 0:
            return f"Gagal menjadwalkan {action}: {(r.stderr if r else '').strip()[:120]}, {T}."
        what = "dimatikan" if action == "shutdown" else "restart"
        return f"Komputer akan {what} dalam 15 detik. Katakan 'batalkan shutdown' untuk membatalkan, {T}."
    if action == "logoff":
        threading.Timer(2, ld.log_out).start()
        return f"Sign out sekarang, {T}."
    if action == "sleep":
        threading.Timer(2, lambda: ld.run(["systemctl", "suspend"])).start()
        return f"Masuk mode sleep, {T}. Sampai jumpa."
    return "Aksi tidak dikenal."


def _do_power(action: str) -> str:
    if not IS_WINDOWS:
        return _do_power_linux(action)
    if action == "shutdown":
        subprocess.run(["shutdown", "/s", "/t", "15"], creationflags=NO_WINDOW)
        return f"Komputer akan dimatikan dalam 15 detik. Katakan 'batalkan shutdown' untuk membatalkan, {T}."
    if action == "restart":
        subprocess.run(["shutdown", "/r", "/t", "15"], creationflags=NO_WINDOW)
        return f"Komputer akan restart dalam 15 detik. Katakan 'batalkan shutdown' untuk membatalkan, {T}."
    if action == "logoff":
        subprocess.run(["shutdown", "/l"], creationflags=NO_WINDOW)
        return f"Sign out sekarang, {T}."
    if action == "sleep":
        threading.Timer(2, lambda: ctypes.windll.powrprof.SetSuspendState(False, True, False)).start()
        return f"Masuk mode sleep, {T}. Sampai jumpa."
    return "Aksi tidak dikenal."


def shutdown_pc() -> str:
    """Shut down the computer (asks confirmation, 15s delay, can be cancelled)."""
    return safety.guard("matikan komputer", _do_power, action="shutdown")


def restart_pc() -> str:
    """Restart the computer (asks confirmation, 15s delay, can be cancelled)."""
    return safety.guard("restart komputer", _do_power, action="restart")


def sleep_pc() -> str:
    """Put the computer to sleep (asks confirmation)."""
    return safety.guard("sleep komputer", _do_power, action="sleep")


def log_off() -> str:
    """Sign out of the desktop session (asks confirmation)."""
    return safety.guard("sign out dari Windows" if IS_WINDOWS else "keluar (log out) dari sesi desktop",
                        _do_power, action="logoff")


def cancel_shutdown() -> str:
    """Cancel a scheduled shutdown or restart."""
    if not IS_WINDOWS:
        active = ld.out(["systemctl", "--user", "is-active", f"{POWER_UNIT}.timer"]) == "active"
        ld.run(["systemctl", "--user", "stop", f"{POWER_UNIT}.timer"])
        legacy = ld.run(["shutdown", "-c"])           # a `shutdown +N` scheduled outside Digidaw
        if active or (legacy and legacy.returncode == 0 and not legacy.stderr):
            return f"Shutdown dibatalkan, {T}."
        return f"Tidak ada jadwal shutdown yang aktif, {T}."
    r = subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
    if r.returncode == 0:
        return f"Shutdown dibatalkan, {T}."
    return f"Tidak ada jadwal shutdown yang aktif, {T}."


def _do_run_command(command: str) -> str:
    try:
        r = _shell(command, timeout=60)
    except subprocess.TimeoutExpired:
        return "Perintah melebihi batas waktu 60 detik."
    out = (r.stdout or "").strip() or (r.stderr or "").strip() or "(tanpa output)"
    if (r.returncode != 0 or r.stderr) and ACCESS_DENIED_RE.search((r.stderr or "") + out):
        return safety.guard(f"menjalankan ulang perintah itu sebagai administrator ({ADMIN_PROMPT})",
                            _run_command_elevated, command=command) + \
            " Catatan untuk asisten: perintahnya ditolak karena butuh hak administrator."
    status = "berhasil" if r.returncode == 0 else f"gagal (kode {r.returncode})"
    return f"Perintah {status}. Output: {out[:1500]}"


def _run_command_elevated(command: str) -> str:
    ok, out = run_elevated(command, timeout=120)
    if not ok:
        return f"Perintah tidak dijalankan karena {out}, {T}."
    if ACCESS_DENIED_RE.search(out):
        return f"Walau sebagai administrator, sistem tetap menolaknya (kemungkinan proses sistem yang dilindungi), {T}."
    return f"Perintah sudah dijalankan sebagai administrator, {T}." + (f" Output: {out[:600]}" if out else "")


def run_command(command: str) -> str:
    """Run a PowerShell command on the PC and return its output (asks confirmation).
    Use only when no other specific tool can do the job."""
    return safety.guard(f"jalankan perintah {SHELL_NAME}: {command}", _do_run_command, command=command)


if not IS_WINDOWS:   # these docstrings are what Gemini reads about the tools
    run_command.__doc__ = """Run a bash shell command on this Arch Linux PC and return its output (asks confirmation).
    Use only when no other specific tool can do the job. sudo is not available non-interactively."""
    lock_workstation.__doc__ = "Lock the PC (Hyprland lock screen)."
    take_screenshot.__doc__ = take_screenshot.__doc__.replace("all screens", "all monitors")


# ─────────────────────────── info & timers ───────────────────────────

def get_weather(city: str = "") -> str:
    """Current weather for a city (empty = current location based on IP)."""
    city = (city or config.DEFAULT_CITY or "").strip()
    words = city.split()
    # "Bandar Lampung tanjung bintang" isn't known: try "Bandar Lampung tanjung", then "Bandar Lampung", ... and
    # finally the location of the internet connection
    attempts = [" ".join(words[:n]) for n in range(len(words), 0, -1)] + [""]
    reached = False
    for place in dict.fromkeys(attempts):
        try:
            url = f"https://wttr.in/{urllib.parse.quote(place)}?format=%l:+%C,+suhu+%t,+kelembapan+%h,+angin+%w&lang=id"
            text = requests.get(url, timeout=8).text.strip()
            reached = True
        except Exception:
            continue
        if text and not re.search(r"unknown location|not found|sorry", text, re.IGNORECASE) and len(text) < 200:
            return f"Cuaca {text}."
    if reached:
        return f"Maaf {T}, kota '{city}' tidak ditemukan di layanan cuaca."
    return f"Maaf {T}, data cuaca sedang tidak bisa diakses."


ANNOUNCE = None   # set by main: speaks + shows alerts on the HUD (core.announce)


def timers_snapshot() -> list[dict]:
    import alarms
    return alarms.snapshot()


def sleep_timer(minutes: float, target: str = "music") -> str:
    """Do something after a while: target='music' pauses the music ('matikan musik dalam 30 menit',
    'sleep timer 20 menit'), target='laptop' shuts the PC down ('matikan laptop dalam 15 menit',
    asks confirmation)."""
    minutes = max(0.1, float(minutes))
    label = f"{int(minutes)} menit" if minutes >= 1 else f"{int(minutes * 60)} detik"
    if target.lower() in ("laptop", "komputer", "pc", "shutdown", "computer"):
        return safety.guard(f"mematikan komputer dalam {label}", _schedule_shutdown, minutes=minutes, label=label)
    import alarms
    import media
    alarms.schedule_action(minutes * 60, "Sleep timer: musik dihentikan", lambda: media.media_control("pause"))
    return f"Musik akan saya hentikan dalam {label}, {T}."


def _schedule_shutdown(minutes: float, label: str) -> str:
    import alarms
    alarms.schedule_action(minutes * 60, "Waktunya mematikan komputer", lambda: _do_power("shutdown"))
    return f"Komputer akan dimatikan dalam {label}, {T}. Ucapkan 'batalkan timer' kalau berubah pikiran."


def set_timer(minutes: float, message: str = "") -> str:
    """Kept for protocols: a countdown timer (see alarms.set_timer)."""
    import alarms
    return alarms.set_timer(minutes, message)


def list_timers() -> str:
    import alarms
    return alarms.list_timers()


# ─────────────────────────── memory skills ───────────────────────────

def remember(fact: str) -> str:
    """Save a fact about the user to long-term memory (e.g. 'nama saya Budi', 'saya suka kopi')."""
    return memory.add(fact)


def forget(keyword: str) -> str:
    """Delete memories containing the keyword."""
    return memory.remove(keyword)


def recall_memories() -> str:
    """List everything stored in long-term memory."""
    return memory.as_text()


# Skills exposed to Gemini function calling (media/window/AI skills live in their own modules)
AI_TOOLS = [
    open_application, open_many, close_application, close_many, close_all_apps, list_running_apps, open_website, google_search, project_location,
    search_youtube, play_spotify,
    set_volume, volume_up, volume_down, volume_mute, set_brightness, take_screenshot,
    type_text, press_keys, get_clipboard, set_clipboard, start_dictation,
    write_note, append_to_last_note, read_last_note,
    open_folder, create_folder, find_files, open_file, delete_file, open_project,
    empty_recycle_bin, recycle_bin_status, clean_temp_files, system_cleanup,
    get_system_status, get_network_info, get_time, get_date, lock_workstation,
    shutdown_pc, restart_pc, sleep_pc, log_off, cancel_shutdown, run_command,
    get_weather, sleep_timer,
    remember, forget, recall_memories,
]
