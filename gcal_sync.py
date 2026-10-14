"""
Optional: copy the agenda to Google Calendar, so the events (and their reminders) are on the phone too.

- One-way, Digidaw -> Google: a calendar named "Digidaw" is created in the user's account and kept equal to the
  agenda (added, changed and deleted events follow). Birthdays repeat yearly there too.
- The narrowest permission Google offers: calendar.app.created. Digidaw can only manage calendars it created
  itself; it can't read or touch the user's other calendars.
- Setup once: a Google Cloud "Desktop app" OAuth client saved as ~/.config/digidaw/google_client.json, then
  "hubungkan Google Calendar" (or: python gcal_sync.py login) to sign in from the browser.
"""
import datetime
import json
import os
import threading
import time
from pathlib import Path

import config

T = config.USER_TITLE
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "digidaw"
CLIENT_FILE = CONFIG_DIR / "google_client.json"
TOKEN_FILE = CONFIG_DIR / "google_token.json"
STATE_FILE = CONFIG_DIR / "google_calendar.json"     # {calendar_id, events: {agenda id: google event id}}
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
API = "https://www.googleapis.com/calendar/v3"
CALENDAR_NAME = "Digidaw"
TIMEZONE = "Asia/Jakarta"

_lock = threading.Lock()
_timer: threading.Timer | None = None
_status = {"last": 0.0, "error": ""}


# ───────────────────────────── auth ─────────────────────────────

def configured() -> bool:
    return CLIENT_FILE.exists()


def connected() -> bool:
    return TOKEN_FILE.exists()


def _credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _session():
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(_credentials())


def login() -> str:
    """Sign in from the browser (opens Google's consent page) and save the token."""
    if not configured():
        return (f"File {CLIENT_FILE} belum ada, {T}. Buat OAuth client 'Desktop app' di Google Cloud Console, "
                f"unduh JSON-nya ke lokasi itu, lalu coba lagi.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300,
                                  authorization_prompt_message="", success_message=
                                  "Digidaw tersambung ke Google Calendar. Jendela ini boleh ditutup.")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    sync_now()
    return f"Google Calendar tersambung, {T}. Agenda Digidaw sekarang muncul di kalender 'Digidaw' di HP Anda."


# ───────────────────────────── state ─────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"calendar_id": "", "events": {}}


def _save_state(state: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _calendar_id(session, state: dict) -> str:
    """The "Digidaw" calendar (created on first use; created again if the user deleted it)."""
    if state.get("calendar_id"):
        r = session.get(f"{API}/calendars/{state['calendar_id']}")
        if r.status_code == 200:
            return state["calendar_id"]
        state["events"] = {}                      # the calendar is gone: every event must be created again
    r = session.post(f"{API}/calendars", json={"summary": CALENDAR_NAME, "timeZone": TIMEZONE,
                                               "description": "Agenda dari asisten Digidaw (disinkronkan otomatis)."})
    r.raise_for_status()
    state["calendar_id"] = r.json()["id"]
    return state["calendar_id"]


# ───────────────────────────── mapping ─────────────────────────────

def event_body(event: dict) -> dict:
    """An agenda event as a Google Calendar event (all-day without a time; 1 hour with one)."""
    day = datetime.date.fromisoformat(event["date"])
    title = event["title"][:1].upper() + event["title"][1:]
    days_before = int(event.get("remind_days", 1) or 0)
    body = {"summary": title, "description": "Dari Digidaw", "reminders": {"useDefault": False, "overrides": []},
            "extendedProperties": {"private": {"digidaw_id": event["id"]}}}
    overrides = body["reminders"]["overrides"]
    if event.get("time"):
        hh, mm = map(int, event["time"].split(":"))
        start = datetime.datetime.combine(day, datetime.time(hh, mm))
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        body["end"] = {"dateTime": (start + datetime.timedelta(hours=1)).isoformat(), "timeZone": TIMEZONE}
        overrides.append({"method": "popup", "minutes": 60})
        if days_before:
            overrides.append({"method": "popup", "minutes": days_before * 1440})
    else:
        body["start"] = {"date": day.isoformat()}
        body["end"] = {"date": (day + datetime.timedelta(days=1)).isoformat()}
        # all-day: minutes count back from midnight, so 300 = 19.00 the evening before (like Digidaw itself)
        overrides.append({"method": "popup", "minutes": 300 + max(0, days_before - 1) * 1440})
    if event.get("yearly"):
        body["recurrence"] = ["RRULE:FREQ=YEARLY"]
    return body


# ───────────────────────────── sync ─────────────────────────────

def sync_now() -> str:
    """Make the Google "Digidaw" calendar equal to the agenda. Returns a short result."""
    if not connected():
        return "belum tersambung"
    import agenda
    with _lock:
        try:
            session = _session()
            state = _load_state()
            calendar = _calendar_id(session, state)
            with agenda._lock:
                events = [dict(e) for e in agenda._events]
            known = state.setdefault("events", {})
            signatures = state.setdefault("signatures", {})
            created = updated = deleted = 0
            for event in events:
                body = event_body(event)
                signature = json.dumps(body, sort_keys=True)
                google_id = known.get(event["id"])
                if google_id and signatures.get(event["id"]) == signature:
                    continue
                if google_id:
                    r = session.put(f"{API}/calendars/{calendar}/events/{google_id}", json=body)
                    if r.status_code in (404, 410):
                        google_id = None
                    else:
                        r.raise_for_status()
                        updated += 1
                if not google_id:
                    r = session.post(f"{API}/calendars/{calendar}/events", json=body)
                    r.raise_for_status()
                    known[event["id"]] = r.json()["id"]
                    created += 1
                signatures[event["id"]] = signature
            alive = {e["id"] for e in events}
            for agenda_id in [k for k in known if k not in alive]:
                r = session.delete(f"{API}/calendars/{calendar}/events/{known[agenda_id]}")
                if r.status_code not in (200, 204, 404, 410):
                    r.raise_for_status()
                known.pop(agenda_id, None)
                signatures.pop(agenda_id, None)
                deleted += 1
            _save_state(state)
            _status.update(last=time.time(), error="")
            return f"{created} ditambah, {updated} diubah, {deleted} dihapus"
        except Exception as e:
            _status["error"] = str(e)[:200]
            print(f"[Google Calendar] sinkron gagal: {e}")
            return f"gagal: {str(e)[:120]}"


def schedule_sync(delay: float = 3.0):
    """Sync in the background a moment after the agenda changed (several changes become one sync)."""
    global _timer
    if not connected():
        return
    if _timer:
        _timer.cancel()
    _timer = threading.Timer(delay, sync_now)
    _timer.daemon = True
    _timer.start()


# ───────────────────────────── skills ─────────────────────────────

def connect_google_calendar() -> str:
    """Connect Digidaw's agenda to Google Calendar so events show on the phone ('hubungkan Google Calendar',
    'sinkronkan agenda ke Google'). Opens the Google sign-in page in the browser."""
    if connected():
        result = sync_now()
        return f"Google Calendar sudah tersambung, {T}. Sinkron: {result}."
    if not configured():
        return (f"Untuk menyambungkan Google Calendar perlu file kunci OAuth dulu, {T}. Langkahnya ada di panduan "
                f"yang saya tulis; setelah file {CLIENT_FILE.name} ada di {CLIENT_FILE.parent}, ucapkan lagi perintah ini.")
    threading.Thread(target=_login_background, daemon=True, name="gcal-login").start()
    return f"Halaman masuk Google saya buka di browser, {T}. Pilih akun Anda dan izinkan Digidaw."


def _login_background():
    try:
        message = login()
    except Exception as e:
        message = f"Gagal menyambungkan Google Calendar, {T}: {str(e)[:120]}"
    import agenda
    if agenda.ANNOUNCE:
        agenda.ANNOUNCE(message, "info")


def google_calendar_status() -> str:
    """Is the agenda synced to Google Calendar? ('status Google Calendar')."""
    if not connected():
        return f"Agenda belum disinkronkan ke Google Calendar, {T}."
    if _status["error"]:
        return f"Sinkron Google Calendar terakhir gagal, {T}: {_status['error'][:120]}"
    when = datetime.datetime.fromtimestamp(_status["last"]).strftime("%H.%M") if _status["last"] else "belum"
    return f"Agenda tersinkron ke kalender 'Digidaw' di Google, {T}. Terakhir sinkron pukul {when}."


def disconnect_google_calendar() -> str:
    """Stop syncing to Google Calendar ('putuskan Google Calendar'). The "Digidaw" calendar stays in Google."""
    for path in (TOKEN_FILE, STATE_FILE):
        path.unlink(missing_ok=True)
    return f"Sinkron Google Calendar dihentikan, {T}. Kalender 'Digidaw' di Google tidak saya hapus."


AI_TOOLS = [connect_google_calendar, google_calendar_status, disconnect_google_calendar]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        import agenda
        agenda._load()
        print(login())
    elif len(sys.argv) > 1 and sys.argv[1] == "sync":
        import agenda
        agenda._load()
        print(sync_now())
    else:
        print("Pemakaian: python gcal_sync.py login | sync")
