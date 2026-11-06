"""
Reads out the notifications that arrive on this PC ("cek pesan masuk", "ada notifikasi apa"), and says when a
message comes in, like a butler at the door: "Tuan, ada pesan WhatsApp dari Rina. Mau saya bacakan?" ("ya" reads it).

Every desktop notification (WhatsApp, Telegram, mail...) is a D-Bus call to org.freedesktop.Notifications. A
listener (busctl in monitor mode) sees those calls, so this works with any notification daemon (swaync, mako,
dunst) without changing it; what the user sees on screen is untouched.

Saying it out loud (config.ANNOUNCE_MESSAGES): only who sent it, never the text unless asked; personal chats only by
default (groups are chatty). Not while the user is away (they hear a summary when back), has Do Not Disturb on,
plays something fullscreen, already looks at that chat app, or is talking with Digidaw (it waits for a pause).

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
import diag
import storage

T = config.USER_TITLE
UNREAD_RE = re.compile(r"\((\d{1,4})\)")
KEEP_SECONDS = 12 * 3600
MAX_KEPT = 200
MAX_READ = 8                    # messages read out in one go (the rest are counted)
MAX_CHARS = 280                 # per message

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
# apps that are chat / mail: "cek pesan masuk" reads these; "cek notifikasi" reads everything
CHAT_APPS = ("whatsapp", "whatsie", "wasistlos", "telegram", "signal", "discord", "slack", "messenger", "element", "teams",
             "thunderbird", "geary", "evolution", "mail", "gmail", "skype", "line", "wechat", "instagram", "kdeconnect")
IGNORED_APPS = ("digidaw", "volume", "brightness", "wp-notify", "spotify", "cliphist", "hyprland", "swaync")
# (the first match counts: "whatsapp bisnis" before "whatsapp")
LABEL = {"wasistlos": "WhatsApp Bisnis", "bisnis": "WhatsApp Bisnis", "business": "WhatsApp Bisnis",
         "whatsie": "WhatsApp", "whatsapp": "WhatsApp", "telegram": "Telegram", "signal": "Signal",
         "discord": "Discord", "slack": "Slack", "thunderbird": "Thunderbird email", "geary": "email", "mail": "email",
         "gmail": "Gmail", "instagram": "Instagram", "messenger": "Messenger", "kdeconnect": "HP"}

ANNOUNCE = None                 # set by main (core.announce): new messages are said out loud
GATHER_SECONDS = 4              # messages arriving together are announced together
PERSON_GAP = 5 * 60             # the same chat is announced again only after this long...
GROUP_GAP = 15 * 60             # ...a group even less often
WAIT_FOR_PAUSE = 60             # the user is talking with Digidaw: wait this long for a pause, then stay quiet
OFFER_SECONDS = 45              # "Mau saya bacakan?" can be answered with a plain "ya" for this long

_items: collections.deque = collections.deque(maxlen=MAX_KEPT)    # newest last
_kept_file = None               # set by start: what came in stays known after a restart, for KEEP_SECONDS. (Tests and other processes never write it.)
_keep_timer = None
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_started = False
_waiting: list[dict] = []       # new messages not announced yet
_timer: threading.Timer | None = None
_announcing = threading.Lock()  # one announcement at a time
_told: dict[str, float] = {}    # chat -> when it was last announced
_offer = {"until": 0.0, "senders": ""}
_last_chat = {"name": "", "app": "", "at": 0.0}   # the chat the user last heard about ("balas: …" goes there)


def _label(app: str) -> str:
    key = app.lower().strip()
    for name, label in LABEL.items():
        if name in key:
            return label
    return app.strip() or "aplikasi"


def _is_chat(app: str) -> bool:
    key = app.lower()
    return any(c in key for c in CHAT_APPS)


def note_chat(item: dict):
    """The user just heard about this chat (announced or read out): a "balas …" now answers it."""
    if _is_chat(item.get("app", "")):
        _last_chat.update(name=_chat(item)[0], app=item["app"], at=time.time())


def known_chats() -> list[str]:
    """The chats messages came from lately, newest first ("Toko Buku", "Budi"): where a spoken name ends in
    "kirim pesan ke bisnis oke saya datang"."""
    names: list[str] = []
    if _last_chat["name"]:
        names.append(_last_chat["name"])
    for item in reversed(list(_items)):
        if _is_chat(item["app"]):
            who = _chat(item)[0]
            if who and who not in names:
                names.append(who)
    return names


def app_of_chat(name: str) -> str:
    """The app this chat's latest message came in on ("" when none did lately): "balas ke Rina" goes back the same way."""
    wanted = name.strip().lower()
    for item in reversed(list(_items)):
        if wanted and _is_chat(item["app"]) and wanted in _chat(item)[0].lower():
            return item["app"]
    return ""


def last_chat(max_age: float = 15 * 60) -> dict | None:
    """{"name", "app", "label"} of the chat the user last heard about, if that was recently."""
    if _last_chat["name"] and time.time() - _last_chat["at"] <= max_age:
        return {"name": _last_chat["name"], "app": _last_chat["app"], "label": _label(_last_chat["app"])}
    return None


# direction marks and zero-width characters chat apps put around names ("sari\u200e"): invisible, but they break
# matching a name and show up as odd boxes in the log
INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def _clean(text: str) -> str:
    """The text of a notification, as speech: no markup, one line."""
    text = re.sub(r"<[^>]+>", "", text or "")                               # notification markup (<b>, <a href>)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = INVISIBLE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_hooks: list = []               # fn(item) -> True when it announced the message itself (watchers.py)
# texts of messages read out loud: the log file leaves them out (main._LogFile); the HUD still shows them
PRIVATE_TEXTS: collections.deque = collections.deque(maxlen=60)


def on_new(fn):
    """fn(item) runs for every new notification; when it returns True the usual "ada pesan dari …" is skipped."""
    if fn not in _hooks:
        _hooks.append(fn)


def _keep_soon():
    """Saved a moment later: messages come in bunches."""
    global _keep_timer
    if _kept_file is None or (_keep_timer is not None and _keep_timer.is_alive()):
        return
    _keep_timer = threading.Timer(2.0, _keep_now)
    _keep_timer.daemon = True
    _keep_timer.start()


def _keep_now():
    if _kept_file is None:
        return
    try:
        with _lock:
            items = [dict(i) for i in _items]
        storage.save_json(_kept_file, items, mode=storage.PRIVATE)
    except OSError as e:
        diag.warn_once("notifications.keep", "pesan masuk tidak bisa disimpan", e)


def _load_kept(path):
    """What came in before the restart (the private logs folder, gone after KEEP_SECONDS)."""
    global _kept_file
    _kept_file = path
    cutoff = time.time() - KEEP_SECONDS
    with _lock:
        for item in storage.load_json(path, []):
            if isinstance(item, dict) and item.get("at", 0) >= cutoff and {"app", "summary", "body", "read"} <= set(item):
                _items.append(item)


def _unread_in_titles(wanted: str = "") -> list[tuple[str, int]]:
    """(chat app, its own unread count): WhatsApp's window says "(26)" while its notifications may never have reached Digidaw."""
    import window_manager
    found: dict[str, int] = {}
    try:
        windows = window_manager._windows()
    except Exception:
        return []
    for w in windows:
        app = f"{w.get('class', '')} {w.get('process', '')}"
        count = UNREAD_RE.search(w.get("title", ""))
        if count and _is_chat(app):
            label = _label(app)
            if not wanted or wanted in label.lower():
                found[label] = max(found.get(label, 0), int(count[1]))
    return list(found.items())


def _add(app: str, summary: str, body: str, replaces: int = 0):
    app, summary, body = (app or "").strip(), _clean(summary), _clean(body)
    if not (summary or body) or any(i in app.lower() for i in IGNORED_APPS):
        return
    now = time.time()
    with _lock:
        for item in list(_items)[-12:]:           # the same notification shown twice (an update, a retry)
            if item["app"] == app and item["summary"] == summary and item["body"] == body and now - item["at"] < 4:
                return
        item = {"app": app, "summary": summary, "body": body, "at": now, "read": False}
        _items.append(item)
    _keep_soon()
    taken = False
    for fn in list(_hooks):
        try:
            taken = bool(fn(item)) or taken
        except Exception as e:
            diag.warn_once("notifications.hook", "pemantau pesan (kabari saya kalau …) gagal", e)
    announce = not taken and _is_chat(app) and ANNOUNCE is not None and config.ANNOUNCE_MESSAGES != "off"
    if announce:
        with _lock:
            _waiting.append(item)
        _schedule(GATHER_SECONDS)


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
    from pathlib import Path
    _load_kept(Path(config.LOG_DIR) / "notifications.json")
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
    text = text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit(" ", 1)[0] + " dan seterusnya"
    if len(text) >= 4 and text not in PRIVATE_TEXTS:
        PRIVATE_TEXTS.append(text)          # someone's message: kept out of the log
    return text


def _from(item: dict, names: list[str]) -> bool:
    """Is the notification from one of these people or groups (the chat, or the sender inside a group)?"""
    if not names:
        return True
    m = re.match(r"^([^:]{1,40}):\s", item["body"])
    who = f"{item['summary']} | {m.group(1) if m else ''}".lower()
    return any(n in who for n in names)


def read_notifications(app: str = "", messages_only: bool = False, include_read: bool = False, sender: str = "") -> str:
    """Read out the notifications that came in on this PC: who sent it and what it says (WhatsApp, Telegram,
    email, any app). Use for "cek pesan masuk", "ada notifikasi apa", "bacakan pesan WhatsApp".
    app: only this app ("whatsapp", "telegram"...); empty = all apps.
    messages_only: only chat and email apps (for "pesan masuk"). include_read: also the ones already read out
    ("baca ulang", "bacakan lagi"). sender: only from this person or group ("Rina", "Kelas TI"; several:
    "Rina, Budi"), for "bacakan pesan dari Rina"."""
    said = _reading(app, messages_only, include_read, sender)
    _keep_soon()
    return said


def _reading(app: str = "", messages_only: bool = False, include_read: bool = False, sender: str = "",
             mark: bool = True) -> str:
    """read_notifications' answer; mark=False only words it (the voice gets ready before "ya" is said)."""
    wanted = _label(app).lower() if app.strip() else ""
    names = [n.strip().lower() for n in sender.split(",") if n.strip()]
    items = [i for i in _fresh()
             if (include_read or not i["read"])
             and (not wanted or wanted in _label(i["app"]).lower() or wanted in i["app"].lower())
             and (not messages_only or _is_chat(i["app"])) and _from(i, names)]
    kind = _label(app) if wanted else ("pesan" if messages_only else "notifikasi")
    if names and not items:
        return f"Tidak ada {kind} baru dari {sender.strip()}, {T}."
    if not items:
        earlier = [i for i in _fresh() if i["read"] and (not wanted or wanted in _label(i["app"]).lower())
                   and (not messages_only or _is_chat(i["app"]))]
        if earlier and not include_read:
            return f"Tidak ada {kind} baru, {T}. Mau saya bacakan ulang {len(earlier)} yang tadi?"
        marked = _unread_in_titles(wanted) if messages_only or wanted else []
        if marked:
            counts = ", ".join(f"{label} menandai {count} belum dibaca" for label, count in marked)
            return f"Belum ada {kind} baru yang sampai ke saya, {T}, tetapi {counts}. Ucapkan buka {marked[0][0]} untuk melihatnya."
        return f"Belum ada {kind} yang masuk, {T}."

    chosen = items[-MAX_READ:]
    if names:                  # "bacakan pesan dari Rina": just what she wrote ("Dari Rina: …? Lalu, …")
        said, last = [], ""
        for item in chosen:
            head, _, text = _sentence(item).partition(": ")
            line = f"Lalu, {text}" if head == last and text else f"{head}: {text}" if text else head
            said.append(line if line.endswith((".", "?", "!")) else line + ".")
            last = head
        if mark:
            with _lock:
                for item in chosen:
                    item["read"] = True
            note_chat(chosen[-1])
        older = len(items) - len(chosen)
        return " ".join(said) + (f" Ada {older} yang lebih lama, belum saya bacakan." if older > 0 else "")
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
    if mark:
        with _lock:
            for item in chosen:
                item["read"] = True
        note_chat(chosen[-1])
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
    _keep_soon()
    return f"{n} notifikasi saya lupakan, {T}."


# ───────────────────────────── saying it when it comes in ─────────────────────────────

def _chat(item: dict) -> tuple[str, bool]:
    """(chat, is_group). A group's notification has the group as its title and "Sari: halo" as its text (WhatsApp,
    Telegram); Discord puts the channel in the title: "Budi (#umum, Server)"."""
    who, text = item["summary"], item["body"]
    m = re.match(r"^([^:]{1,40}):\s+(.+)$", text)
    if m and who and m.group(1) != who:
        return who, True
    if re.search(r"\(#[^)]*\)$", who):
        return who, True
    return who or _label(item["app"]), False


def _name(item: dict) -> str:
    who, group = _chat(item)
    return f"grup {who}" if group and not re.match(r"(?:grup|group|gc)\b", who, re.IGNORECASE) else who


def _join(names: list[str], most: int = 3) -> str:
    """'Rina', 'Rina dan Budi', 'Rina, Budi, dan grup Kelas TI', 'Rina, Budi, dan 3 obrolan lain'."""
    if len(names) > most:
        names = names[:most - 1] + [f"{len(names) - most + 1} obrolan lain"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + (", dan " if len(names) > 2 else " dan ") + names[-1]


def announcement(items: list[dict]) -> str:
    """'Tuan, ada pesan WhatsApp dari Rina.' / 'Tuan, Rina mengirim 3 pesan di WhatsApp.' /
    'Tuan, ada 4 pesan baru, dari Rina, Budi, dan grup Kelas TI.' — who, never what (that only when asked)."""
    names = [_name(i) for i in items]
    chats = list(dict.fromkeys(names))
    if len(chats) > 1:
        return f"{T}, ada {len(items)} pesan baru, dari {_join(chats)}."
    label, group = _label(items[0]["app"]), _chat(items[0])[1]
    if group:
        return f"{T}, ada {len(items)} pesan baru di {chats[0]}." if len(items) > 1 else f"{T}, ada pesan baru di {chats[0]}."
    return f"{T}, {chats[0]} mengirim {len(items)} pesan di {label}." if len(items) > 1 else \
        f"{T}, ada pesan {label} dari {chats[0]}."


def unread_summary(since: float = 0) -> str:
    """'ada 3 pesan baru, dari Rina dan grup Kelas TI.' — the chat messages that came in after `since` and weren't
    read out yet (for the welcome back); '' when there are none."""
    items = [i for i in _fresh() if not i["read"] and _is_chat(i["app"]) and i["at"] >= since]
    if not items:
        return ""
    if len(items) == 1:
        return f"ada pesan {_label(items[0]['app'])} dari {_name(items[0])}."
    return f"ada {len(items)} pesan baru, dari {_join(list(dict.fromkeys(_name(i) for i in items)))}."


def mark_told(since: float = 0):
    """The chats just named in a summary (the welcome back) count as announced: the next message from them isn't
    announced again right after it (the same name twice in a row)."""
    now = time.time()
    for item in _fresh():
        if not item["read"] and _is_chat(item["app"]) and item["at"] >= since:
            _told[_chat(item)[0]] = now


def prepare_reading(senders: str = ""):
    """Digidaw just asked "Mau saya bacakan?": get the voice for the answer ready now, so "ya" is answered at once."""
    try:
        import tts
        text = _reading(messages_only=True, sender=senders, mark=False)
        if text and not text.startswith(("Tidak ada", "Belum ada")):
            tts.prepare(text)
    except Exception:
        pass


def offer_reading(senders: str = ""):
    """Digidaw just asked "Mau saya bacakan?" about these chats ("Rina, Budi"; "" = all new messages): a plain
    "ya" now reads them."""
    _offer.update(until=time.time() + OFFER_SECONDS, senders=senders)


def offered() -> str | None:
    """A "ya" right now would answer "Mau saya bacakan?": the chats it was about ("" = all new messages); None = nothing
    was offered. Only looks. Whether a phrase is a command gets asked several times before it runs (hearing.level, core),
    and when this used the offer up, the first of those took it: "iya" was then ignored, and so was "bacakan" (6 Nov 16:19)."""
    return _offer["senders"] if time.time() < _offer["until"] else None


def clear_offer():
    _offer["until"] = 0.0


def _schedule(delay: float):
    global _timer
    with _lock:
        if _timer is None:
            _timer = threading.Timer(delay, _announce_waiting)
            _timer.daemon = True
            _timer.start()


def _do_not_disturb() -> bool:
    """The notification daemon is set to Do Not Disturb (swaync, mako, dunst)."""
    for argv, on in ((["swaync-client", "-D"], lambda o: o.strip() == "true"),
                     (["makoctl", "mode"], lambda o: "do-not-disturb" in o),
                     (["dunstctl", "is-paused"], lambda o: o.strip() == "true")):
        if shutil.which(argv[0]):
            try:
                if on(subprocess.run(argv, capture_output=True, text=True, timeout=2).stdout):
                    return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def worth_telling(items: list[dict], front: dict | None = None, now: float | None = None) -> list[dict]:
    """The messages to announce now: not read yet, not from a chat just announced, not from groups unless wanted,
    and not from the chat app the user is looking at (front = the window in front)."""
    now = time.time() if now is None else now
    shown = " ".join(str((front or {}).get(k, "")) for k in ("class", "process", "title")).lower()
    chosen = []
    for item in items:
        who, group = _chat(item)
        if item["read"] or (group and config.ANNOUNCE_MESSAGES != "all"):
            continue
        if now - _told.get(who, 0) < (GROUP_GAP if group else PERSON_GAP):
            continue                     # just announced; still unread, so "cek pesan" reads it
        if shown and (_label(item["app"]).lower() in shown or item["app"].lower() in shown):
            continue                     # the user is looking at that app already
        chosen.append(item)
    return chosen


def _announce_waiting():
    global _timer
    with _lock:
        _timer = None
    if not _announcing.acquire(blocking=False):
        return _schedule(10)             # still busy with the previous announcement
    try:
        with _lock:
            items, _waiting[:] = _waiting[:], []
        import presence
        if not items or ANNOUNCE is None or config.ANNOUNCE_MESSAGES == "off":
            return
        try:
            import screen_context
            front = screen_context.foreground()
        except Exception:
            front = None
        # (the log says why, never who: messages are not written to disk)
        quiet = "Anda sedang pergi, nanti diringkas" if presence.is_away() else presence.busy_reason(front)
        chosen = [] if quiet else worth_telling(items, front)
        if quiet:
            print(f"· {len(items)} pesan baru tidak diumumkan ({quiet})")
        if not chosen:
            return
        said = {}

        def words() -> str:
            # worded when it is said, after the wait: a chat the welcome back just named isn't announced again
            said["chosen"] = worth_telling(items, front)
            return announcement(said["chosen"]) + " Mau saya bacakan?" if said["chosen"] else ""
        if ANNOUNCE(words, "message", listen=config.FOLLOWUP_SECONDS, wait=WAIT_FOR_PAUSE, push=False):
            chosen = said["chosen"]
            senders = ", ".join(dict.fromkeys(_chat(i)[0] for i in chosen))
            offer_reading(senders)
            note_chat(chosen[-1])
            now = time.time()
            for item in chosen:
                _told[_chat(item)[0]] = now
            prepare_reading(senders)
        elif said and not said.get("chosen"):
            pass                         # announced meanwhile (the welcome back): nothing left to say
        else:
            print(f"· {len(chosen)} pesan baru tidak diumumkan (Anda sedang bicara dengan Digidaw)")
    except Exception as e:
        print(f"[notifikasi] {e}")
    finally:
        _announcing.release()


AI_TOOLS = [read_notifications, clear_notifications]
