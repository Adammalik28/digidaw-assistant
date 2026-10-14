"""
Screen context: Digidaw knows which window / browser tab the user was looking at, so "ini" works:
"tutup tab ini", "ringkas halaman ini", "simpan link ini", "salin link ini",
"jelaskan / terjemahkan teks yang saya blok".

A light poller remembers the last foreground window that isn't Digidaw itself (clicking the HUD
must not make the HUD "this page"). The URL of a browser tab is read from the address bar through
Windows UI Automation (Chrome, Edge, Brave, Opera, Firefox); nothing is injected into the browser.
On Linux (Hyprland) the window comes from hyprctl and the URL from the browser's history (linux_browser.py).
"""
import re
import time
import datetime
import threading

import psutil
import requests

import config
import oscompat

IS_WINDOWS = oscompat.IS_WINDOWS
if IS_WINDOWS:
    import win32gui
    import win32process
else:
    import linux_desktop as ld

T = config.USER_TITLE
BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
            "opera.exe": "Opera", "vivaldi.exe": "Vivaldi",
            # Linux process names
            "chrome": "Chrome", "chromium": "Chromium", "msedge": "Edge", "firefox": "Firefox", "firefox-bin": "Firefox",
            "brave": "Brave", "opera": "Opera", "vivaldi-bin": "Vivaldi", "librewolf": "LibreWolf"}
SHELL_PROCESSES = {"shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe", "searchapp.exe",
                   "textinputhost.exe", "lockapp.exe",
                   "rofi", "wofi", "fuzzel", "waybar", "swaync", "hyprlock", "wlogout", "nwg-drawer"}
TITLE_SUFFIX =re.compile(r"\s+[—–-]\s+(?:Google Chrome|Mozilla Firefox|Microsoft​? Edge|Brave|Opera|Vivaldi|Chromium)$")

_last = {"hwnd": 0, "title": "", "process": "", "pid": 0, "time": 0.0}
_uia = {"auto": None, "mod": None}
_lock = threading.Lock()


# ───────────────────────────── active window tracking ─────────────────────────────

def _describe(hwnd) -> dict | None:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        proc = psutil.Process(pid).name()
    except psutil.Error:
        proc = ""
    return {"hwnd": hwnd, "title": title, "process": proc, "pid": pid}


def _is_self(w: dict) -> bool:
    import window_manager
    return (window_manager._is_digidaw(w) or not w["title"] or w["title"] in window_manager.IGNORED_TITLES
            or w["title"] in ("Task Switching", "Pengalihan Tugas", "Search", "Cari", "Start", "Mulai")
            or w["process"].lower() in SHELL_PROCESSES)


def _foreground() -> dict | None:
    if IS_WINDOWS:
        return _describe(win32gui.GetForegroundWindow())
    return ld.active_window()


def _poll():
    while True:
        try:
            w = _foreground()
            if w and w["title"] and not _is_self(w):
                with _lock:
                    _last.update(w, time=time.time())
        except Exception:
            pass
        time.sleep(0.7)


def start():
    threading.Thread(target=_poll, daemon=True, name="screen-context").start()


def current() -> dict | None:
    """The window the user is working in (the last non-Digidaw foreground window that still exists)."""
    with _lock:
        w = dict(_last)
    if w["hwnd"] and IS_WINDOWS and win32gui.IsWindow(w["hwnd"]):
        w["title"] = win32gui.GetWindowText(w["hwnd"]) or w["title"]
        return w
    if w["hwnd"] and not IS_WINDOWS:
        live = ld.window_exists(w["hwnd"])
        if live:
            w["title"] = live["title"] or w["title"]
            return w
    import window_manager
    return next((x for x in window_manager._windows() if not window_manager._is_digidaw(x)), None)


def is_browser(w: dict | None) -> bool:
    return bool(w) and w["process"].lower() in BROWSERS


def page_title(w: dict) -> str:
    return TITLE_SUFFIX.sub("", w["title"]).strip()


def prompt_hint() -> str:
    """One line for the AI's system prompt."""
    w = current()
    if not w:
        return ""
    kind = f"tab {BROWSERS[w['process'].lower()]}" if is_browser(w) else w["process"].removesuffix(".exe")
    return (f"\nThe window the user is currently looking at: \"{w['title'][:120]}\" ({kind}). When they say "
            "'ini' (this page/tab/text/link) they mean this window: use summarize_current_page, close_current_tab, "
            "save_current_link, copy_current_link or get_selected_text.")


# ───────────────────────────── browser URL (UI Automation) ─────────────────────────────

def _automation():
    if _uia["auto"] is None:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        _uia["mod"] = UIA
        _uia["auto"] = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    return _uia["auto"], _uia["mod"]


def _linux_url(w: dict) -> str:
    """Linux: look the page title up in the browser's history; as a last resort copy the address bar
    (Ctrl+L, Ctrl+C, Esc), which briefly moves the keyboard focus to the address bar."""
    import linux_browser
    url = linux_browser.url_for_title(page_title(w), w["process"], w.get("class", ""))
    if url:
        return url
    before = ld.clipboard_get()
    marker = f"__digidaw_{time.time()}__"
    ld.clipboard_set(marker)
    ld.focus(w["hwnd"])
    time.sleep(0.15)
    ld.send_keys(["ctrl", "l"], w["hwnd"])
    time.sleep(0.15)
    ld.send_keys(["ctrl", "c"], w["hwnd"])
    value = ""
    for _ in range(10):
        time.sleep(0.08)
        value = ld.clipboard_get()
        if value != marker:
            break
    ld.send_keys(["escape"], w["hwnd"])
    if before:
        ld.clipboard_set(before)
    return "" if value == marker else value


def current_url(w: dict | None = None) -> str:
    """Address of the active tab in the browser window (\"\" if it can't be read)."""
    w = w or current()
    if not is_browser(w):
        return ""
    if not IS_WINDOWS:
        value = _linux_url(w).strip()
        if not value or " " in value:
            return ""
        return value if re.match(r"^[a-z][\w+.-]*:", value) else "https://" + value
    import comtypes
    comtypes.CoInitialize()
    try:
        auto, UIA = _automation()
        root = auto.ElementFromHandle(w["hwnd"])
        edit = None
        for _ in range(2):   # Firefox switches its accessibility tree on at the first query
            if w["process"].lower() == "firefox.exe":
                edit = root.FindFirst(UIA.TreeScope_Descendants,
                                      auto.CreatePropertyCondition(UIA.UIA_AutomationIdPropertyId, "urlbar-input"))
            edit = edit or root.FindFirst(UIA.TreeScope_Descendants,
                                          auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                                       UIA.UIA_EditControlTypeId))
            if edit:
                break
            time.sleep(0.4)
        if not edit:
            return ""
        value = edit.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
    except Exception:
        return ""
    value = (value or "").strip()
    if not value or " " in value:
        return ""                        # the user is typing a search in the address bar
    if not re.match(r"^[a-z][\w+.-]*:", value):
        value = "https://" + value
    return value


# ───────────────────────────── helpers ─────────────────────────────

def _page_text(url: str) -> str:
    from ai_skills import HEADERS, _strip_html
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "html"):
        return ""
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", r.text)
    main = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", html)
    text = _strip_html(main.group(2) if main and len(main.group(2)) > 2000 else html)
    return re.sub(r"\s+", " ", text).strip()


def _focus(w: dict):
    import window_manager
    window_manager._force_foreground(w["hwnd"])
    time.sleep(0.25)


def _hotkey(w: dict, *keys: str):
    """Press a shortcut in that window (pyautogui on Windows; Hyprland sends it straight to the window on Linux)."""
    if IS_WINDOWS:
        import pyautogui
        pyautogui.hotkey(*keys)
    else:
        ld.send_keys(list(keys), w["hwnd"])


def _no_page() -> str:
    return f"Saya tidak melihat halaman browser yang sedang Anda buka, {T}."


# ───────────────────────────── skills ─────────────────────────────

def active_window_info() -> str:
    """What the user is looking at right now: the active window's title and app, and the page address
    if it is a browser tab ('saya sedang buka apa', 'ini halaman apa')."""
    w = current()
    if not w:
        return f"Tidak ada jendela aktif, {T}."
    if is_browser(w):
        url = current_url(w)
        return f"Tab {BROWSERS[w['process'].lower()]}: {page_title(w)}" + (f" ({url})" if url else "")
    return f"Jendela aktif: {w['title']} ({w['process'].removesuffix('.exe')})"


def summarize_current_page(question: str = "") -> str:
    """Summarise (or answer a question about) the web page / article the user has open in the browser:
    'ringkas halaman ini', 'artikel ini tentang apa', 'poin penting dari halaman ini'. question = what
    they want to know (empty = a short summary)."""
    w = current()
    if not is_browser(w):
        import ai_skills
        return ai_skills.analyze_screen(question or "Ringkas isi jendela yang sedang dibuka di layar.")
    url, title = current_url(w), page_title(w)
    text = ""
    if url.startswith("http") and not re.search(r"localhost|127\.0\.0\.1|youtube\.com/watch|youtu\.be", url):
        try:
            text = _page_text(url)
        except requests.RequestException:
            text = ""
    if len(text) < 500:        # login wall, web app, video, local page: read what is on screen instead
        import ai_skills
        _focus(w)
        return ai_skills.analyze_screen(
            (question or "Ringkas isi halaman web yang sedang dibuka") + f" (halaman: {title}). Jawab singkat, bahasa Indonesia.")
    import llm
    ask = question or "Ringkas isi halaman ini dalam 3-5 kalimat, sebut poin terpentingnya."
    try:
        answer = llm.generate(
            f"Judul: {title}\nURL: {url}\n\nIsi halaman:\n{text[:12000]}\n\n"
            f"Permintaan user: {ask}\nJawab dalam bahasa Indonesia yang enak didengar (akan dibacakan), tanpa markdown, "
            f"maksimal 90 kata.", max_tokens=600)
    except Exception as e:
        return f"Maaf {T}, halaman ini belum bisa saya ringkas: {e}"
    return answer.strip()


def close_current_tab() -> str:
    """Close the browser tab the user is looking at ('tutup tab ini')."""
    w = current()
    if not is_browser(w):
        return _no_page()
    title = page_title(w)
    _focus(w)
    _hotkey(w, "ctrl", "w")
    return f"Tab {title[:60]} sudah ditutup, {T}."


MAX_TAB_CYCLE = 40      # tabs checked per browser window when looking for a tab by name


def _browser_windows() -> list[dict]:
    import window_manager
    # never Digidaw's music player window (Chrome or Firefox): cycling its tabs ("tutup tab instagram") would
    # disturb the song. system_control._closable() recognises the player by its own browser profile.
    import system_control
    wins = []
    for w in window_manager._windows():
        if not is_browser(w) or window_manager._is_digidaw(w) or w.get("class") == "DigidawPlayer":
            continue
        try:
            if not system_control._closable(psutil.Process(w["pid"])):
                continue
        except psutil.Error:
            continue
        wins.append(w)
    return wins


def _title_now(w: dict) -> str:
    import window_manager
    live = next((x for x in window_manager._windows() if x["hwnd"] == w["hwnd"]), None)
    return page_title(live) if live else ""


def _find_tab(name: str) -> tuple[dict | None, str]:
    """Bring the browser tab whose title contains `name` to the front (cycling through the tabs of each
    browser window with Ctrl+PageDown). Returns (window, title), or (None, "") when no tab matches."""
    needle = name.lower().strip()
    wins = _browser_windows()
    for w in wins:                                  # the visible tab of some window already matches
        if needle in page_title(w).lower():
            _focus(w)
            return w, page_title(w)
    for w in wins:
        _focus(w)
        start = page_title(w)
        seen = 0
        while seen < MAX_TAB_CYCLE:
            _hotkey(w, "ctrl", "pagedown")
            time.sleep(0.18)
            title = _title_now(w)
            seen += 1
            if needle in title.lower():
                return w, title
            if title == start:
                break                               # went round every tab of this window
    return None, ""


def close_tab(name: str = "") -> str:
    """Close ONE browser tab by (part of) its title, e.g. 'gemini', 'youtube', 'tiktok' ('tutup tab Gemini').
    Never closes the whole browser. Empty name = the tab the user is looking at."""
    if not name.strip() or re.fullmatch(r"(?:ini|itu|sekarang|aktif|yang ini)", name.strip().lower()):
        return close_current_tab()
    w, title = _find_tab(name)
    if not w:
        return f"Tidak ada tab '{name}' yang terbuka, {T}."
    _hotkey(w, "ctrl", "w")
    return f"Tab {title[:60]} sudah ditutup, {T}."


def switch_tab(name: str) -> str:
    """Switch to the browser tab whose title contains `name` ('pindah ke tab YouTube')."""
    w, title = _find_tab(name)
    if not w:
        return f"Tidak ada tab '{name}' yang terbuka, {T}."
    return f"Beralih ke tab {title[:60]}, {T}."


def save_current_link(note: str = "") -> str:
    """Save the address of the page the user has open to their link list ('simpan link ini',
    'bookmark halaman ini'), optionally with a note."""
    w = current()
    url = current_url(w) if is_browser(w) else ""
    if not url:
        return _no_page()
    import system_control
    path = system_control._notes_dir() / "Link Tersimpan.md"
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Link Tersimpan\n\n")
        f.write(f"- {datetime.datetime.now():%d-%m-%Y %H:%M} — [{page_title(w)}]({url})"
                + (f" — {note.strip()}" if note.strip() else "") + "\n")
    return f"Link {page_title(w)[:60]} sudah saya simpan ke daftar Link Tersimpan, {T}."


def copy_current_link() -> str:
    """Copy the address of the page the user has open to the clipboard ('salin link ini')."""
    w = current()
    url = current_url(w) if is_browser(w) else ""
    if not url:
        return _no_page()
    import pyperclip
    pyperclip.copy(url)
    return f"Link {page_title(w)[:60]} sudah disalin, {T}."


def get_selected_text() -> str:
    """The text the user has selected / highlighted in the active window. Use it to explain, translate,
    summarise or read out 'teks yang saya blok / pilih / ini'. Returns the text itself."""
    w = current()
    if not w:
        return "Tidak ada jendela aktif."
    if not IS_WINDOWS:
        selected = ld.clipboard_get(primary=True)      # Wayland's PRIMARY selection = the highlighted text
        if selected.strip():
            return f"Teks yang dipilih user di '{w['title'][:80]}':\n{selected[:6000]}"
    import pyperclip
    try:
        before = pyperclip.paste()
    except Exception:
        before = ""
    marker = f"__digidaw_{time.time()}__"
    pyperclip.copy(marker)
    _focus(w)
    _hotkey(w, "ctrl", "c")
    selected = ""
    for _ in range(10):
        time.sleep(0.1)
        selected = pyperclip.paste()
        if selected != marker:
            break
    try:
        pyperclip.copy(before)
    except Exception:
        pass
    if not selected or selected == marker:
        return "Tidak ada teks yang sedang dipilih. Minta user memblok teksnya dulu."
    return f"Teks yang dipilih user di '{w['title'][:80]}':\n{selected[:6000]}"


NAV_KEYS = {"scroll_down": ("pagedown",), "scroll_up": ("pageup",), "top": ("home",), "bottom": ("end",),
            "back": ("alt", "left"), "forward": ("alt", "right"), "refresh": ("f5",), "new_tab": ("ctrl", "t"),
            "next_tab": ("ctrl", "tab"), "previous_tab": ("ctrl", "shift", "tab"), "reopen_tab": ("ctrl", "shift", "t")}
NAV_WORDS = {"scroll_down": "Scroll ke bawah", "scroll_up": "Scroll ke atas", "top": "Ke paling atas",
             "bottom": "Ke paling bawah", "back": "Kembali ke halaman sebelumnya", "forward": "Maju ke halaman berikutnya",
             "refresh": "Memuat ulang halaman", "new_tab": "Membuka tab baru", "next_tab": "Pindah ke tab berikutnya",
             "previous_tab": "Pindah ke tab sebelumnya", "reopen_tab": "Membuka lagi tab yang baru ditutup"}


def navigate(action: str, times: int = 1) -> str:
    """Navigate the window the user is looking at: action = scroll_down, scroll_up, top, bottom, back,
    forward, refresh, new_tab, next_tab, previous_tab, reopen_tab ('scroll ke bawah', 'kembali',
    'refresh halaman', 'tab baru'). times = how many (e.g. scroll 3 kali)."""
    keys = NAV_KEYS.get(action)
    if not keys:
        return f"Navigasi '{action}' tidak dikenal, {T}."
    w = current()
    if not w:
        return f"Tidak ada jendela aktif, {T}."
    _focus(w)
    for _ in range(max(1, min(int(times or 1), 10))):
        _hotkey(w, *keys)
        time.sleep(0.12)
    return f"{NAV_WORDS[action]}, {T}."


AI_TOOLS = [navigate, active_window_info, summarize_current_page, close_current_tab, close_tab, switch_tab, save_current_link,
            copy_current_link, get_selected_text]
