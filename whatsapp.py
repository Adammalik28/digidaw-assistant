"""
Replying to WhatsApp by voice: after "ada pesan WhatsApp dari Rina" (or reading it), "balas: oke saya otw" sends
that text to Rina, and "kirim WhatsApp ke Budi: nanti saya telepon" to anyone in the chat list. A message never leaves
without the user's "ya" (safety.guard, always asked, whatever the confirmation setting).

WhatsApp runs here as Whatsie (WhatsApp Web inside Qt WebEngine). Digidaw works that page the way a person would,
through Chromium's DevTools protocol: open the chat (from the chat list, else through the search box), check that the
chat that opened really is that person's (its name in the chat header), put the text in the message box, press Enter,
and check the box is empty again (sent). If any step doesn't look right it stops before sending and says so.

DevTools is only on when Whatsie was started with QTWEBENGINE_REMOTE_DEBUGGING (on 127.0.0.1 only). When it wasn't,
Digidaw restarts Whatsie once with it; WhatsApp stays logged in (same profile on disk).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time

import requests

import config

T = config.USER_TITLE
PORT = 9230
APP = "whatsie"
LOAD_WAIT = 25                       # seconds for a restarted Whatsie to show the chat list (a command has 40 s)
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


# ───────────────────────────── reaching Whatsie ─────────────────────────────

def _port_open() -> bool:
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
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


def _restart() -> str:
    """Start Whatsie again with DevTools on. Returns "" or what went wrong."""
    import psutil
    import oscompat
    if not shutil.which(APP):
        return "aplikasi WhatsApp (Whatsie) tidak terpasang di laptop ini"
    running = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "") == APP]
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
    env = dict(oscompat.user_env(), QTWEBENGINE_REMOTE_DEBUGGING=f"127.0.0.1:{PORT}")
    subprocess.Popen([APP], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    deadline = time.time() + LOAD_WAIT
    while time.time() < deadline:
        time.sleep(1)
        if _port_open() and _target():
            tab = _tab()
            if tab:
                try:
                    if tab.js(JS_READY) and tab.js(JS_CHATS_LOADED):
                        time.sleep(1.5)          # the list keeps filling in for a moment after the first rows
                        return ""
                finally:
                    tab.close()
    return "WhatsApp sudah saya buka ulang, tapi daftar chatnya belum muncul"


def _connect():
    """(tab, error). Restarts Whatsie with DevTools when it was started without."""
    if not _port_open() or not _target():
        problem = _restart()
        if problem:
            return None, problem
    tab = _tab()
    if not tab:
        return None, "halaman WhatsApp tidak ditemukan di Whatsie"
    if not tab.js(JS_READY):
        tab.close()
        return None, "WhatsApp belum siap (masih memuat atau belum login)"
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


def _open_chat(tab, name: str) -> str:
    """Open the chat with this name. Returns the chat's full title, or "" when it isn't there."""
    hit = tab.js(JS_FIND_ROW % json.dumps(name))
    if not hit:
        if not tab.js(JS_SEARCH_BOX):
            return ""
        _key(tab, "a", "KeyA", 65, modifiers=2)                      # Ctrl+A: replace whatever was searched before
        _key(tab, "Backspace", "Backspace", 8)
        tab.call("Input.insertText", {"text": name})
        deadline = time.time() + SEARCH_WAIT
        while time.time() < deadline:
            time.sleep(0.5)
            hit = tab.js(JS_FIND_ROW % json.dumps(name))
            if hit:
                break
    if not hit:
        return ""
    _click(tab, hit["x"], hit["y"])
    for _ in range(10):
        time.sleep(0.3)
        if _same(tab.js(JS_HEADER) or "", hit["title"]):
            return hit["title"]
    return ""


def _send(name: str, text: str) -> str:
    """The real sending (after the user said "ya")."""
    tab, problem = _connect()
    if not tab:
        return f"Maaf {T}, pesannya belum terkirim: {problem}."
    try:
        title = _open_chat(tab, name)
        if not title:
            return f"Maaf {T}, chat {name} tidak saya temukan di WhatsApp, jadi pesannya tidak saya kirim."
        try:
            import tts
            tts.prepare(f"Terkirim ke {title}, {T}.")    # said the moment it is sent, not ~1.3 s later
        except Exception:
            pass
        if not tab.js(JS_FOCUS_COMPOSE):
            return f"Maaf {T}, kotak pesan di chat {title} tidak bisa dibuka, pesannya tidak terkirim."
        if (tab.js(JS_COMPOSE_TEXT) or "").strip():
            return f"Maaf {T}, di chat {title} masih ada tulisan yang belum terkirim, jadi saya tidak menimpanya."
        tab.call("Input.insertText", {"text": text})
        time.sleep(0.3)
        typed = (tab.js(JS_COMPOSE_TEXT) or "").strip()
        if re.sub(r"\s+", " ", typed) != re.sub(r"\s+", " ", text.strip()):
            return f"Maaf {T}, teksnya tidak masuk dengan benar ke kotak pesan, jadi belum saya kirim."
        try:
            import brain
            brain._check_cancel()            # the user was already told it took too long: never send after that
        except ImportError:
            pass
        _key(tab, "Enter", "Enter", 13, text="\r")
        for _ in range(10):
            time.sleep(0.3)
            if not (tab.js(JS_COMPOSE_TEXT) or "").strip():
                return f"Terkirim ke {title}, {T}."
        return f"Pesannya sudah saya tulis di chat {title}, tapi belum terlihat terkirim, {T}. Coba cek WhatsApp."
    except Exception as e:
        return f"Maaf {T}, pengiriman ke {name} gagal: {str(e)[:100]}."
    finally:
        tab.close()


# ───────────────────────────── skills ─────────────────────────────

def reply_message(text: str, to: str = "") -> str:
    """Reply on WhatsApp by voice: "balas: oke saya otw" (to the chat the user just heard about), or "kirim WhatsApp ke
    Budi: nanti saya telepon". Always asks the user to confirm before sending.
    text: exactly what to send. to: the person or group; empty = the chat just announced / read out."""
    import notifications
    import safety
    text = (text or "").strip().strip('"').strip()
    if not text:
        return f"Apa isi balasannya, {T}?"
    name = (to or "").strip()
    if not name:
        last = notifications.last_chat()
        if not last:
            return f"Mau dikirim ke siapa, {T}? Sebutkan namanya, misalnya: kirim WhatsApp ke Rina, isinya oke."
        if last["label"] != "WhatsApp":
            return f"Pesan terakhir dari {last['name']} masuk lewat {last['label']}; yang bisa saya balas baru WhatsApp, {T}."
        name = last["name"]
    if not shutil.which(APP):
        return f"Maaf {T}, membalas baru bisa lewat aplikasi WhatsApp Whatsie, dan itu belum terpasang."
    return safety.guard(f'mengirim WhatsApp ke {name}: "{text}"', _send, always=True, name=name, text=text)


# ───────────────────────────── a message in a few steps ─────────────────────────────

COMPOSE_SECONDS = 40                 # the next phrase within this long answers "Ke siapa?" / "Pesannya apa?"
CANCEL_RE = re.compile(r"(?:batal|batalkan|tidak jadi|gak jadi|nggak jadi|ga jadi|enggak jadi|nggak usah|gak usah|"
                       r"tidak usah|jangan|cancel)(?: (?:deh|aja|saja|dulu|ya))*", re.IGNORECASE)
_compose = {"stage": "", "to": "", "at": 0.0}


def compose(to: str = "") -> str:
    """Start a WhatsApp message by voice and ask for what is missing: the person ("kirim pesan WhatsApp" -> "Ke
    siapa?") or the text ("kirim pesan ke Bisnis" -> "Pesannya apa?"). The answer then goes on through
    compose_answer(), and the message is only sent after "ya", like any reply."""
    to = (to or "").strip()
    if to:
        _compose.update(stage="text", to=to, at=time.time())
        return f"Pesan untuk {to}, isinya apa, {T}?"
    _compose.update(stage="to", to="", at=time.time())
    return f"Mau kirim WhatsApp ke siapa, {T}?"


def composing() -> bool:
    """Digidaw just asked "Ke siapa?" / "Pesannya apa?" and waits for the answer."""
    if _compose["stage"] and time.time() - _compose["at"] > COMPOSE_SECONDS:
        _compose.update(stage="", to="")
    return bool(_compose["stage"])


def compose_answer(said: str) -> str:
    """The user's answer to the question compose() asked."""
    said = (said or "").strip().rstrip(".")
    stage, to = _compose["stage"], _compose["to"]
    _compose.update(stage="", to="")
    if CANCEL_RE.fullmatch(said.lower()):
        return f"Baik, tidak jadi dikirim, {T}."
    if stage == "to":
        name = re.sub(r"^(?:ke|kepada|untuk|buat|sama)\s+", "", said, flags=re.IGNORECASE).strip()
        return compose(name) if name else compose()
    return reply_message(said, to)


AI_TOOLS = [reply_message]
