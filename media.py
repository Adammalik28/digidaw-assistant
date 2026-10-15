"""
Media engine for DIGIDAW

1. YouTube player: a dedicated Chrome/Edge window ("DIGIDAW Media") driven through the
   Chrome DevTools Protocol. It is launched with an autoplay policy that needs no click, so
   songs start immediately and can be paused, resumed, skipped, seeked and looped exactly.
2. Media sessions: explicit play / pause / next + "now playing" for ANY app (Spotify, Firefox,
   Chrome, Media Player, ...), not just a toggle key. Windows: SMTC; Linux: MPRIS via playerctl.
3. Ducking: temporarily lowers other apps while DIGIDAW speaks or listens (pycaw / pactl).
"""
import os
import re
import json
import time
import socket
import shutil
import asyncio
import itertools
import threading
import subprocess
import webbrowser
import urllib.parse

import requests

import config
import oscompat
import firefox_player

IS_WINDOWS = oscompat.IS_WINDOWS
if IS_WINDOWS:
    import winreg
else:
    import linux_desktop as ld

T = config.USER_TITLE
CDP_URL = f"http://127.0.0.1:{config.MEDIA_DEBUG_PORT}"
# a Chrome profile can't be shared between Windows and Linux (encrypted cookies), so each OS keeps its own
PROFILE_DIR = config.BASE_DIR / ("browser_profile" if IS_WINDOWS else "browser_profile_linux")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}

# SMTC playback status values
PLAYING, PAUSED = 4, 5


# ───────────────────────────── browser discovery ─────────────────────────────

_BROWSERS = {
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "brave": [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"],
}


def _default_browser_family() -> str | None:
    if not IS_WINDOWS:
        family = ld.default_browser()
        return family if family in _BROWSERS else None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\https\UserChoice") as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
    except OSError:
        return None
    for family, marker in (("chrome", "chrome"), ("edge", "msedge"), ("brave", "brave")):
        if marker in prog_id:
            return family
    return None


def browser_exe() -> str | None:
    """Chromium browser used for the media player and the HUD (config, then default, then any)."""
    order = [config.MEDIA_BROWSER] if config.MEDIA_BROWSER in _BROWSERS else []
    default = _default_browser_family()
    if default and default not in order:
        order.append(default)
    order += [f for f in ("chrome", "edge", "brave", "chromium") if f not in order and f in _BROWSERS]
    for family in order:
        if not IS_WINDOWS:
            command = ld.browser_command(family)
            if command:
                return shutil.which(command)
            continue
        for candidate in _BROWSERS[family]:
            path = os.path.expandvars(candidate)
            if os.path.isfile(path):
                return path
    return None


if not IS_WINDOWS:     # Linux: Chromium browsers are found on PATH (see linux_desktop.BROWSER_COMMANDS)
    _BROWSERS = {family: [] for family in ("chrome", "edge", "brave", "chromium")}


# ───────────────────────── Chrome DevTools Protocol ─────────────────────────

class _Tab:
    """Minimal synchronous DevTools client for one browser tab."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect
        try:
            self._ws = connect(ws_url, max_size=None, open_timeout=5, proxy=None)
        except TypeError:  # older websockets without proxy support
            self._ws = connect(ws_url, max_size=None, open_timeout=5)
        self._ids = itertools.count(1)

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        msg_id = next(self._ids)
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(method)
            msg = json.loads(self._ws.recv(timeout=remaining))
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "CDP error"))
                return msg.get("result", {})

    def js(self, expression: str, await_promise: bool = False):
        """Evaluate JS as if triggered by a real user gesture (so play()/fullscreen are allowed)."""
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "userGesture": True, "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            return None
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_alive_cache = [0.0, False]


def _firefox() -> bool:
    """Play YouTube in the Firefox player? Only when chosen explicitly: remote-controlled Firefox reports
    navigator.webdriver = true, and YouTube then restarts every song after ~45 s. 'auto' uses a Chromium browser."""
    if IS_WINDOWS or not firefox_player.available():
        return False
    return config.MEDIA_BROWSER == "firefox"


def _tab(target: dict):
    return firefox_player.Tab(target) if _firefox() else _Tab(_ws_url(target))


def _alive() -> bool:
    """Is the DIGIDAW media browser running? (A refused connection on Windows can take ~2 s,
    so probe with a short socket timeout and cache the answer briefly.)"""
    if _firefox():
        return firefox_player.alive()
    if time.time() - _alive_cache[0] < 1.5:
        return _alive_cache[1]
    port = int(CDP_URL.rsplit(":", 1)[1])
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            alive = True
    except OSError:
        alive = False
    _alive_cache[:] = [time.time(), alive]
    return alive


def _pages() -> list[dict]:
    try:
        return [t for t in requests.get(CDP_URL + "/json/list", timeout=2).json() if t.get("type") == "page"]
    except (requests.RequestException, ValueError):
        return []


def _ws_url(target: dict) -> str:
    return target.get("webSocketDebuggerUrl") or \
        f"ws://127.0.0.1:{config.MEDIA_DEBUG_PORT}/devtools/page/{target['id']}"


def _player_target() -> dict | None:
    if _firefox():
        return firefox_player.player_target()
    pages = _pages()
    for marker in ("youtube.com/watch", "youtube.com"):
        for t in pages:
            if marker in t.get("url", ""):
                return t
    return None


def _launch(url: str) -> dict:
    if _firefox():
        return firefox_player.launch(url)
    exe = browser_exe()
    if not exe:
        raise RuntimeError("Chrome/Edge tidak ditemukan")
    PROFILE_DIR.mkdir(exist_ok=True)
    subprocess.Popen(
        [exe, f"--remote-debugging-port={config.MEDIA_DEBUG_PORT}", f"--user-data-dir={PROFILE_DIR}",
         "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--no-default-browser-check",
         "--hide-crash-restore-bubble", "--disable-features=Translate", *config.MEDIA_BROWSER_ARGS,
         # Linux: no keyring password prompt for this separate profile
         *([] if IS_WINDOWS else ["--password-store=basic"]),
         "--new-window", url],
        **oscompat.detached(),
    )
    for _ in range(80):  # up to ~20 s for a cold browser start
        time.sleep(0.25)
        _alive_cache[0] = 0.0   # force a fresh probe
        if _alive():
            target = _player_target()
            if target:
                return target
    raise RuntimeError("browser media tidak merespons")


def _new_tab(url: str) -> dict:
    if _firefox():
        return firefox_player.new_tab(url)
    r = requests.put(CDP_URL + "/json/new?" + urllib.parse.quote(url, safe=""), timeout=5)
    r.raise_for_status()
    return r.json()


_STATUS_JS = r"""(() => {
  const v = document.querySelector('video.html5-main-video') || document.querySelector('video');
  if (!v) return {url: location.href, ready: false};
  const p = document.getElementById('movie_player');
  const d = (p && p.getVideoData) ? p.getVideoData() : {};
  return {url: location.href, ready: true, paused: v.paused, ended: v.ended,
          time: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : 0,
          title: d.title || document.title.replace(/ - YouTube$/, ''), author: d.author || '',
          ad: !!document.querySelector('#movie_player.ad-showing'), loop: v.loop};
})()"""

_SETUP_JS = r"""(() => {
  if (window.__digidawTimer) return true;
  window.__digidawTimer = setInterval(() => {
    if (!%s) return;
    const skip = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
    if (skip) skip.click();
  }, 700);
  return true;
})()""" % ("true" if config.YOUTUBE_AUTO_SKIP_ADS else "false")

# the Skip button when there is one; otherwise jump to the end of the ad (also works for unskippable ads)
_SKIP_AD_JS = r"""(() => {
  const skip = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
  if (skip) { skip.click(); return 'button'; }
  const v = document.querySelector('video');
  if (v && isFinite(v.duration)) { v.currentTime = v.duration; return 'seek'; }
  return false;
})()"""
# apps that report "playing" over MPRIS / SMTC without being music (a voice note, a call ringing)
# not music: chat apps and social video feeds (a TikTok tab "plays" all the time) are never "the music" to resume
CHAT_APPS = re.compile(r"whats\s?app|whatsie|telegram|discord|signal|messenger|slack|tiktok|instagram|facebook|"
                       r"reels|shorts", re.IGNORECASE)

_PLAY_JS = "(() => { const v = document.querySelector('video'); if (!v) return false; " \
           "return v.play().then(() => true).catch(() => false); })()"


def _ensure_playing(tab: _Tab, video_id: str, timeout: float = 15) -> dict | None:
    """Wait until the new video is loaded, then make sure it is actually playing."""
    deadline = time.time() + timeout
    info = None
    while time.time() < deadline:
        info = tab.js(_STATUS_JS)
        if info and info.get("ready") and video_id in info.get("url", ""):
            tab.js(_SETUP_JS)
            if info["paused"] and not info["ended"]:
                tab.js(_PLAY_JS, await_promise=True)
            elif info["time"] > 0.3 or info["ad"]:
                return info
        time.sleep(0.4)
    return info


def _open_in_player(url: str, video_id: str, wait: bool = True) -> dict | None:
    if not _firefox() and firefox_player._pids():
        # a Firefox player from before (MEDIA_BROWSER was firefox) still holds the debug port: Chrome can't use it
        firefox_player.restart_player()
        _alive_cache[0] = 0.0
    target = _player_target() if _alive() else None
    if target or _alive():           # (finding the tab may have closed a stuck player: then start a new one)
        target = target or _new_tab(url)
        with _tab(target) as tab:
            if video_id not in target.get("url", "") or "list=RD" in url:
                tab.call("Page.navigate", {"url": url})
            if config.MEDIA_BRING_TO_FRONT:
                tab.call("Page.bringToFront")
            return _ensure_playing(tab, video_id)
    target = _launch(url)
    if not wait:
        threading.Thread(target=_ensure_after_launch, args=(target, video_id), daemon=True).start()
        return None
    with _tab(target) as tab:
        return _ensure_playing(tab, video_id)


def _ensure_after_launch(target: dict, video_id: str):
    try:
        with _tab(target) as tab:
            _ensure_playing(tab, video_id)
    except Exception as e:
        print(f"[media] {e}")


def _yt_js(expression: str, await_promise: bool = False):
    """Run JS in the DIGIDAW YouTube tab. Returns None if the player isn't open."""
    if not _alive():
        return None
    target = _player_target()
    if not target:
        return None
    try:
        with _tab(target) as tab:
            return tab.js(expression, await_promise)
    except Exception:
        return None


def _yt_status() -> dict | None:
    info = _yt_js(_STATUS_JS)
    return info if info and info.get("ready") else None


# ───────────────────────── Windows media sessions (SMTC) ─────────────────────────

# Windows' media-session service can stop answering, and awaiting it then blocks the whole thread
# (asyncio timeouts never fire). So every call runs on a dedicated SMTC thread with a hard timeout;
# a stuck thread is abandoned and SMTC is skipped for a while. The session manager is requested once
# and reused, and code that must never wait (voice loop, HUD) reads a snapshot kept fresh by a poller.

SMTC_TIMEOUT = 3.0
SMTC_RETRY_AFTER = 30.0


class _SmtcWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.manager = None
        threading.Thread(target=self.loop.run_forever, daemon=True, name="smtc-loop").start()

    async def _manager(self):
        if self.manager is None:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
            self.manager = await Manager.request_async()
        return self.manager

    def run(self, fn, timeout: float):
        async def job():
            return await fn(await self._manager())
        return asyncio.run_coroutine_threadsafe(job(), self.loop).result(timeout)


_smtc_worker: _SmtcWorker | None = None
_smtc_down_until = 0.0
_smtc_failures = 0
_smtc_lock = threading.Lock()


def _smtc_call(fn, timeout: float = SMTC_TIMEOUT):
    """Run `async fn(manager)` on the SMTC thread. Returns None if SMTC is unavailable or stuck."""
    global _smtc_worker, _smtc_down_until, _smtc_failures
    if time.time() < _smtc_down_until:
        return None
    with _smtc_lock:
        if _smtc_worker is None:
            _smtc_worker = _SmtcWorker()
        worker = _smtc_worker
    try:
        result = worker.run(fn, timeout)
        _smtc_failures = 0
        return result
    except TimeoutError:
        with _smtc_lock:
            if _smtc_worker is worker:
                _smtc_worker = None   # abandon the stuck thread; a fresh one is made after the pause
        # each stuck call leaves a blocked thread behind, so back off: 30 s, 1 min, 2 min ... 10 min
        _smtc_failures += 1
        _smtc_down_until = time.time() + min(600.0, SMTC_RETRY_AFTER * 2 ** (_smtc_failures - 1))
        return None
    except Exception:
        worker.manager = None
        return None


_app_names: dict[str, str] = {}


def _app_name(aumid: str) -> str:
    """Friendly app name for a media session's AppUserModelID (e.g. 'Firefox', 'Spotify')."""
    if aumid in _app_names:
        return _app_names[aumid]
    import system_control
    start_apps = system_control._start_apps_cache   # only if already loaded (loading takes seconds)
    name = next((a["Name"] for a in start_apps or [] if a.get("AppID") == aumid), None)
    if not name:
        low = aumid.lower()
        for marker, label in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"),
                              ("firefox", "Firefox"), ("brave", "Brave"), ("zune", "Media Player"),
                              ("vlc", "VLC")):
            if marker in low:
                name = label
                break
        if not name and re.fullmatch(r"[0-9A-F]{16}", aumid):
            name = "Firefox"            # Firefox registers its media session under its install hash
    name = name or aumid.split("!")[-1].split(".")[0] or "Media"
    if start_apps is not None:   # don't remember a guess made before the Start Menu list was ready
        _app_names[aumid] = name
    return name


async def _describe(session) -> dict:
    title = artist = ""
    try:
        props = await session.try_get_media_properties_async()
        title, artist = props.title or "", props.artist or ""
    except Exception:
        pass
    info = {"aumid": session.source_app_user_model_id, "title": title, "artist": artist,
            "status": int(session.get_playback_info().playback_status), "position": 0, "duration": 0}
    try:
        tl = session.get_timeline_properties()
        info["position"] = tl.position.total_seconds() if hasattr(tl.position, "total_seconds") else 0
        info["duration"] = tl.end_time.total_seconds() if hasattr(tl.end_time, "total_seconds") else 0
    except Exception:
        pass
    return info


async def _read_sessions(manager) -> list[dict]:
    current = manager.get_current_session()
    cur_id = current.source_app_user_model_id if current else None
    described = [await _describe(s) for s in manager.get_sessions()]
    described.sort(key=lambda d: d["aumid"] != cur_id)
    return described


_session_cache: list = [0.0, None]   # [updated_at, sessions]
_last_active: dict[str, float] = {}  # Linux: player -> last time it was seen playing


def smtc_sessions(timeout: float = SMTC_TIMEOUT) -> list[dict] | None:
    """All media sessions, current one first. None if SMTC / MPRIS isn't available right now."""
    if not IS_WINDOWS:
        sessions = ld.media_sessions(timeout)
        if sessions is not None:
            for s in sessions:
                s["app"] = ld.player_label(s["aumid"])
                if s["status"] == PLAYING:
                    _last_active[s["aumid"]] = time.time()
            # "current" = playing now, else the player that played most recently
            sessions.sort(key=lambda s: (s["status"] != PLAYING, -_last_active.get(s["aumid"], 0)))
            _session_cache[:] = [time.time(), sessions]
        return sessions
    sessions = _smtc_call(_read_sessions, timeout)
    if sessions is not None:
        for s in sessions:
            s["app"] = _app_name(s["aumid"])
        _session_cache[:] = [time.time(), sessions]
    return sessions


def cached_sessions(max_age: float = 6.0) -> list[dict] | None:
    """Last known sessions without waiting (None if unknown / too old)."""
    updated, sessions = _session_cache
    return sessions if sessions is not None and time.time() - updated <= max_age else None


def start_monitor(interval: float = 2.0):
    """Keep the session snapshot fresh in the background."""
    def loop():
        while True:
            smtc_sessions()
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="smtc-poller").start()


def _smtc(action: str, only_status: int | None = None, aumids: list[str] | None = None,
          current_only: bool = False) -> list[str] | None:
    """Apply an action to matching sessions. Returns affected AUMIDs (None = SMTC unavailable)."""
    if not IS_WINDOWS:
        return _mpris(action, only_status, aumids, current_only)

    async def run(manager):
        sessions = manager.get_sessions()
        if current_only:
            current = manager.get_current_session()
            sessions = [current] if current else []
        done = []
        for s in sessions:
            if aumids is not None and s.source_app_user_model_id not in aumids:
                continue
            if only_status is not None and int(s.get_playback_info().playback_status) != only_status:
                continue
            op = {"pause": s.try_pause_async, "play": s.try_play_async, "toggle": s.try_toggle_play_pause_async,
                  "next": s.try_skip_next_async, "previous": s.try_skip_previous_async}[action]
            if await op():
                done.append(s.source_app_user_model_id)
        return done
    return _smtc_call(run)


def _mpris(action: str, only_status: int | None, aumids: list[str] | None, current_only: bool) -> list[str] | None:
    """Linux version of _smtc through playerctl. The 'current' player is the playing one, else the one that
    played last (chat apps such as WhatsApp are never media players here, see linux_desktop.NOT_MEDIA)."""
    sessions = smtc_sessions()
    if sessions is None:
        return None
    if current_only:
        sessions = sessions[:1]
    done = []
    for s in sessions:
        if aumids is not None and s["aumid"] not in aumids:
            continue
        if only_status is not None and s["status"] != only_status:
            continue
        if ld.media_action(s["aumid"], action):
            done.append(s["aumid"])
    return done


def _press_media_key(vk: int):
    if not IS_WINDOWS:
        ld.media_key({0xB0: "next", 0xB1: "previous"}.get(vk, "toggle"))
        return
    import ctypes
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


# ─────────────────────────────── YouTube search ───────────────────────────────

_http = requests.Session()        # keeps the connection to YouTube warm between searches
_search_cache: dict[str, tuple[float, list]] = {}


def _search_youtube(query: str, limit: int = 5) -> list[dict]:
    key = query.lower().strip()
    cached = _search_cache.get(key)
    if cached and time.time() - cached[0] < 6 * 3600:
        return cached[1]
    html = _http.get("https://www.youtube.com/results", params={"search_query": query},
                     headers=HEADERS, timeout=8).text
    results = []
    for m in re.finditer(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html):
        chunk = html[m.end(): m.end() + 5000]
        t = re.search(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', chunk)
        try:
            title = json.loads(f'"{t.group(1)}"') if t else ""
        except ValueError:
            title = ""
        length = re.search(r'"lengthText":\{.*?"simpleText":"([\d:.]+)"', chunk[:3000])
        owner = re.search(r'"(?:ownerText|longBylineText)":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', chunk)
        seconds = 0
        for part in re.split(r"[:.]", length.group(1)) if length else []:
            seconds = seconds * 60 + int(part or 0)
        results.append({"id": m.group(1), "title": title, "seconds": seconds,
                        "channel": owner.group(1) if owner else ""})
        if len(results) >= limit:
            break
    if not results:  # page layout changed: fall back to any video id
        ids = list(dict.fromkeys(re.findall(r'"videoId":"([\w-]{11})"', html)))
        results = [{"id": i, "title": ""} for i in ids[:limit]]
    if results:
        _search_cache[key] = (time.time(), results)
        if len(_search_cache) > 200:
            _search_cache.pop(next(iter(_search_cache)))
    return results


# "putar <x>" is nearly always a song: among the top results prefer what looks like one, unless the user asked
# for a video ("putar video cara masak", "putar podcast …")
VIDEO_WORDS = re.compile(r"\b(?:video|tutorial|cara|podcast|berita|news|film|movie|trailer|vlog|review|kuliah|materi|"
                         r"episode|eps|siaran|live|dokumenter|documentary|ceramah|kajian|resep|gameplay|walkthrough)\b",
                         re.IGNORECASE)
# in a title, "video"/"live" are often part of a song ("Official Video", "Live Lounge"): only these say "not music"
NOT_MUSIC_RE = re.compile(r"\b(?:tutorial|cara|podcast|berita|news|trailer|vlog|review|reaction|kuliah|materi|episode|"
                          r"eps|dokumenter|documentary|ceramah|kajian|resep|gameplay|walkthrough|penjelasan|teori|"
                          r"parodi|kata-kata|motivasi)\b", re.IGNORECASE)
MUSIC_CHANNEL_RE = re.compile(r" - Topic$|vevo|records|music|musik|official", re.IGNORECASE)
SONG_HINT_RE = re.compile(r"official|audio|lyric|lirik|music|musik|\bmv\b|\bft\.?|feat|remix|cover|acoustic|akustik|"
                          r"\blagu\b|\bsong\b|karaoke|sped up|slowed|playlist|lofi|kompilasi|full album", re.IGNORECASE)


def _song_score(r: dict) -> float:
    score = 0.0
    seconds, title = r.get("seconds", 0), r.get("title", "")
    if 90 <= seconds <= 480:
        score += 2
    elif seconds > 900:
        score -= 2                      # a lecture … or a long music compilation (the title tells which)
    elif seconds > 480 or not seconds:
        score -= 1                      # rather long for a song / a live stream
    if SONG_HINT_RE.search(title):
        score += 2
    elif " - " in title:
        score += 1                      # "Artist - Title" (but also "Speaker - Topic")
    if MUSIC_CHANNEL_RE.search(r.get("channel", "")):
        score += 2
    if NOT_MUSIC_RE.search(title):
        score -= 3
    return score


def _looks_musical(r: dict) -> bool:
    return bool((SONG_HINT_RE.search(r.get("title", "")) or MUSIC_CHANNEL_RE.search(r.get("channel", "")))
                and not NOT_MUSIC_RE.search(r.get("title", "")))


def _pick_song(query: str, results: list[dict]) -> dict:
    """The most song-like of the first results (an earlier one wins a near tie: YouTube's order matters).
    When none looks like a song ("konsumen" -> lectures), search once more as "<query> song"."""
    if VIDEO_WORDS.search(query):
        return results[0]
    ranked = lambda rs: max(enumerate(rs), key=lambda ir: _song_score(ir[1]) - ir[0] * 0.5)[1]
    best = ranked(results[:5])
    if any(_looks_musical(r) for r in results[:5]):
        return best
    try:
        more = _search_youtube(f"{query} song")
    except requests.RequestException:
        more = []
    if more and _song_score(ranked(more[:5])) > _song_score(best):
        return ranked(more[:5])
    return best


# ─────────────────────────────── public skills ───────────────────────────────

_last_paused: list[str] = []   # sessions DIGIDAW paused, so "lanjutkan" resumes the right thing
STATE_FILE = config.BASE_DIR / "media_state.json"


def _last_query() -> str:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_query", "")
    except (OSError, ValueError):
        return ""


def _remember_query(query: str):
    try:
        STATE_FILE.write_text(json.dumps({"last_query": query}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def play_youtube(query: str, mix: bool = False) -> str:
    """Play a song or video on YouTube immediately — it auto-plays, no click needed.
    Set mix=True for a continuous YouTube Mix of similar songs (e.g. 'putar playlist lagu galau',
    'putar lagu-lagu Tulus')."""
    try:
        results = _search_youtube(query)
    except requests.RequestException:
        results = []
    if not results:
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
        return f"Saya tidak bisa mengambil hasil YouTube, jadi saya buka pencariannya, {T}."

    video = _pick_song(query, results)
    _remember_query(query)
    url = f"https://www.youtube.com/watch?v={video['id']}" + (f"&list=RD{video['id']}" if mix else "")
    if config.PAUSE_OTHER_MEDIA_ON_PLAY:
        threading.Thread(target=_smtc, args=("pause",), kwargs={"only_status": PLAYING}, daemon=True).start()
    _last_paused.clear()

    title = video["title"] or query
    if _alive():
        # the player is already open: navigate and answer right away; making sure it really
        # plays (and skipping ads) continues in the background
        threading.Thread(target=_play_in_background, args=(url, video["id"], title), daemon=True).start()
        return f"Memutar {title}{' dalam mode mix' if mix else ''}, {T}."
    try:
        info = _open_in_player(url, video["id"], wait=False)
    except Exception as e:
        webbrowser.open(url)
        return f"Membuka {title} di browser biasa, {T} (pemutar otomatis gagal: {e})."
    return f"Memutar {(info or {}).get('title') or title}{' dalam mode mix' if mix else ''}, {T}."


YOUTUBE_ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


def youtube_id(url: str) -> str:
    """The video id in a YouTube link (watch, youtu.be, shorts, music.youtube.com), or ''."""
    m = YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def play_url(url: str) -> str:
    """Play a YouTube link (e.g. shared from the phone) in Digidaw's player, like 'putar …' does."""
    video_id = youtube_id(url)
    if not video_id:
        return f"Itu bukan link YouTube, {T}."
    playlist = re.search(r"[?&]list=([\w-]+)", url)
    url = f"https://www.youtube.com/watch?v={video_id}" + (f"&list={playlist.group(1)}" if playlist else "")
    if config.PAUSE_OTHER_MEDIA_ON_PLAY:
        threading.Thread(target=_smtc, args=("pause",), kwargs={"only_status": PLAYING}, daemon=True).start()
    _last_paused.clear()
    threading.Thread(target=_play_in_background, args=(url, video_id, "video dari HP"), daemon=True).start()
    return f"Memutar video dari HP, {T}."


def _play_in_background(url: str, video_id: str, title: str):
    try:
        info = _open_in_player(url, video_id)
    except Exception as e:
        print(f"[media] pemutar gagal ({e}); membuka {title} di browser biasa")
        webbrowser.open(url)
        return
    if not info or (info.get("paused", True) and not info.get("ad")):
        print(f"[media] {title}: video belum mulai berputar")


def media_control(action: str, seconds: int = 10) -> str:
    """Control music/video playback on the PC (YouTube, Spotify, browsers, any player).
    action: 'pause' (pause/stop what is playing), 'play' (resume / lanjutkan), 'toggle',
    'next' (next song/video), 'previous', 'forward' / 'rewind' (seek by `seconds`),
    'restart' (current video from the beginning), 'fullscreen', 'loop' (repeat current video on/off),
    'skip_ad' (skip the YouTube ad that is playing: 'skip iklan'), 'close' (close the YouTube player window)."""
    action = action.lower().strip()
    action = {"stop": "pause", "jeda": "pause", "resume": "play", "lanjutkan": "play", "putar": "play",
              "skip": "next", "prev": "previous", "back": "previous", "ulang": "restart",
              "maju": "forward", "mundur": "rewind", "skip iklan": "skip_ad", "skip_ads": "skip_ad",
              "lewati iklan": "skip_ad"}.get(action, action)

    if action == "pause":
        paused = _smtc("pause", only_status=PLAYING)
        smtc_ok = paused is not None
        paused = paused or []
        yt = _yt_status()
        if yt and not yt["paused"]:
            _yt_js("document.querySelector('video').pause()")
            paused.append("youtube")
        if not paused and not smtc_ok:
            # media service not answering: use the last snapshot to decide, then the media key
            known = cached_sessions(max_age=30)
            if known is not None and not any(s["status"] == PLAYING for s in known):
                return f"Tidak ada media yang sedang diputar, {T}."
            _press_media_key(0xB3)
            return f"Media di-pause, {T}."
        if not paused:
            return f"Tidak ada media yang sedang diputar, {T}."
        _last_paused[:] = paused
        return f"Media di-pause, {T}."

    if action == "play":
        resumed, smtc_ok = [], True
        if _last_paused:
            result = _smtc("play", only_status=PAUSED, aumids=list(_last_paused))
            smtc_ok = result is not None
            resumed = result or []
            if "youtube" in _last_paused and _yt_js(_PLAY_JS, await_promise=True):
                resumed.append("youtube")
            _last_paused.clear()
        if not resumed:          # Digidaw's own player first: "lanjutkan musik" means the music it was playing
            yt = _yt_status()
            if yt and yt["paused"] and _yt_js(_PLAY_JS, await_promise=True):
                resumed = ["youtube"]
        if not resumed and smtc_ok:
            current = [s for s in (cached_sessions() or []) if s["status"] == PAUSED]
            if not current or not all(CHAT_APPS.search(f"{s.get('title', '')} {s.get('app', '')}") for s in current):
                result = _smtc("play", only_status=PAUSED, current_only=True)   # never a paused TikTok / chat
                smtc_ok = result is not None
                resumed = result or []
        if not resumed and not smtc_ok:
            _press_media_key(0xB3)   # media service not answering: best effort
            return f"Melanjutkan pemutaran, {T}."
        if not resumed:
            playing = [s for s in (cached_sessions() or []) if s["status"] == PLAYING
                       and not CHAT_APPS.search(f"{s.get('title', '')} {s.get('app', '')}")]
            if playing:
                return f"{playing[0]['title'] or 'Media'} sudah sedang diputar, {T}."
            last = _last_query()
            if last:   # nothing paused: pick up the music where the user left off, as a mix
                return play_youtube(last, mix=True)
            return f"Tidak ada media yang bisa dilanjutkan, {T}. Sebutkan lagu yang ingin diputar."
        return f"Melanjutkan pemutaran, {T}."

    if action == "toggle":
        if _smtc("toggle", current_only=True) is None:
            _press_media_key(0xB3)
        return f"Play/pause, {T}."

    if action in ("next", "previous"):
        yt = _yt_status()
        if yt and not yt["paused"]:
            if action == "next":
                _yt_js("document.querySelector('.ytp-next-button') && document.querySelector('.ytp-next-button').click()")
            else:
                _yt_js("history.back()")
        elif not _smtc(action, current_only=True):
            _press_media_key(0xB0 if action == "next" else 0xB1)
        return f"{'Lagu berikutnya' if action == 'next' else 'Lagu sebelumnya'}, {T}."

    yt = _yt_status()
    if not yt:
        return f"Perintah '{action}' hanya untuk pemutar YouTube DIGIDAW, dan pemutarnya belum terbuka, {T}."
    if action == "skip_ad":
        if not yt.get("ad"):
            return f"Tidak ada iklan yang sedang diputar, {T}."
        _yt_js(_SKIP_AD_JS)
        return f"Iklan dilewati, {T}."
    if action in ("forward", "rewind"):
        delta = abs(int(seconds or 10)) * (1 if action == "forward" else -1)
        _yt_js(f"(() => {{ const v = document.querySelector('video'); v.currentTime = Math.max(0, v.currentTime + ({delta})); }})()")
        return f"{'Maju' if delta > 0 else 'Mundur'} {abs(delta)} detik, {T}."
    if action == "restart":
        _yt_js("(() => { const v = document.querySelector('video'); v.currentTime = 0; v.play(); })()")
        return f"Memutar ulang dari awal, {T}."
    if action == "fullscreen":
        _yt_js("document.querySelector('.ytp-fullscreen-button') && document.querySelector('.ytp-fullscreen-button').click()")
        return f"Mode layar penuh diubah, {T}."
    if action == "loop":
        on = _yt_js("(() => { const v = document.querySelector('video'); v.loop = !v.loop; return v.loop; })()")
        return f"Pengulangan video {'aktif' if on else 'nonaktif'}, {T}."
    if action == "close":
        target = _player_target()
        if target and _firefox():
            firefox_player.close(target)
        elif target:
            requests.get(f"{CDP_URL}/json/close/{target['id']}", timeout=3)
        return f"Pemutar YouTube ditutup, {T}."
    return f"Aksi media '{action}' tidak dikenal, {T}."


def is_playing() -> bool:
    """Is any app playing audio/video? Never waits: reads the poller's snapshot. When unknown it
    answers True, so the voice loop stays strict (lyrics must not become commands)."""
    sessions = cached_sessions()
    if sessions is None:
        return True
    return any(s["status"] == PLAYING for s in sessions)


def now_playing() -> str:
    """Tell what song/video is currently playing (title, artist, app)."""
    sessions = smtc_sessions()
    if sessions is None:
        sessions = cached_sessions(max_age=30) or []
    yt = _yt_status()
    playing = [s for s in sessions if s["status"] == PLAYING]
    if yt and not yt["paused"]:
        if yt["ad"]:
            return f"Sedang diputar di YouTube: {yt['title']} oleh {yt['author']}, saat ini iklan sedang tayang, {T}."
        pos = f"{int(yt['time'] // 60)}:{int(yt['time'] % 60):02d}"
        dur = f"{int(yt['duration'] // 60)}:{int(yt['duration'] % 60):02d}" if yt["duration"] else "?"
        return f"Sedang diputar di YouTube: {yt['title']} oleh {yt['author']}, menit {pos} dari {dur}, {T}."
    if playing:
        s = playing[0]
        by = f" oleh {s['artist']}" if s["artist"] else ""
        return f"Sedang diputar di {s['app']}: {s['title']}{by}, {T}."
    paused = [s for s in sessions if s["status"] == PAUSED and s["title"]]
    if paused:
        return f"Tidak ada yang sedang diputar. Terakhir: {paused[0]['title']} di {paused[0]['app']} (di-pause), {T}."
    return f"Tidak ada media yang sedang diputar, {T}."


def snapshot() -> dict:
    """Compact now-playing info for the HUD."""
    yt = _yt_status()
    if yt and not yt["paused"]:
        return {"title": yt["title"], "artist": yt["author"], "app": "YouTube (DIGIDAW)",
                "playing": True, "position": yt["time"], "duration": yt["duration"]}
    sessions = cached_sessions(max_age=10) or []
    for s in sorted(sessions, key=lambda x: x["status"] != PLAYING):
        if s["title"]:
            return {"title": s["title"], "artist": s["artist"], "app": s["app"],
                    "playing": s["status"] == PLAYING, "position": s["position"], "duration": s["duration"]}
    if yt:
        return {"title": yt["title"], "artist": yt["author"], "app": "YouTube (DIGIDAW)",
                "playing": False, "position": yt["time"], "duration": yt["duration"]}
    return {}


# ─────────────────────────────── audio ducking ───────────────────────────────

_duck_lock = threading.Lock()
_duck_depth = 0
_ducked: dict[str, float] = {}


def _com_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def duck():
    """Lower every other app's audio (reference counted; pair with unduck())."""
    global _duck_depth
    if not config.DUCK_MEDIA:
        return
    with _duck_lock:
        _duck_depth += 1
        if _duck_depth > 1:
            return
        if not IS_WINDOWS:
            _ducked.update(ld.duck(config.DUCK_LEVEL))
            return
        _com_init()
        try:
            from pycaw.pycaw import AudioUtilities
            for s in AudioUtilities.GetAllSessions():
                if not s.Process or s.Process.pid == os.getpid() or s.State != 1:
                    continue
                vol = s.SimpleAudioVolume
                original = vol.GetMasterVolume()
                _ducked[s.InstanceIdentifier] = original
                vol.SetMasterVolume(original * config.DUCK_LEVEL, None)
        except Exception:
            pass


def unduck(force: bool = False):
    global _duck_depth
    with _duck_lock:
        _duck_depth = 0 if force else max(0, _duck_depth - 1)
        if _duck_depth or not _ducked:
            return
        if not IS_WINDOWS:
            ld.unduck(dict(_ducked))
            _ducked.clear()
            return
        _com_init()
        try:
            from pycaw.pycaw import AudioUtilities
            for s in AudioUtilities.GetAllSessions():
                original = _ducked.get(s.InstanceIdentifier)
                if original is not None:
                    s.SimpleAudioVolume.SetMasterVolume(original, None)
        except Exception:
            pass
        _ducked.clear()


AI_TOOLS = [play_youtube, media_control, now_playing]
