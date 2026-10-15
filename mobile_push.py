"""
Notifications on the phone even when the HUD page is closed or the phone is locked (Web Push).

The phone's browser gives a "subscription" (an address at its push service — Google's for Chrome on Android);
Digidaw sends alerts and a ringing alarm there, encrypted for that phone only (the push service can't read
them). Needs the phone to trust Digidaw's certificate (install /ca.crt), since browsers only allow a service
worker on a trusted https page, and the PC to be online.

Subscriptions belong to a logged-in session: logging out (or "keluar dari semua HP") removes them.
"""
import base64
import itertools
import json
import os
import queue
import threading
import time
from pathlib import Path

import config
import events

SUBS_FILE = Path(config.LOG_DIR) / "push_subscriptions.json"
# the key that signs Digidaw's pushes, next to the HTTPS certificate (never in the project folder)
VAPID_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "digidaw" / "tls" / "vapid.pem"
CLAIM = "mailto:digidaw@example.com"         # push services want a contact; nothing is sent there
MAX_SUBS = 10

_subs: list[dict] = []          # {"endpoint", "keys": {"p256dh", "auth"}, "session", "added"}
_lock = threading.Lock()
_started = False


# ───────────────────────────── keys ─────────────────────────────

def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if VAPID_FILE.exists():
        return serialization.load_pem_private_key(VAPID_FILE.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAPID_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    VAPID_FILE.chmod(0o600)
    return key


def public_key() -> str:
    """The server key the phone subscribes with (base64url of the uncompressed P-256 point)."""
    from cryptography.hazmat.primitives import serialization
    raw = _private_key().public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ───────────────────────────── subscriptions ─────────────────────────────

def _load():
    try:
        data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        _subs[:] = [s for s in data if s.get("endpoint", "").startswith("https://") and s.get("keys")]
    except (OSError, ValueError):
        pass


def _save():
    try:
        SUBS_FILE.write_text(json.dumps(_subs), encoding="utf-8")
        SUBS_FILE.chmod(0o600)
    except OSError:
        pass


def subscribe(sub: dict, session: str) -> bool:
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return False
    with _lock:
        _subs[:] = [s for s in _subs if s["endpoint"] != endpoint]
        _subs.append({"endpoint": endpoint, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
                      "session": session, "added": time.time()})
        del _subs[:-MAX_SUBS]
        _save()
    return True


def unsubscribe(endpoint: str = "", session: str = ""):
    """One subscription (by endpoint), every one of a session (logout), or all (no arguments)."""
    with _lock:
        if endpoint:
            _subs[:] = [s for s in _subs if s["endpoint"] != endpoint]
        elif session:
            _subs[:] = [s for s in _subs if s.get("session") != session]
        else:
            _subs.clear()
        _save()


def subscribed(session: str) -> bool:
    with _lock:
        return any(s.get("session") == session for s in _subs)


def forget_sessions(valid: set[str]):
    """Drop subscriptions whose login has expired."""
    with _lock:
        before = len(_subs)
        _subs[:] = [s for s in _subs if s.get("session") in valid]
        if len(_subs) != before:
            _save()


# ───────────────────────────── sending ─────────────────────────────

def send(payload: dict, session: str = "", urgent: bool = False, ttl: int = 3600) -> int:
    """Push to every subscribed phone (or one session's). Returns how many accepted it."""
    from pywebpush import WebPushException, webpush
    with _lock:
        targets = [s for s in _subs if not session or s.get("session") == session]
    if not targets:
        return 0
    data = json.dumps(payload, ensure_ascii=False)
    sent, gone = 0, []
    for s in targets:
        try:
            webpush({"endpoint": s["endpoint"], "keys": s["keys"]}, data, vapid_private_key=_vapid(),
                    vapid_claims={"sub": CLAIM}, ttl=ttl, timeout=10,
                    headers={"Urgency": "high" if urgent else "normal"})
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", 0)
            if status in (404, 410):                 # the phone unsubscribed / reinstalled the browser
                gone.append(s["endpoint"])
            else:
                print(f"[HUD HP] notifikasi push gagal ({status or e})")
        except Exception as e:                       # offline, DNS...
            print(f"[HUD HP] notifikasi push gagal ({type(e).__name__}: {e})")
    for endpoint in gone:
        unsubscribe(endpoint)
    return sent


_vapid_cache: list = []


def _vapid():
    if not _vapid_cache:
        from py_vapid import Vapid
        _private_key()                               # made on first use
        _vapid_cache.append(Vapid.from_file(str(VAPID_FILE)))
    return _vapid_cache[0]


_outbox: queue.Queue = queue.Queue(maxsize=100)


def _send_async(payload: dict, **kw):
    """Queued: one sender sends in order (two pushes sent at the same instant could lose one)."""
    try:
        _outbox.put_nowait((payload, kw))
    except queue.Full:
        pass


def _sender():
    while True:
        payload, kw = _outbox.get()
        try:
            send(payload, **kw)
        except Exception as e:
            print(f"[HUD HP] notifikasi push: {e}")


# ───────────────────────────── what gets pushed ─────────────────────────────

_ids = itertools.count(1)       # each alert its own notification (a shared tag would replace the earlier one)
LEVEL_TITLE = {"warning": "Peringatan", "reminder": "Pengingat", "info": "Digidaw"}
KIND_TITLE = {"alarm": "Alarm", "timer": "Timer selesai", "reminder": "Pengingat"}


def _watch():
    q = events.subscribe()
    while True:
        event = q.get()
        try:
            kind = event.get("type")
            if kind == "alert" and event.get("text"):
                title, text = LEVEL_TITLE.get(event.get("level"), "Digidaw"), event["text"]
                head, sep, rest = text.partition(":")
                if sep and rest.strip() and len(head) <= 24:     # "Pengingat: rapat …" -> title + the rest
                    title, text = head.strip(), rest.strip()
                _send_async({"kind": "alert", "title": title, "text": text[:300], "level": event.get("level", "info"),
                             "tag": f"alert-{int(event['ts'])}-{next(_ids)}"})
            elif kind == "alarm" and event.get("ringing"):
                _send_async({"kind": "alarm", "title": KIND_TITLE.get(event.get("kind"), "Alarm"),
                             "text": (event.get("text") or event.get("label") or "Alarm")[:300],
                             "tag": f"alarm-{event.get('id')}"}, urgent=True, ttl=120)
            elif kind == "alarm":
                _send_async({"kind": "alarm_end", "tag": f"alarm-{event.get('id')}"}, urgent=True, ttl=60)
        except Exception as e:
            print(f"[HUD HP] notifikasi push: {e}")


def start():
    """Load subscriptions and forward alerts / alarms to subscribed phones."""
    global _started
    if _started:
        return
    _started = True
    _load()
    threading.Thread(target=_watch, daemon=True, name="hp-push-watch").start()
    threading.Thread(target=_sender, daemon=True, name="hp-push-send").start()
