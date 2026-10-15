"""
Reads out the notifications that arrive on this PC ("cek pesan masuk", "ada notifikasi apa").

Every desktop notification (WhatsApp, Telegram, mail...) is a D-Bus call to org.freedesktop.Notifications. A
listener (busctl in monitor mode) sees those calls, so this works with any notification daemon (swaync, mako,
dunst) without changing it; what the user sees on screen is untouched.

Privacy: messages are kept in memory only (never written to disk), at most 200, and forgotten after 12 hours or
when Digidaw restarts. Only what the app put in the notification is known: if WhatsApp is set to hide message
previews, only "pesan baru" can be read.
"""
import atexit
import collections
import ctypes
import json
import re
import shutil
import signal
import subprocess
import threading
import time

import config

T = config.USER_TITLE
KEEP_SECONDS = 12 * 3600
MAX_KEPT = 200
MAX_READ = 8                    # messages read out in one go (the rest are counted)
MAX_CHARS = 280                 # per message

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
# apps that are chat / mail: "cek pesan masuk" reads these; "cek notifikasi" reads everything
CHAT_APPS = ("whatsapp", "whatsie", "telegram", "signal", "discord", "slack", "messenger", "element", "teams",
             "thunderbird", "geary", "evolution", "mail", "gmail", "skype", "line", "wechat", "instagram", "kdeconnect")
IGNORED_APPS = ("digidaw", "volume", "brightness", "wp-notify", "spotify", "cliphist", "hyprland", "swaync")
LABEL = {"whatsie": "WhatsApp", "whatsapp": "WhatsApp", "telegram": "Telegram", "signal": "Signal",
         "discord": "Discord", "slack": "Slack", "thunderbird": "Thunderbird email", "geary": "email", "mail": "email",
         "gmail": "Gmail", "instagram": "Instagram", "messenger": "Messenger", "kdeconnect": "HP"}

_items: collections.deque = collections.deque(maxlen=MAX_KEPT)    # newest last
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_started = False


def _label(app: str) -> str:
    key = app.lower().strip()
    for name, label in LABEL.items():
        if name in key:
            return label
    return app.strip() or "aplikasi"


def _is_chat(app: str) -> bool:
    key = app.lower()
    return any(c in key for c in CHAT_APPS)


def _clean(text: str) -> str:
    """The text of a notification, as speech: no markup, one line."""
    text = re.sub(r"<[^>]+>", "", text or "")                               # notification markup (<b>, <a href>)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _add(app: str, summary: str, body: str, replaces: int = 0):
    app, summary, body = (app or "").strip(), _clean(summary), _clean(body)
    if not (summary or body) or any(i in app.lower() for i in IGNORED_APPS):
        return
    now = time.time()
    with _lock:
        for item in list(_items)[-12:]:           # the same notification shown twice (an update, a retry)
            if item["app"] == app and item["summary"] == summary and item["body"] == body and now - item["at"] < 4:
                return
        _items.append({"app": app, "summary": summary, "body": body, "at": now, "read": False})


# ───────────────────────────── listener ─────────────────────────────

def _die_with_parent():
    """The listener must not outlive Digidaw (the service keeps child apps alive across restarts)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)           # PR_SET_PDEATHSIG
    except Exception:
        pass


def _handle(line: str):
    try:
        msg = json.loads(line)
    except ValueError:
        return
    if msg.get("member") != "Notify" or msg.get("type") != "method_call":
        return
    data = (msg.get("payload") or {}).get("data") or []
    if len(data) >= 5:
        _add(str(data[0]), str(data[3]), str(data[4]), int(data[1] or 0))


def _listen():
    global _proc
    delay = 2
    while True:
        try:
            _proc = subprocess.Popen(["busctl", "--user", "monitor", "--json=short", "--match", MATCH],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                     text=True, preexec_fn=_die_with_parent)
            started = time.time()
            for line in _proc.stdout:
                _handle(line)
            _proc.wait()
            delay = 2 if time.time() - started > 60 else min(delay * 2, 120)
        except OSError:
            delay = min(delay * 2, 120)
        time.sleep(delay)


def start():
    """Start listening in the background (Linux with busctl; elsewhere nothing happens)."""
    global _started
    if _started or not shutil.which("busctl"):
        return
    _started = True
    threading.Thread(target=_listen, daemon=True, name="notifications").start()
    atexit.register(lambda: _proc and _proc.poll() is None and _proc.terminate())


# ───────────────────────────── reading them out ─────────────────────────────

def _fresh() -> list[dict]:
    cutoff = time.time() - KEEP_SECONDS
    with _lock:
        return [i for i in _items if i["at"] >= cutoff]


def _sentence(item: dict) -> str:
    """'Budi Santoso: Besok jadi ketemu?' — and for a group message 'Sari di Grup Kuliah: …'."""
    who, text = item["summary"], item["body"]
    if _is_chat(item["app"]):
        m = re.match(r"^([^:]{1,40}):\s+(.+)$", text)                 # a group: the title is the group's name
        if m and who and m.group(1) != who:
            return f"Dari {m.group(1)} di {who}: {_trim(m.group(2))}"
        if who and text:
            return f"Dari {who}: {_trim(text)}"
    if who and text:
        return f"{who}: {_trim(text)}"
    return _trim(who or text)


def _trim(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit(" ", 1)[0] + " dan seterusnya"


def read_notifications(app: str = "", messages_only: bool = False, include_read: bool = False) -> str:
    """Read out the notifications that came in on this PC: who sent it and what it says (WhatsApp, Telegram,
    email, any app). Use for "cek pesan masuk", "ada notifikasi apa", "bacakan pesan WhatsApp".
    app: only this app ("whatsapp", "telegram"...); empty = all apps.
    messages_only: only chat and email apps (for "pesan masuk"). include_read: also the ones already read out
    ("baca ulang", "bacakan lagi")."""
    wanted = _label(app).lower() if app.strip() else ""
    items = [i for i in _fresh()
             if (include_read or not i["read"])
             and (not wanted or wanted in _label(i["app"]).lower() or wanted in i["app"].lower())
             and (not messages_only or _is_chat(i["app"]))]
    kind = _label(app) if wanted else ("pesan" if messages_only else "notifikasi")
    if not items:
        earlier = [i for i in _fresh() if i["read"] and (not wanted or wanted in _label(i["app"]).lower())
                   and (not messages_only or _is_chat(i["app"]))]
        if earlier and not include_read:
            return f"Tidak ada {kind} baru, {T}. Mau saya bacakan ulang {len(earlier)} yang tadi?"
        return f"Belum ada {kind} yang masuk, {T}."

    chosen = items[-MAX_READ:]
    by_app: dict[str, list[dict]] = {}
    for item in chosen:
        by_app.setdefault(_label(item["app"]), []).append(item)
    parts = []
    for label, group in by_app.items():
        count = len(group)
        if len(by_app) == 1 and wanted:
            head = f"Ada {count} pesan {label}." if _is_chat(group[0]["app"]) else f"Ada {count} notifikasi {label}."
        else:
            head = f"{label}, {count}:"
        parts.append(head + " " + " ".join(f"{_sentence(i)}." if not _sentence(i).endswith((".", "?", "!")) else _sentence(i)
                                           for i in group))
    with _lock:
        for item in chosen:
            item["read"] = True
    reply = " ".join(parts)
    older = len(items) - len(chosen)
    if older > 0:
        reply += f" Ada {older} yang lebih lama, belum saya bacakan."
    if len(by_app) > 1 or not wanted:
        reply = f"Ada {len(chosen)} {kind} baru, {T}. " + reply
    return reply


def clear_notifications() -> str:
    """Forget the notifications Digidaw has collected (not those on screen)."""
    with _lock:
        n = len(_items)
        _items.clear()
    return f"{n} notifikasi saya lupakan, {T}."


AI_TOOLS = [read_notifications, clear_notifications]
