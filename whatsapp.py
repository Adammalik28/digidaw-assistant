"""
Replying to WhatsApp by voice: after "ada pesan WhatsApp dari Rina" (or reading it), "balas: oke saya otw" sends
that text to Rina, and "kirim WhatsApp ke Budi: nanti saya telepon" to anyone in the chat list. A message never leaves
without the user's "ya" (safety.guard, always asked, whatever the confirmation setting).

Two WhatsApps run on this PC, both WhatsApp Web in an app's own browser engine:
  Whatsie    "whatsapp", the personal number: Qt WebEngine, reached through Chromium's DevTools protocol;
  WasIstLos  "whatsapp bisnis", the business number: WebKitGTK, reached through WebKit's inspector.
"balas ..." goes through the app the message came in on; "kirim whatsapp bisnis ke Rina ..." says which. ("kirim pesan ke
Bisnis" is a chat called that, in the personal one.) The question before sending names the business one.

Digidaw works the page the way a person would: open the chat (from the chat list, else through the search box), check that
the chat that opened really is that person's (its name in the chat header), put the text in the message box, press Enter,
and check the box is empty again (sent). If any step doesn't look right it stops before sending and says so.

The control port is only on when the app was started with it (on 127.0.0.1 only): QTWEBENGINE_REMOTE_DEBUGGING and
WEBKIT_INSPECTOR_HTTP_SERVER. When it wasn't, Digidaw restarts the app once with it, on the workspace it was on; WhatsApp
stays logged in (same profile on disk).
"""
import itertools
import json
import os
import re
import shutil
import socket
import time

import requests

import config

T = config.USER_TITLE
PORT = 9230
APP = "whatsie"
BUSINESS = "bisnis"                  # the account's name in the code and in what the AI passes
BUSINESS_PORT = 9231
BUSINESS_APP = "wasistlos"
LABELS = {"": "WhatsApp", BUSINESS: "WhatsApp Bisnis"}
PROGRAMS = {"": APP, BUSINESS: BUSINESS_APP}
LOAD_WAIT = 25                       # seconds for a restarted app to show the chat list (a command has 40 s)
SEARCH_WAIT = 8                      # seconds for WhatsApp's search to list a chat (longer right after a start)

# the page, read with JavaScript (WhatsApp Web's own markup: ids and roles, never the UI language's words)
JS_READY = "!!document.querySelector('#pane-side')"
# the chat list has rows (right after a start, #pane-side exists seconds before the chats are loaded into it)
JS_CHATS_LOADED = "document.querySelectorAll('#pane-side span[title]').length > 0"
JS_FIND_ROW = r"""
((want) => {
  want = want.toLowerCase();
  const titles = [...document.querySelectorAll('#pane-side span[title]')].filter(s => s.title);
  const hit = titles.find(s => s.title.toLowerCase() === want)
           || titles.find(s => s.title.toLowerCase().startsWith(want))
           || titles.find(s => s.title.toLowerCase().includes(want));
  if (!hit) return null;
  const row = hit.closest('[role="listitem"], [role="row"], [role="gridcell"]') || hit;
  row.scrollIntoView({block: 'center'});
  const r = row.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, title: hit.title};
})(%s)
"""
JS_SEARCH_BOX = r"""
(() => {
  const box = document.querySelector('#side [contenteditable="true"]') || document.querySelector('#side input[type="text"]')
           || document.querySelector('#side input');
  if (!box) return false;
  box.focus();
  return true;
})()
"""
JS_HEADER = r"""
(() => {
  const h = document.querySelector('#main header');
  if (!h) return '';
  const t = h.querySelector('span[title], span[dir="auto"]');
  return (t && (t.title || t.innerText)) || h.innerText || '';
})()
"""
JS_FOCUS_COMPOSE = r"""
(() => {
  const box = document.querySelector('#main footer [contenteditable="true"]');
  if (!box) return false;
  box.focus();
  return true;
})()
"""
JS_COMPOSE_TEXT = r"""
(() => { const b = document.querySelector('#main footer [contenteditable="true"]'); return b ? b.innerText : null; })()
"""
# (WebKit's inspector can't press or click from outside: done from inside the page)
JS_CLICK_AT = r"""
((x, y) => {
  const target = document.elementFromPoint(x, y);
  if (!target) return false;
  for (const kind of ['mouseover', 'mousedown', 'mouseup', 'click'])
    target.dispatchEvent(new MouseEvent(kind, {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0}));
  return true;
})(%s, %s)
"""
JS_CLEAR_FIELD = r"""
(() => {
  const field = document.activeElement;
  if (!field) return false;
  if (field.select) field.select(); else document.execCommand('selectAll');
  document.execCommand('delete');
  return true;
})()
"""
JS_COMPOSE_FOCUSED = """document.activeElement === document.querySelector('#main footer [contenteditable="true"]')"""
JS_INSERT_TEXT = "document.execCommand('insertText', false, %s)"
JS_SEND_BUTTON = r"""
(() => {
  const icon = document.querySelector('#main footer [data-icon*="send"]');
  const button = icon ? (icon.closest('button') || icon.closest('[role="button"]') || icon) : null;
  if (!button) return false;
  button.click();
  return true;
})()
"""


def control_env(program: str) -> dict:
    """What this program has to be started with for Digidaw to reach its page (from this computer only)."""
    if program == APP:
        return {"QTWEBENGINE_REMOTE_DEBUGGING": f"127.0.0.1:{PORT}"}
    if program == BUSINESS_APP:
        return {"WEBKIT_INSPECTOR_HTTP_SERVER": f"127.0.0.1:{BUSINESS_PORT}"}
    return {}


def _account(said: str) -> str:
    return BUSINESS if re.search(r"bisnis|business|wasistlos", (said or "").lower()) else ""


# ───────────────────────────── reaching the apps ─────────────────────────────

def _port_open(port: int = PORT) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _target() -> dict | None:
    try:
        pages = requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=3).json()
    except (requests.RequestException, ValueError):
        return None
    return next((p for p in pages if p.get("type") == "page" and "web.whatsapp.com" in p.get("url", "")), None)


def _tab():
    import media
    target = _target()
    if not target:
        return None
    return media._Tab(target.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{PORT}/devtools/page/{target['id']}")


class _Chromium:
    """What a person does, in Whatsie: Chromium's DevTools press and click from outside the page."""

    def __init__(self, tab):
        self.tab = tab

    def js(self, expression: str):
        return self.tab.js(expression)

    def click(self, x: float, y: float):
        _click(self.tab, x, y)

    def clear_field(self):
        _key(self.tab, "a", "KeyA", 65, modifiers=2)                 # Ctrl+A: replace whatever was searched before
        _key(self.tab, "Backspace", "Backspace", 8)

    def insert(self, text: str):
        self.tab.call("Input.insertText", {"text": text})

    def enter(self):
        _key(self.tab, "Enter", "Enter", 13, text="\r")

    def close(self):
        self.tab.close()


class _WebKitPage:
    """What a person does, in WasIstLos: WebKit's inspector only runs JavaScript in the page, through a target it announces
    on connecting. Enter comes from the compositor: a key press made up inside the page isn't a real one."""

    def __init__(self, port: int = BUSINESS_PORT):
        from websockets.sync.client import connect
        found = re.search(r"/socket/(\d+)/(\d+)/WebPage", requests.get(f"http://127.0.0.1:{port}/", timeout=3).text)      # (WhatsApp's service workers are listed too, first)
        if not found:
            raise ConnectionError("tidak ada halaman di inspector")
        self._ws = connect(f"ws://127.0.0.1:{port}{found[0]}", max_size=None, open_timeout=5, proxy=None)
        self._ids = itertools.count(1)
        self._page = ""
        deadline = time.time() + 5
        while not self._page and time.time() < deadline:
            self._note(json.loads(self._ws.recv(timeout=max(0.1, deadline - time.time()))))
        if not self._page:
            self.close()
            raise ConnectionError("inspector tidak menyebut halamannya")

    def _note(self, message: dict):
        """The page's target, also when the page swaps it while navigating."""
        params = message.get("params") or {}
        if message.get("method") == "Target.targetCreated":
            self._page = params["targetInfo"]["targetId"]
        elif message.get("method") == "Target.didCommitProvisionalTarget":
            self._page = params["newTargetId"]

    def js(self, expression: str, timeout: float = 10.0):
        inner = next(self._ids)
        self._ws.send(json.dumps({"id": next(self._ids), "method": "Target.sendMessageToTarget", "params": {
            "targetId": self._page, "message": json.dumps({"id": inner, "method": "Runtime.evaluate", "params": {
                "expression": expression, "returnByValue": True, "emulateUserGesture": True}})}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("inspector WebKit tidak menjawab")
            message = json.loads(self._ws.recv(timeout=remaining))
            if message.get("method") != "Target.dispatchMessageFromTarget":
                self._note(message)
                continue
            answer = json.loads(message["params"]["message"])
            if answer.get("id") == inner:
                result = answer.get("result") or {}
                return None if result.get("wasThrown") or "error" in answer else (result.get("result") or {}).get("value")

    def click(self, x: float, y: float):
        self.js(JS_CLICK_AT % (json.dumps(x), json.dumps(y)))

    def clear_field(self):
        self.js(JS_CLEAR_FIELD)

    def insert(self, text: str):
        self.js(JS_INSERT_TEXT % json.dumps(text))

    def enter(self):
        import linux_desktop as ld
        import window_manager
        window = window_manager.find_app(BUSINESS_APP)
        self.js(JS_FOCUS_COMPOSE)
        if window and self.js(JS_COMPOSE_FOCUSED):       # (Enter anywhere else does something else: in the search box it opens the first chat)
            ld.send_keys(["enter"], window["hwnd"])
            for _ in range(5):
                time.sleep(0.3)
                if not (self.js(JS_COMPOSE_TEXT) or "").strip():
                    return
        self.js(JS_SEND_BUTTON)          # (only while the text is still there: it can't go twice)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _page(account: str = ""):
    """A connection to this account's page, or None."""
    if account != BUSINESS:
        return _tab()
    try:
        return _WebKitPage()
    except (requests.RequestException, OSError, TimeoutError, ValueError, KeyError, ConnectionError) as e:
        print(f"[WhatsApp Bisnis] {str(e)[:100]}")
        return None


def _reachable(account: str = "") -> bool:
    if account == BUSINESS:
        return _port_open(BUSINESS_PORT)
    return _port_open() and bool(_target())


def _restart(account: str = "") -> str:
    """Start the app again with its control port on, on the workspace it was on. Returns "" or what went wrong."""
    import linux_desktop as ld
    import psutil
    import window_manager
    program, label = PROGRAMS[account], LABELS[account]
    if not shutil.which(program):
        return f"aplikasi {label} ({program}) tidak terpasang di laptop ini"
    window = window_manager.find_app(program)
    place = str(window["workspace"]) if window and not str(window["workspace"]).startswith("special:") else ""
    running = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "") == program]
    for p in running:
        try:
            p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(running, timeout=6)
    for p in running:
        if p.is_running():
            try:
                p.kill()
            except psutil.Error:
                pass
    with ld.quiet_opening(place):
        ld.spawn([program])
    deadline = time.time() + LOAD_WAIT
    while time.time() < deadline:
        time.sleep(1)
        if _reachable(account):
            tab = _page(account)
            if tab:
                try:
                    if tab.js(JS_READY) and tab.js(JS_CHATS_LOADED):
                        time.sleep(1.5)          # the list keeps filling in for a moment after the first rows
                        return ""
                except (OSError, TimeoutError):
                    pass
                finally:
                    tab.close()
    return f"{label} sudah saya buka ulang, tapi daftar chatnya belum muncul"


def _connect(account: str = ""):
    """(tab, error). Restarts the app with its control port when it was started without."""
    if not _reachable(account):
        problem = _restart(account)
        if problem:
            return None, problem
    tab = _page(account)
    if not tab:
        return None, f"halaman {LABELS[account]} tidak ditemukan"
    if not tab.js(JS_READY):
        tab.close()
        return None, f"{LABELS[account]} belum siap (masih memuat atau belum login)"
    return tab, ""


# ───────────────────────────── doing it like a person ─────────────────────────────

def _click(tab, x: float, y: float):
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        params = {"type": kind, "x": x, "y": y}
        if kind != "mouseMoved":
            params.update(button="left", clickCount=1)
        tab.call("Input.dispatchMouseEvent", params)


def _key(tab, key: str, code: str, vk: int, modifiers: int = 0, text: str = ""):
    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            "modifiers": modifiers}
    if text:
        down["text"] = text
    tab.call("Input.dispatchKeyEvent", down)
    tab.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                        "nativeVirtualKeyCode": vk, "modifiers": modifiers})


def _same(header: str, name: str) -> bool:
    a, b = re.sub(r"\s+", " ", header).strip().lower(), re.sub(r"\s+", " ", name).strip().lower()
    return bool(a) and (a == b or a.startswith(b) or b in a)


def _open_chat(page, name: str) -> str:
    """Open the chat with this name. Returns the chat's full title, or "" when it isn't there."""
    hit = page.js(JS_FIND_ROW % json.dumps(name))
    if not hit:
        if not page.js(JS_SEARCH_BOX):
            return ""
        page.clear_field()
        page.insert(name)
        deadline = time.time() + SEARCH_WAIT
        while time.time() < deadline:
            time.sleep(0.5)
            hit = page.js(JS_FIND_ROW % json.dumps(name))
            if hit:
                break
    if not hit:
        return ""
    page.click(hit["x"], hit["y"])
    for _ in range(10):
        time.sleep(0.3)
        if _same(page.js(JS_HEADER) or "", hit["title"]):
            return hit["title"]
    return ""


def _send(name: str, text: str, account: str = "") -> str:
    """The real sending (after the user said "ya")."""
    label = LABELS[account]
    tab, problem = _connect(account) if account else _connect()
    if not tab:
        return f"Maaf {T}, pesannya belum terkirim: {problem}."
    page = tab if isinstance(tab, _WebKitPage) else _Chromium(tab)
    where = f" di {label}" if account else ""
    try:
        title = _open_chat(page, name)
        if not title:
            return f"Maaf {T}, chat {name} tidak saya temukan di {label}, jadi pesannya tidak saya kirim."
        try:
            import tts
            tts.prepare(f"Terkirim ke {title}{where}, {T}.")    # said the moment it is sent, not ~1.3 s later
        except Exception:
            pass
        if not page.js(JS_FOCUS_COMPOSE):
            return f"Maaf {T}, kotak pesan di chat {title} tidak bisa dibuka, pesannya tidak terkirim."
        if (page.js(JS_COMPOSE_TEXT) or "").strip():
            return f"Maaf {T}, di chat {title} masih ada tulisan yang belum terkirim, jadi saya tidak menimpanya."
        page.insert(text)
        time.sleep(0.3)
        typed = (page.js(JS_COMPOSE_TEXT) or "").strip()
        if re.sub(r"\s+", " ", typed) != re.sub(r"\s+", " ", text.strip()):
            return f"Maaf {T}, teksnya tidak masuk dengan benar ke kotak pesan, jadi belum saya kirim."
        try:
            import brain
            brain._check_cancel()            # the user was already told it took too long: never send after that
        except ImportError:
            pass
        page.enter()
        for _ in range(10):
            time.sleep(0.3)
            if not (page.js(JS_COMPOSE_TEXT) or "").strip():
                return f"Terkirim ke {title}{where}, {T}."
        return f"Pesannya sudah saya tulis di chat {title}, tapi belum terlihat terkirim, {T}. Coba cek {label}."
    except Exception as e:
        return f"Maaf {T}, pengiriman ke {name} gagal: {str(e)[:100]}."
    finally:
        page.close()


# ───────────────────────────── skills ─────────────────────────────

def reply_message(text: str, to: str = "", account: str = "") -> str:
    """Reply on WhatsApp by voice: "balas: oke saya otw" (to the chat the user just heard about), or "kirim WhatsApp ke
    Budi: nanti saya telepon". Always asks the user to confirm before sending.
    text: exactly what to send. to: the person or group; empty = the chat just announced / read out.
    account: "bisnis" only when the user says WhatsApp bisnis / WA bisnis (the WasIstLos app); empty = the app the chat's
    last message came in on, else the personal WhatsApp."""
    import notifications
    import safety
    text = (text or "").strip().strip('"').strip()
    if not text:
        return f"Apa isi balasannya, {T}?"
    account = _account(account)
    name = (to or "").strip()
    if not name:
        last = notifications.last_chat()
        if not last:
            return f"Mau dikirim ke siapa, {T}? Sebutkan namanya, misalnya: kirim WhatsApp ke Rina, isinya oke."
        if last["label"] not in LABELS.values():
            return f"Pesan terakhir dari {last['name']} masuk lewat {last['label']}; yang bisa saya balas baru WhatsApp, {T}."
        name = last["name"]
        account = account or _account(last["label"])
    elif not account:
        account = _account(notifications.app_of_chat(name))
    if not shutil.which(PROGRAMS[account]):
        return f"Maaf {T}, membalas lewat {LABELS[account]} butuh aplikasi {PROGRAMS[account]}, dan itu belum terpasang."
    return safety.guard(f'mengirim {LABELS[account]} ke {name}: "{text}"', _send, always=True, name=name, text=text,
                        **({"account": account} if account else {}))


# ───────────────────────────── a message in a few steps ─────────────────────────────

COMPOSE_SECONDS = 40                 # the next phrase within this long answers "Ke siapa?" / "Pesannya apa?"
CANCEL_RE = re.compile(r"(?:batal|batalkan|tidak jadi|gak jadi|nggak jadi|ga jadi|enggak jadi|nggak usah|gak usah|"
                       r"tidak usah|jangan|cancel)(?: (?:deh|aja|saja|dulu|ya))*", re.IGNORECASE)
_compose = {"stage": "", "to": "", "at": 0.0, "account": ""}


NOT_A_PERSON = {"whatsapp", "wa", "whatsie", "telegram", "pesan", "chat", "hp", "dia"}     # "kirim pesan ke WhatsApp": to whom?


def stop_composing():
    _compose.update(stage="", to="", account="")


def compose(to: str = "", account: str = "") -> str:
    """Start a WhatsApp message by voice and ask for what is missing: the person ("kirim pesan WhatsApp" -> "Ke
    siapa?") or the text ("kirim pesan ke Bisnis" -> "Pesannya apa?"). The answer then goes on through
    compose_answer(), and the message is only sent after "ya", like any reply."""
    to = (to or "").strip()
    if to and to.lower() not in NOT_A_PERSON:
        _compose.update(stage="text", to=to, at=time.time(), account=account)
        return f"Pesan untuk {to}, isinya apa, {T}?"
    _compose.update(stage="to", to="", at=time.time(), account=account)
    return f"Mau kirim {LABELS[account]} ke siapa, {T}?"


def composing() -> bool:
    """Digidaw just asked "Ke siapa?" / "Pesannya apa?" and waits for the answer."""
    if _compose["stage"] and time.time() - _compose["at"] > COMPOSE_SECONDS:
        stop_composing()
    return bool(_compose["stage"])


def compose_answer(said: str) -> str:
    """The user's answer to the question compose() asked."""
    said = (said or "").strip().rstrip(".")
    stage, to, account = _compose["stage"], _compose["to"], _compose["account"]
    stop_composing()
    if CANCEL_RE.fullmatch(said.lower()):
        return f"Baik, tidak jadi dikirim, {T}."
    if stage == "to":
        name = re.sub(r"^(?:ke|kepada|untuk|buat|sama)\s+", "", said, flags=re.IGNORECASE).strip()
        return compose(name, account) if name else compose("", account)
    if re.fullmatch(r"(?:ya|iya|iyah|hah|apa)", said.lower()):       # answering the question, not dictating
        _compose.update(stage="text", to=to, at=time.time(), account=account)
        return f"Isi pesannya apa, {T}?"
    return reply_message(said, to, **({"account": account} if account else {}))


AI_TOOLS = [reply_message]
