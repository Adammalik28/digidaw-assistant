"""
The YouTube player in Firefox: a separate Firefox window (its own profile, so it never touches the user's normal
Firefox) driven over WebDriver BiDi — the same jobs the Chrome player does over DevTools: open a video, make sure
it plays, skip ads, seek, pause, read what is playing, and reuse one tab for every song.

media.py uses it through the same small interface as its Chrome code: alive(), player_target(), launch(),
new_tab(), Tab(target).js() / .call("Page.navigate" | "Page.bringToFront"), close().
"""
import itertools
import json
import shutil
import socket
import subprocess
import threading
import time

import config
import oscompat

PROFILE_DIR = config.BASE_DIR / "browser_profile_firefox"
APP_ID = "DigidawPlayer"          # Wayland app id / window class, so this window is told apart from normal Firefox
PREFS = {
    "media.autoplay.default": 0,                      # let YouTube start the video without a click
    "media.autoplay.blocking_policy": 0,
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.tabs.warnOnClose": False,
    "browser.translations.automaticallyPopup": False,
}

_lock = threading.RLock()
_conn = {"ws": None, "ids": itertools.count(1)}
_alive_cache = [0.0, False]


def _port() -> int:
    return int(config.MEDIA_DEBUG_PORT)


def available() -> bool:
    return bool(shutil.which("firefox"))


def alive() -> bool:
    """Is the Digidaw Firefox player running? (cached briefly; a socket probe, never blocks)"""
    if time.time() - _alive_cache[0] < 1.5:
        return _alive_cache[1]
    try:
        with socket.create_connection(("127.0.0.1", _port()), timeout=0.25):
            ok = True
    except OSError:
        ok = False
    _alive_cache[:] = [time.time(), ok]
    if not ok:
        _drop()
    return ok


# ───────────────────────────── BiDi session ─────────────────────────────

def _drop():
    ws = _conn["ws"]
    _conn["ws"] = None
    if ws:
        try:
            ws.close()
        except Exception:
            pass


SESSION_BUSY_WAIT = 4.0      # seconds a busy session may block before the player counts as stuck


def _open_ws(path: str):
    from websockets.sync.client import connect
    url = f"ws://127.0.0.1:{_port()}{path}"
    try:
        return connect(url, max_size=None, open_timeout=5, proxy=None)
    except TypeError:
        return connect(url, max_size=None, open_timeout=5)


class SessionLost(RuntimeError):
    """A leftover automation session blocks Firefox (Digidaw was killed mid-command): the player must be restarted."""


def _send(method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    ws = _conn["ws"]
    msg_id = next(_conn["ids"])
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(method)
        msg = json.loads(ws.recv(timeout=remaining))
        if msg.get("id") == msg_id:
            if msg.get("type") == "error":
                raise RuntimeError(f"{msg.get('error')}: {msg.get('message', '')[:120]}")
            return msg.get("result", {})


def _begin():
    """Firefox allows ONE automation session and keeps it even after its connection is gone, and a session can't
    be reconnected. So every command opens its own session and ends it right away (a few ms locally): nothing is
    ever left behind when Digidaw restarts, and another Digidaw process just waits its turn."""
    deadline = time.time() + SESSION_BUSY_WAIT
    while True:
        _conn.update(ws=_open_ws("/session"), ids=itertools.count(1))
        try:
            _send("session.new", {"capabilities": {}}, timeout=5)
            return
        except RuntimeError as e:
            _drop()
            if "Maximum number of active sessions" not in str(e):
                raise
            if time.time() > deadline:
                raise SessionLost(str(e))
            time.sleep(0.3)


def _end():
    try:
        _send("session.end", timeout=2)
    except Exception:
        pass
    _drop()


class _Session:
    """Several commands in one short session (tab ids are only valid inside the session that read them)."""

    def __enter__(self):
        _lock.acquire()
        try:
            _begin()
        except BaseException:
            _lock.release()
            raise
        return self

    def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        return _send(method, params, timeout)

    def youtube_context(self) -> str | None:
        contexts = self.send("browsingContext.getTree", {"maxDepth": 0}).get("contexts", [])
        for marker in ("youtube.com/watch", "youtube.com"):
            for c in contexts:
                if marker in c.get("url", ""):
                    return c["context"]
        return None

    def __exit__(self, *exc):
        try:
            _end()
        finally:
            _lock.release()


def call(method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    """One BiDi command in its own short session."""
    with _Session() as session:
        return session.send(method, params, timeout)


def _pids() -> list[int]:
    import psutil
    found = []
    for p in psutil.process_iter(["cmdline"]):
        if str(PROFILE_DIR) in " ".join(p.info["cmdline"] or []):
            found.append(p.pid)
    return found


def restart_player():
    """Close only Digidaw's own player window (never the user's Firefox)."""
    import psutil
    _drop()
    for pid in _pids():
        try:
            psutil.Process(pid).terminate()
        except psutil.Error:
            pass
    for _ in range(20):
        if not _pids():
            break
        time.sleep(0.25)
    for pid in _pids():
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    _alive_cache[:] = [time.time(), False]


# ───────────────────────────── tabs ─────────────────────────────

def _contexts() -> list[dict]:
    try:
        return call("browsingContext.getTree", {"maxDepth": 0}).get("contexts", [])
    except SessionLost:
        restart_player()          # the next play opens a fresh player
        return []
    except Exception:
        return []


def player_target() -> dict | None:
    """The tab with YouTube in it, as {'id': context, 'url': ...} (like a DevTools target)."""
    tabs = [{"id": c["context"], "url": c.get("url", "")} for c in _contexts()]
    for marker in ("youtube.com/watch", "youtube.com"):
        for t in tabs:
            if marker in t["url"]:
                return t
    return None


def _write_prefs():
    PROFILE_DIR.mkdir(exist_ok=True)
    lines = [f'user_pref("{k}", {json.dumps(v)});' for k, v in PREFS.items()]
    (PROFILE_DIR / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")


def launch(url: str) -> dict:
    if not available():
        raise RuntimeError("Firefox tidak ditemukan")
    _write_prefs()
    subprocess.Popen(["firefox", f"--remote-debugging-port={_port()}", "-profile", str(PROFILE_DIR), "-no-remote",
                      "--name", APP_ID, "--class", APP_ID, "--new-window", url], **oscompat.detached())
    for _ in range(80):                  # up to ~20 s for a cold start
        time.sleep(0.25)
        _alive_cache[0] = 0.0
        if alive():
            target = player_target()
            if target:
                return target
    raise RuntimeError("Firefox player tidak merespons")


def new_tab(url: str) -> dict:
    with _Session() as session:
        context = session.send("browsingContext.create", {"type": "tab"})["context"]
        session.send("browsingContext.navigate", {"context": context, "url": url, "wait": "none"})
    return {"id": "youtube", "url": url}


def close(target: dict):
    try:
        with _Session() as session:
            context = session.youtube_context()
            if context:
                session.send("browsingContext.close", {"context": context})
    except Exception:
        pass


class Tab:
    """Same use as media._Tab: js() runs JavaScript in the YouTube tab; call() understands the two DevTools commands
    media.py uses. The tab is looked up again in every session (its id changes from session to session)."""

    def __init__(self, target: dict | None = None):
        pass

    def js(self, expression: str, await_promise: bool = False):
        # BiDi returns typed "remote values"; JSON keeps the results plain dicts, as with DevTools
        wrapped = (f"(async () => JSON.stringify(await ({expression})))()" if await_promise
                   else f"JSON.stringify({expression})")
        try:
            with _Session() as session:
                context = session.youtube_context()
                if not context:
                    return None
                r = session.send("script.evaluate", {"expression": wrapped, "target": {"context": context},
                                                     "awaitPromise": await_promise, "userActivation": True})
        except Exception:
            return None
        if r.get("type") != "success":
            return None
        value = (r.get("result") or {}).get("value")
        try:
            return json.loads(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def call(self, method: str, params: dict | None = None):
        with _Session() as session:
            context = session.youtube_context()
            if not context:
                return None
            if method == "Page.navigate":
                return session.send("browsingContext.navigate", {"context": context, "url": params["url"], "wait": "none"})
            if method == "Page.bringToFront":
                return session.send("browsingContext.activate", {"context": context})
        raise ValueError(method)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass
