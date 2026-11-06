"""
Agenda: events on a date ("ulang tahun ibu tanggal 20 Oktober", "rapat 15/11 jam 9"), remembered across restarts.

- Birthdays / anniversaries repeat every year by themselves; anything can ("setiap tahun").
- Reminders come in steps: the evening before (19.00, or N days before when asked: "ingatkan seminggu
  sebelumnya"), the morning of the day (07.00), and an hour before an event that has a time.
- A reminder that fell while the PC was off is said once when Digidaw starts (if the event isn't over yet).

alarms.py handles clock-time things (timer, alarm, "ingatkan jam 7"); this module handles dates.
"""
import datetime
import json
import re
import threading
import time as time_module
import time
import uuid
from pathlib import Path

import config
import jadwal_kuliah
import storage
import events

T = config.USER_TITLE
ANNOUNCE = None                       # set by main (core.announce)
STATE_FILE = Path(config.BASE_DIR) / "agenda.json"
SMALL_HOURS_UNTIL = 4                 # before this hour the night isn't over yet (tomorrow_offset)
EVENING_BEFORE = (19, 0)              # "besok ada ..." the day before
MORNING_OF = (7, 0)                   # "hari ini ..." on the day
HOUR_BEFORE = 60                      # minutes before an event with a time
CATCH_UP_HOURS = 14                   # a reminder missed while Digidaw was off is still said this long after
BULAN = ["januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september", "oktober",
         "november", "desember"]
BULAN_PENDEK = {"jan": 1, "feb": 2, "peb": 2, "mar": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "agu": 8, "agt": 8,
                "ags": 8, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "oct": 10, "nov": 11, "nop": 11, "des": 12, "dec": 12}
HARI = ["senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu"]
YEARLY_RE = re.compile(r"\b(?:ulang ?tahun|ultah|ulta|milad|hari jadi|anniversary|hut|peringatan pernikahan|"
                       r"setiap tahun|tiap tahun|tahunan)\b", re.IGNORECASE)

_events: list[dict] = []       # {id, title, date "YYYY-MM-DD", time "HH:MM"|"", yearly, remind_days, created}
_notified: set[str] = set()    # "id|occurrence date|step" already said
_lock = threading.RLock()


# ───────────────────────────── storage ─────────────────────────────

def _save():
    data = {"events": _events, "notified": sorted(_notified)[-400:]}
    try:
        storage.save_json(STATE_FILE, data, backup=True, mode=storage.PRIVATE, indent=1)
    except OSError:
        pass
    events.emit("agenda", items=snapshot())


_loaded = {"file": None}


def _ensure_loaded():
    """Before changing anything: a process that hadn't read the file wrote only its new event over all the others (it happened)."""
    if _loaded["file"] != STATE_FILE:
        _load()


def _load():
    _loaded["file"] = STATE_FILE
    data = storage.load_json(STATE_FILE)
    if not isinstance(data, dict):
        return
    with _lock:
        _events[:] = data.get("events", [])
        _notified.update(data.get("notified", []))


# ───────────────────────────── dates ─────────────────────────────

def _today() -> datetime.date:
    return datetime.date.today()


def _hour() -> int:
    return datetime.datetime.now().hour


def tomorrow_offset() -> int:
    """How many calendar days away "besok" is. Said at 00:50, before going to sleep, it is the morning ahead: "kuliah besok jam 9" was saved a day too late."""
    return 0 if _hour() < SMALL_HOURS_UNTIL else 1


def _num(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    try:
        import alarms
        value = alarms._number(word)
        return int(value) if value else None
    except Exception:
        return None


def _month(word: str) -> int | None:
    w = word.lower().strip(".")
    if w in BULAN:
        return BULAN.index(w) + 1
    return BULAN_PENDEK.get(w[:4]) or BULAN_PENDEK.get(w[:3])


_DAY_NUM = r"(\d{1,2}|(?:se|dua|tiga) ?puluh(?: (?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan))?|" \
           r"satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|(?:dua|tiga|empat|lima|enam|" \
           r"tujuh|delapan|sembilan) belas)"
_MONTH_WORD = r"(januari|februari|pebruari|maret|april|mei|juni|juli|agustus|september|oktober|november|nopember|" \
              r"desember|jan|feb|mar|apr|jun|jul|agu|agt|ags|aug|sep|sept|okt|oct|nov|nop|des|dec)\b\.?"
DATE_PATTERNS = [
    # 20 oktober 2026 / tanggal 20 okt
    (re.compile(rf"(?:tanggal |tgl\.? |tg )?\b{_DAY_NUM} {_MONTH_WORD}(?: (\d{{4}}))?", re.IGNORECASE), "dmy_words"),
    # 20/10/2026, 20-10, 20.10.2026
    (re.compile(r"(?:tanggal |tgl\.? )?\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b(?![.:]\d)", re.IGNORECASE), "dmy_digits"),
    # tanggal 20 (this or next month)
    (re.compile(rf"\b(?:tanggal|tgl\.?|tg) {_DAY_NUM}\b(?!\s*(?:jam(?!\s+\d)|menit))", re.IGNORECASE), "day_only"),
    (re.compile(r"\b(hari ini|nanti malam|malam ini|besok lusa|besok|lusa)\b", re.IGNORECASE), "relative"),
    (re.compile(r"\b(minggu depan|pekan depan|bulan depan|tahun depan)\b", re.IGNORECASE), "next"),
    (re.compile(r"\b(?:hari )?(senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu|ahad)(?: (depan|ini))?\b", re.IGNORECASE),
     "weekday"),
]


def _future(date: datetime.date, keep_year: bool) -> datetime.date:
    """A date without a year that already passed this year means next year."""
    if not keep_year and date < _today():
        try:
            return date.replace(year=date.year + 1)
        except ValueError:
            return date.replace(year=date.year + 1, day=28)
    return date


def parse_date(text: str) -> tuple[datetime.date | None, str]:
    """(date, the matched words) from Indonesian text; (None, "") when there is no date in it."""
    today = _today()
    for pattern, kind in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if kind == "dmy_words":
                day, month = _num(m[1]), _month(m[2])
                if not day or not month:
                    continue
                year = int(m[3]) if m[3] else today.year
                return _future(datetime.date(year, month, day), bool(m[3])), m[0]
            if kind == "dmy_digits":
                day, month = int(m[1]), int(m[2])
                if not 1 <= month <= 12 or not 1 <= day <= 31:
                    continue
                year = int(m[3]) if m[3] else today.year
                year += 2000 if year < 100 else 0
                return _future(datetime.date(year, month, day), bool(m[3])), m[0]
            if kind == "day_only":
                day = _num(m[1])
                if not day or not 1 <= day <= 31:
                    continue
                month, year = today.month, today.year
                if day < today.day:
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
                return datetime.date(year, month, day), m[0]
            if kind == "relative":
                tomorrow = tomorrow_offset()
                offset = {"hari ini": 0, "nanti malam": 0, "malam ini": 0, "besok": tomorrow, "lusa": tomorrow + 1, "besok lusa": tomorrow + 1}[m[1].lower()]
                return today + datetime.timedelta(days=offset), m[0]
            if kind == "next":
                word = m[1].lower()
                if word in ("minggu depan", "pekan depan"):
                    return today + datetime.timedelta(days=7), m[0]
                if word == "bulan depan":
                    month, year = (1, today.year + 1) if today.month == 12 else (today.month + 1, today.year)
                    return datetime.date(year, month, min(today.day, 28)), m[0]
                return today.replace(year=today.year + 1), m[0]
            if kind == "weekday":
                name = {"jum'at": "jumat", "ahad": "minggu"}.get(m[1].lower(), m[1].lower())
                ahead = (HARI.index(name) - today.weekday()) % 7 or (7 if tomorrow_offset() else 0)      # (at 00:50 on a Friday, "jumat" is the morning ahead)
                if (m[2] or "").lower() == "depan" and ahead < 7:
                    ahead += 7 if ahead <= 2 else 0
                return today + datetime.timedelta(days=ahead), m[0]
        except ValueError:
            continue
    return None, ""


def parse_time(text: str) -> str:
    """'jam 7 malam' -> '19:00' ('' when the text has no time of day)."""
    import alarms
    # only the clock phrase: other numbers in the sentence (the date "15/11") confuse the clock parser
    m = CLOCK_PHRASE.search(text) or re.search(r"\b\d{1,2}[.:]\d{2}\b(?:\s+(?:pagi|siang|sore|petang|malam))?", text)
    if not m:
        return ""
    clock = alarms.parse_clock(m[0].lower())
    return f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""


CLOCK_PHRASE = re.compile(r"\b(?:jam|pukul)\s+(?:setengah\s+)?[\w.:]+(?:\s+(?:lewat|lebih|kurang)\s+(?:seperempat|\w+)"
                          r"(?:\s+menit)?)?(?:\s+(?:pagi|siang|sore|petang|malam|subuh))?", re.IGNORECASE)


def _occurrence(event: dict, today: datetime.date | None = None) -> datetime.date | None:
    """The next date the event happens (today counts), or None for a one-off that is over."""
    today = today or _today()
    date = datetime.date.fromisoformat(event["date"])
    if not event.get("yearly"):
        return date if date >= today else None
    for year in (today.year, today.year + 1):
        try:
            candidate = date.replace(year=year)
        except ValueError:                       # 29 February
            candidate = date.replace(year=year, day=28)
        if candidate >= today:
            return candidate
    return None


# ───────────────────────────── speaking about dates ─────────────────────────────

def date_text(date: datetime.date, with_day: bool = True) -> str:
    base = f"{date.day} {BULAN[date.month - 1].capitalize()}" + (f" {date.year}" if date.year != _today().year else "")
    return f"{HARI[date.weekday()].capitalize()}, {base}" if with_day else base


def when_text(date: datetime.date) -> str:
    days = (date - _today()).days
    tomorrow = tomorrow_offset()
    if days == tomorrow:
        return "besok"
    if days == 0:
        return "hari ini"
    if days == tomorrow + 1:
        return "lusa"
    if days < 7:
        return f"{HARI[date.weekday()].capitalize()} ini, {days} hari lagi"
    return f"{date_text(date)}, {days} hari lagi"


def said_back(date: datetime.date) -> str:
    """The day in full, when an event is saved: a day misunderstood is heard at once. ("besok jam 09.00" at 00:50 told the user nothing.)"""
    when, days = when_text(date), (date - _today()).days
    if when in ("hari ini", "besok", "lusa"):
        return f"{when}, {date_text(date)}"
    return f"{date_text(date)}, {days} hari lagi" if 0 < days < 7 else when


def _still_ahead(date: datetime.date, hhmm: str, words: str) -> datetime.date:
    """"besok jam 2" said at 03:00 can't be an hour ago: then it is the night after."""
    if date == _today() and hhmm and "besok" in (words or "").lower() and hhmm <= _clock():
        return date + datetime.timedelta(days=1)
    return date


def _clock() -> str:
    return datetime.datetime.now().strftime("%H:%M")


def _title(event: dict) -> str:
    return event["title"][:1].upper() + event["title"][1:]


# ───────────────────────────── reminders ─────────────────────────────

def _steps(event: dict, date: datetime.date) -> list[tuple[str, datetime.datetime, str, bool]]:
    """(key, when, sentence, ring) for every reminder of this occurrence."""
    title, steps = event["title"], []
    days_before = event.get("remind_days", 1)
    if days_before:
        day = date - datetime.timedelta(days=days_before)
        when = datetime.datetime.combine(day, datetime.time(*EVENING_BEFORE))
        lead = "Besok" if days_before == 1 else "Lusa" if days_before == 2 else f"{days_before} hari lagi"
        steps.append((f"before{days_before}", when,
                      f"{lead} {title}, {T}, {date_text(date)}{' jam ' + event['time'].replace(':', '.') if event.get('time') else ''}.",
                      False))
    morning = datetime.datetime.combine(date, datetime.time(*MORNING_OF))
    if event.get("time"):
        hh, mm = map(int, event["time"].split(":"))
        start = datetime.datetime.combine(date, datetime.time(hh, mm))
        if start - datetime.timedelta(minutes=HOUR_BEFORE) > morning:
            steps.append(("morning", morning, f"Hari ini ada {title}, {T}, jam {event['time'].replace(':', '.')}.", False))
        steps.append(("soon", start - datetime.timedelta(minutes=HOUR_BEFORE),
                      f"Pengingat, {T}: {title} satu jam lagi, jam {event['time'].replace(':', '.')}.", True))
    else:
        wish = " Jangan lupa ucapkan selamat." if re.search(r"ulang ?tahun|ultah|milad|anniversary|hari jadi", title, re.I) else ""
        steps.append(("morning", morning, f"Hari ini {title}, {T}.{wish}", True))
    return steps


def _say(text: str, ring: bool, event: dict):
    if ring:
        try:
            import alarms
            item = {"id": event["id"], "kind": "reminder", "label": event["title"], "time": event.get("time") or ""}
            threading.Thread(target=alarms._ring, args=(item, text), daemon=True, name="agenda-ring").start()
            return
        except Exception:
            pass
    if ANNOUNCE:
        ANNOUNCE(text, "reminder")
    else:
        print(f"📅 {text}")


def _due_now(catch_up: bool = False) -> list[tuple[str, str, bool, dict]]:
    """Reminders whose moment has come and weren't said yet. catch_up: also ones missed while Digidaw was off."""
    now = datetime.datetime.now()
    out = []
    with _lock:
        for event in list(_events):
            # look at yesterday's occurrence too, so "hari ini" isn't skipped right after midnight
            date = _occurrence(event, _today() - datetime.timedelta(days=1))
            if date is None:
                continue
            for step, when, text, ring in _steps(event, date):
                key = f"{event['id']}|{date.isoformat()}|{step}"
                if key in _notified or when > now:
                    continue
                late = (now - when).total_seconds()
                event_over = date < _today()
                if late <= 120 or (catch_up and late <= CATCH_UP_HOURS * 3600 and not event_over):
                    out.append((key, text, ring and late <= 120, event))
                _notified.add(key)        # a reminder that is too old is dropped silently
        for key, text in jadwal_kuliah.due_reminders(_notified):
            out.append((key, text, False, {}))
            _notified.add(key)
    return out


def _cleanup():
    """One-off events are kept a day after they pass, then removed."""
    with _lock:
        before = len(_events)
        _events[:] = [e for e in _events if e.get("yearly")
                      or datetime.date.fromisoformat(e["date"]) >= _today() - datetime.timedelta(days=1)]
        return len(_events) != before


def _loop():
    day = _today()
    while True:
        time.sleep(20)
        try:
            due = _due_now()
            changed = _cleanup()
            if _today() != day:             # a new day: "BESOK" becomes "HARI INI" on the HUD
                day, changed = _today(), True
            if due or changed:
                _save()
            for _, text, ring, event in due:
                _say(text, ring, event)
        except Exception as e:
            print(f"[agenda] {e}")


def _sync_google():
    """Copy the change to Google Calendar in the background (only when the user connected it)."""
    try:
        import gcal_sync
        gcal_sync.schedule_sync()
    except Exception:
        pass


def start():
    _load()
    if _cleanup():
        _sync_google()
    _sync_google()                      # catch up with changes made while Google wasn't reachable
    missed = _due_now(catch_up=True)
    _save()
    threading.Thread(target=_loop, daemon=True, name="agenda").start()
    if missed:
        text = " ".join(t for _, t, _, _ in missed[:3])
        threading.Timer(12, _say, args=(text, False, missed[0][3])).start()


# ───────────────────────────── for the HUD / briefing ─────────────────────────────

def upcoming(days: int = 30, keyword: str = "") -> list[tuple[datetime.date, dict]]:
    with _lock:
        items = [(d, e) for e in _events if (d := _occurrence(e)) and (d - _today()).days <= days
                 and (not keyword or keyword.lower() in e["title"].lower())]
    return sorted(items, key=lambda x: (x[0], x[1].get("time") or ""))


def snapshot() -> list[dict]:
    rows = [{"id": e["id"], "title": _title(e), "date": d.isoformat(), "time": e.get("time", ""),
             "yearly": bool(e.get("yearly")), "days": (d - _today()).days} for d, e in upcoming(60)]
    rows += [{"id": f"w{e['id']}", "title": e["name"], "date": d.isoformat(), "time": e["time"], "yearly": False,
              "days": (d - _today()).days, "weekly": True} for d, e in jadwal_kuliah.upcoming(2, classes_only=True)]
    return sorted(rows, key=lambda r: (r["date"], r["time"] or ""))[:8]


def all_events() -> list[dict]:
    """Everything on the agenda for its page: the next date of each event, and those just past (kept for a day)."""
    today = _today()
    rows = []
    with _lock:
        for event in _events:
            day = _occurrence(event) or datetime.date.fromisoformat(event["date"])
            rows.append({"id": event["id"], "title": _title(event), "name": event["title"], "date": day.isoformat(),
                         "time": event.get("time", ""), "yearly": bool(event.get("yearly")),
                         "remind_days": event.get("remind_days", 1), "days": (day - today).days, "past": day < today or _past(day, event)})
    return sorted(rows, key=lambda r: (r["date"], r["time"] or ""))


def update_event(event_id: str, title: str, date: str, time: str = "", yearly: bool = False, remind_days_before: int = 1) -> tuple[bool, str]:
    """A change made on the agenda page. (done, what to tell the user)."""
    _ensure_loaded()
    title = re.sub(r"\s+", " ", (title or "").strip(" .,"))[:120]
    if not title:
        return False, "Nama acara tidak boleh kosong."
    try:
        day = datetime.date.fromisoformat(str(date).strip())
    except ValueError:
        return False, "Tanggalnya belum benar."
    m = re.fullmatch(r"(\d{1,2})[.:](\d{2})", str(time or "").strip())
    if time and not m:
        return False, "Jamnya belum benar."
    yearly = bool(yearly) or bool(YEARLY_RE.search(title))
    if not yearly and day < _today():
        return False, f"Tanggal {date_text(day)} sudah lewat."
    with _lock:
        event = next((e for e in _events if e["id"] == event_id), None)
        if not event:
            return False, "Acara itu sudah tidak ada."
        event.update(title=title, date=day.isoformat(), time=f"{int(m[1]):02d}:{m[2]}" if m else "", yearly=yearly,
                     remind_days=max(0, int(remind_days_before)))
    _save()
    _sync_google()
    return True, f"{title} diperbarui."


def _event_parts(text: str) -> dict:
    """Title, date and time from a sentence, by rule ("MTS cyber security besok jam 07", "tanggal 7 November jam 6 acaranya
    kuliah"). The small model lost the time every time and made up dates ("20 oktober" from this file's own docs)."""
    t = re.sub(r"\b(jam|pukul)(?:\s+(?:jam|pukul))+\b", r"\1", re.sub(r"\s+", " ", text.strip(" .!?")), flags=re.I)
    low = t.lower()
    date, date_words = parse_date(low)
    hhmm = parse_time(low)
    if date:
        date = _still_ahead(date, hhmm, date_words)
    rest = re.sub(re.escape(date_words), " ", t, flags=re.I) if date_words else t
    if date and not hhmm:                 # "7 November 6 acaranya ...": Google dropped the word "jam"
        m = re.match(r"\s*(\d{1,2})\b(?![.:/-]\d)", rest)
        if m and int(m[1]) <= 23:
            hhmm, rest = f"{int(m[1]):02d}:00", rest[m.end():]
    named = re.search(r"\b(?:acaranya|namanya|judulnya)\s+(.+)$", rest, re.I)
    title = _title_from(named[1] if named else rest)
    if title.lower() in GENERIC_TITLES or len(title) < 2:
        title = ""
    return {"title": title, "date": date, "time": hhmm}


GENERIC_TITLES = {"agenda", "acara", "jadwal", "kegiatan", "rencana", "event", "baru", "lagi", "dong", "ini", "itu",
                  "januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september", "oktober", "november", "desember",
                  "senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu", "tanggal", "bulan", "hari"}
DRAFT_SECONDS = 45
_draft: dict = {}                    # an event being dictated over a few sentences: title, date, time, at
CANCEL_DRAFT = re.compile(r"(?:batal|batalkan|tidak jadi|nggak jadi|gak jadi|enggak jadi|ga jadi|tidak usah|nggak usah|gak usah)"
                          r"(?: (?:saja|aja|dulu|deh))?")
OTHER_COMMAND = re.compile(r"(?:buka|tutup|pindah|putar|ubah|ganti|matikan|nyalakan|hidupkan|ketik|cari|setel|atur|jam berapa|"
                           r"lanjutkan|pause|stop|cuaca|kipas|lampu|volume|jalankan)\b")


def _draft_question() -> str:
    title, date = _draft.get("title"), _draft.get("date")
    if not title and not date:
        return f"Acaranya apa dan kapan, {T}?"
    if not title:
        return f"Acaranya apa, {T}?"
    return f"Kapan {title}, {T}?"


def _finish_or_ask() -> str:
    if _draft.get("title") and _draft.get("date"):
        title, date, hhmm = _draft["title"], _draft["date"], _draft.get("time", "")
        _draft.clear()
        return add_event(title, date.isoformat(), hhmm)
    return _draft_question()


def add_from_sentence(text: str) -> str:
    """The sentence is about adding an event: saved when it says what and when, otherwise Digidaw asks for the rest."""
    _draft.clear()
    _draft.update(_event_parts(text), at=time_module.time())
    return _finish_or_ask()


def _continue_draft(t: str, low: str) -> str | None:
    """The next sentence after "Acaranya apa dan kapan?". None = it isn't an answer (the draft is dropped)."""
    if time_module.time() - _draft.get("at", 0) > DRAFT_SECONDS:
        _draft.clear()
        return None
    if CANCEL_DRAFT.fullmatch(low):
        _draft.clear()
        return f"Baik, {T}, tidak jadi."
    parts = _event_parts(t)
    if OTHER_COMMAND.match(low) and not parts["date"]:
        _draft.clear()
        return None
    new = {k: v for k, v in parts.items() if v and not _draft.get(k)}
    if not new or (re.search(r"\b(?:apa|apakah|kenapa|siapa|bagaimana|gimana)\b", low) and not parts["date"]):
        _draft.clear()
        return None
    _draft.update(new, at=time_module.time())
    return _finish_or_ask()


def open_agenda_page() -> str:
    """Open the agenda on its own page: every event with its date, day and time ('buka agenda', 'tampilkan jadwal saya')."""
    import hud_server
    if not hud_server.open_agenda():
        return f"Halaman agenda tidak bisa dibuka, {T}."
    items = [r for r in all_events() if not r["past"]]
    if not items:
        return f"Agenda saya buka, {T}. Belum ada acara yang tercatat."
    first = items[0]
    at = f" jam {first['time'].replace(':', '.')}" if first["time"] else ""
    return f"Agenda saya buka, {T}. Ada {len(items)} acara, terdekat {first['title']} {when_text(datetime.date.fromisoformat(first['date']))}{at}."


def page_data() -> dict:
    """Everything the agenda page shows."""
    import alarms
    import pc_control
    return {"events": all_events(), "alarms": alarms.snapshot(), "weekly": jadwal_kuliah.entries(),
            "weekly_editable": pc_control.is_my_script("pengingat jadwal")}


def close_agenda_page() -> str:
    """Close the agenda page ('tutup agenda')."""
    import hud_server
    return f"Agenda saya tutup, {T}." if hud_server.close_agenda() else f"Halaman agenda sedang tidak terbuka, {T}."


def briefing_line() -> str:
    soon = upcoming(7)
    classes = jadwal_kuliah.briefing_line()
    if not soon:
        return classes
    line = "Agenda minggu ini: " + "; ".join(f"{e['title']} {when_text(d)}" for d, e in soon[:3]) + "."
    return f"{classes} {line}".strip()


# ───────────────────────────── skills ─────────────────────────────

def add_event(title: str, date: str, time: str = "", yearly: bool = False, remind_days_before: int = -1) -> str:
    """Save an event on a date and remind the user about it ('ingatkan saya ulang tahun ibu tanggal 20 Oktober',
    'rapat tanggal 15/11 jam 9'). date: 'YYYY-MM-DD' or Indonesian words ('20 oktober', 'besok', 'sabtu depan').
    time: 'HH:MM' (optional). yearly: repeats every year (birthdays are yearly automatically).
    remind_days_before: leave it out (-1) unless the user says how early to be reminded ("seminggu sebelumnya" = 7);
    by default the reminder comes the evening before."""
    _ensure_loaded()
    title = re.sub(r"\s+", " ", (title or "").strip(" .,"))
    try:
        day = datetime.date.fromisoformat(date.strip())
    except ValueError:
        day, _ = parse_date(date)
    if not day:
        return f"Tanggal '{date}' tidak saya pahami, {T}. Coba sebutkan seperti '20 Oktober'."
    hhmm = ""
    if time:
        m = re.match(r"^\s*(\d{1,2})[.:](\d{2})\s*$", time)
        hhmm = f"{int(m[1]):02d}:{m[2]}" if m else parse_time(time)
    if not title or title.lower() in GENERIC_TITLES:     # "tambahkan agenda" became an event named "agenda"
        _draft.clear()
        _draft.update(title="", date=day, time=hhmm, at=time_module.time())
        return _draft_question()
    yearly = bool(yearly) or bool(YEARLY_RE.search(title))
    if not yearly and day < _today():
        return f"Tanggal {date_text(day)} sudah lewat, {T}."
    remind = remind_days_before if remind_days_before is not None and remind_days_before >= 0 else 1
    with _lock:
        same = next((e for e in _events if e["title"].lower() == title.lower() and e["date"][5:] == day.isoformat()[5:]), None)
        if same:
            return f"{_title(same)} {date_text(day, False)} sudah ada di agenda, {T}."
        event = {"id": uuid.uuid4().hex[:8], "title": title, "date": day.isoformat(), "time": hhmm,
                 "yearly": yearly, "remind_days": int(remind), "created": int(datetime.datetime.now().timestamp())}
        _events.append(event)
    _save()
    _sync_google()
    occurrence = _occurrence(event) or day
    extra = " Saya ingatkan setiap tahun." if yearly else ""
    # (no promise of a reminder whose moment has already gone by: at 00:50 for the morning ahead there is no evening before)
    gone = datetime.datetime.combine(occurrence - datetime.timedelta(days=remind), datetime.time(*EVENING_BEFORE)) <= datetime.datetime.now()
    before = ("" if not remind or gone else " Saya kabari malam sebelumnya juga." if remind == 1 else
              f" Saya kabari juga {remind} hari sebelumnya.")
    at = f", jam {hhmm.replace(':', '.')}" if hhmm else ""
    return f"Siap, {T}. {_title(event)} saya catat {said_back(occurrence)}{at}.{before}{extra}"


def list_events(days: int = 30, keyword: str = "", start: int = 0) -> str:
    """The user's schedule / upcoming events, with their weekly classes ('apa saja jadwal saya', 'acara apa saja bulan ini',
    'jadwal besok', 'kapan ulang tahun ibu' with keyword). days: how far ahead (0 = today only); start: skip the first N days
    (start=1, days=1 = tomorrow only). ALWAYS call this to answer about the schedule; never guess."""
    events_said = _events_said(days, keyword, start)
    classes = "" if keyword else jadwal_kuliah.agenda_sentence(days, start)
    if not classes:
        return events_said
    if events_said.startswith("Tidak ada jadwal"):         # (the classes are a schedule too)
        events_said = events_said.replace("Tidak ada jadwal", "Tidak ada acara lain", 1)
    return f"{classes.replace(f', {T}.', '.')} {events_said}"


def _events_said(days: int = 30, keyword: str = "", start: int = 0) -> str:
    items = [(d, e) for d, e in upcoming(366 if keyword else days, keyword) if (d - _today()).days >= start]
    if not items:
        if keyword:
            return f"Tidak ada acara '{keyword}' di agenda, {T}."
        if start == days and start <= 2:
            day = _today() + datetime.timedelta(days=start)      # (after midnight its name can't be mistaken, whatever word was asked with)
            span = date_text(day) if not tomorrow_offset() else when_text(day)
        else:
            span = f"dalam {days} hari ke depan"
        later = upcoming(366)
        hint = f" Acara terdekat: {later[0][1]['title']} {when_text(later[0][0])}." if later else ""
        return f"Tidak ada jadwal {span}, {T}.{hint}"
    if keyword and len(items) == 1:
        d, e = items[0]
        return f"{_title(e)} {when_text(d)}" + (f" jam {e['time'].replace(':', '.')}" if e.get("time") else "") + f", {T}."
    parts = [f"{e['title']} {when_text(d)}" + (f" jam {e['time'].replace(':', '.')}" if e.get("time") else "")
             + (" (sudah lewat)" if _past(d, e) else "") for d, e in items[:6]]
    more = f", dan {len(items) - 6} lainnya" if len(items) > 6 else ""
    if items and all(_past(d, e) for d, e in items):        # 18.43: "Olahraga hari ini jam 15.00" is over already
        return f"Acara hari ini sudah lewat semua, {T}: " + "; ".join(p.replace(" (sudah lewat)", "") for p in parts) \
            + more + ". Sisa hari ini kosong."
    return f"Ada {len(items)} acara, {T}: " + "; ".join(parts) + more + "."


def _past(day: datetime.date, event: dict) -> bool:
    """An event of today whose time has already gone by."""
    if day != _today() or not event.get("time"):
        return False
    try:
        hour, minute = (int(x) for x in event["time"].split(":")[:2])
    except ValueError:
        return False
    return _clock() > f"{hour:02d}:{minute:02d}"


def _match_title(phrase: str) -> str:
    """The title of the agenda event this phrase talks about ('bayar listrik' -> 'membayar listrik'), or ''."""
    words = [w for w in re.findall(r"\w+", phrase.lower()) if len(w) >= 3 and w not in ("saya", "aku", "harus", "nanti")]
    if not words:
        return ""
    with _lock:
        titles = [e["title"] for e in _events]
    best, score = "", 0
    for title in titles:
        low = title.lower()
        hits = sum(1 for w in words if w in low)
        if hits > score and hits >= max(1, (len(words) + 1) // 2):
            best, score = title, hits
    return best


def _several_days(key: str) -> list[str]:
    """"tanggal 7 dan 8", "besok dan lusa": each day on its own. Only when every part is a day: "makan dan minum" is one title."""
    parts = [p.strip() for p in re.split(r"\s+dan\s+|\s*,\s*", key) if p.strip()]
    if len(parts) < 2:
        return []
    parts = [p if not p.isdigit() else f"tanggal {p}" for p in parts]
    return parts if all(parse_date(p)[0] for p in parts) else []


def delete_event(keyword: str) -> str:
    """Remove an event from the agenda ('hapus acara wisuda', 'batalkan agenda rapat', 'hapus agenda besok', 'hapus agenda tanggal 6')."""
    _ensure_loaded()
    key = keyword.lower().strip()
    hits: list[dict] = []
    for one in _several_days(key) or [key]:
        day = parse_date(one)[0]
        with _lock:
            found = [e for e in _events if one and (one in e["title"].lower() or e["id"] == one)]
            if not found and day:        # by its day ("hapus agenda tanggal 6" found nothing while the event was there)
                found = [e for e in _events if e["date"] == day.isoformat()]
            for e in found:
                _events.remove(e)
        hits += found
    if not hits:
        return f"Tidak ada acara '{keyword}' di agenda, {T}."
    _save()
    _sync_google()
    return f"{', '.join(_title(e) for e in hits[:3])} dihapus dari agenda, {T}."


# ───────────────────────────── understanding speech locally ─────────────────────────────

# "apa saja jadwal saya", "jadwal saya apa aja", "besok ada acara apa", "lihat agenda", "jadwalku minggu ini"
_AGENDA_NOUN = r"(?:jadwal|agenda|acara|kegiatan|rencana)(?:\s?(?:saya|ku|aku|gue|gua|nya))?"
LIST_QUESTION = re.compile(
    rf"\b(?:apa saja|apa aja|apa|ada apa|daftar|lihat|cek|tampilkan|sebutkan|bacakan|berapa)\b.*\b{_AGENDA_NOUN}\b|"
    rf"\b{_AGENDA_NOUN}\b.*\b(?:apa saja|apa aja|apa|ada apa|minggu ini|bulan ini|hari ini|besok|lusa|mendatang|"
    rf"terdekat|ke depan|berikutnya|minggu depan|bulan depan)\b|^{_AGENDA_NOUN}$|^(?:ada )?{_AGENDA_NOUN} (?:hari ini|besok|lusa)$")
# a sentence that ADDS ("ingatkan ... tanggal 15") is never a list question even if it says "jadwal"
ADD_TRIGGER_ONLY = re.compile(r"\b(?:ingatkan|ingetin|ingatin|catat|catatkan|simpan|tambahkan|tambah|jadwalkan|"
                              r"masukkan|hapus|batalkan|buang)\b")
ADD_TRIGGER = re.compile(r"\b(?:ingatkan|ingetin|ingatin|catat|catatkan|simpan|tambahkan|tambah|jadwalkan|masukkan|"
                         r"pasang)\b|\b(?:ada|punya) (?:acara|agenda|janji|undangan)\b|\b(?:ulang ?tahun|ultah)\b",
                         re.IGNORECASE)
EXPLICIT_DATE = re.compile(rf"\b(?:tanggal|tgl)\b|\b{_MONTH_WORD}|\b\d{{1,2}}[/\-]\d{{1,2}}\b", re.IGNORECASE)
EVENT_WORD = re.compile(r"\b(?:acara|agenda|janji|undangan|ulang ?tahun|ultah|rapat|meeting|ujian|wisuda|pernikahan|"
                        r"nikahan|kondangan|arisan|seminar|kuliah|deadline|tenggat|konser|liburan|perjalanan|"
                        r"kontrol|jadwal)\b", re.IGNORECASE)
_TITLE_FILLER = re.compile(
    r"\b(?:tolong|coba|dong|ya|deh|saya|aku|gue|gua|nanti|ingatkan|ingetin|ingatin|catat|catatkan|simpan|tambahkan|"
    r"tambah|tambahkan|buatkan|bikin|bikinkan|jadwal|kegiatan|jadwalkan|masukkan|pasang|pengingat|reminder|agenda|di agenda|"
    r"ke agenda|kalau|bahwa|ada|punya|acara|acaranya|namanya|judulnya|baru|"
    r"pada|di|ke|untuk|buat|tentang|setiap tahun|tiap tahun|tahunan|yang|nya|tolongin|nih|sebelumnya|sebelum)\b",
    re.IGNORECASE)


def _remind_days(text: str) -> int:
    m = re.search(r"\b(seminggu|sehari|dua hari|tiga hari|(\d+) hari|(\d+) minggu) sebelum", text, re.IGNORECASE)
    if not m:
        return -1
    word = m[1].lower()
    if word == "seminggu":
        return 7
    if word == "sehari":
        return 1
    if m[2]:
        return int(m[2])
    if m[3]:
        return int(m[3]) * 7
    return {"dua hari": 2, "tiga hari": 3}.get(word, 1)


def _title_from(text: str, *used: str) -> str:
    t = re.sub(r"\b(jam|pukul)(?:\s+(?:jam|pukul))+\b", r"\1", text, flags=re.I)
    for part in used:
        if part:
            t = re.sub(re.escape(part), " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:seminggu|sehari|dua hari|tiga hari|\d+ hari|\d+ minggu) sebelum(?:nya)?\b", " ", t, flags=re.I)
    t = CLOCK_PHRASE.sub(" ", t)
    t = re.sub(r"\b(?:pagi|siang|sore|malam)\b", " ", t, flags=re.I)
    t = _TITLE_FILLER.sub(" ", t)
    t = re.sub(r"\b(?:tanggal|tgl)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,:-")


OPEN_PAGE = re.compile(rf"(?:tolong |coba )?(?:buka|bukakan|tampilkan|perlihatkan|lihat|liat|lihatkan)(?: halaman)? {_AGENDA_NOUN}|"
                       r"(?:tolong |coba )?(?:buka|tampilkan|lihat)(?: halaman)? kalender (?:saya|ku|aku|agenda)")
# (a word after it is a scrap: "tutup agenda hijau" was "tutup agenda, Digidaw", and it closed the whole browser)
CLOSE_PAGE = re.compile(rf"(?:tolong |coba )?(?:tutup|sembunyikan)(?: halaman)? {_AGENDA_NOUN}(?: \w{{1,10}})?")
START_DRAFT = re.compile(r"(?:tolong |coba )?(?:tambah|tambahkan|buat|buatkan|bikin|bikinkan|catat|catatkan|masukkan|simpan)"
                         rf"(?: ke)? {_AGENDA_NOUN}(?: baru)?(?: (?:dong|ya|deh))?")


_DAY_WORDS = r"hari ini|sekarang|besok|lusa|minggu ini|minggu depan|pekan ini|(?:hari )?(?:senin|selasa|rabu|kamis|jumat|jum'at|sabtu)|hari minggu"
_DAY_ASKED = r"(?:apa )?ada apa(?: saja| aja)?|ngapain(?: saja| aja)?|ada (?:acara|kegiatan|agenda)(?: apa(?: saja| aja)?)?|jadwal(?:nya)?(?: saya|ku)?(?: apa(?: saja| aja)?)?"
DAY_QUESTION = re.compile(rf"(?:{_DAY_ASKED}) (?P<day>{_DAY_WORDS})|(?P<day2>{_DAY_WORDS})(?: saya| aku)? (?:{_DAY_ASKED})")
_WEEKDAYS = ["senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu"]


def _list_day(said: str) -> str:
    """"besok ada apa", "jadwal saya hari senin", "minggu ini ada apa"."""
    said = said.replace("jum'at", "jumat").replace("hari ", "", 1) if said.startswith("hari ") and said != "hari ini" else said.replace("jum'at", "jumat")
    if said in ("hari ini", "sekarang"):
        return list_events(0)
    if said == "besok":
        return list_events(tomorrow_offset(), start=tomorrow_offset())
    if said == "lusa":
        return list_events(tomorrow_offset() + 1, start=tomorrow_offset() + 1)
    if said in ("minggu ini", "pekan ini"):
        return list_events(7)
    if said == "minggu depan":
        return list_events(14)
    offset = (_WEEKDAYS.index(said) - datetime.date.today().weekday()) % 7
    return list_events(offset, start=offset)


def handle(text: str) -> str | None:
    """Understand an agenda sentence locally. None = not about the agenda (alarms / the AI take it)."""
    t = re.sub(r"\b(jam|pukul)(?:\s+(?:jam|pukul))+\b", r"\1", re.sub(r"\s+", " ", text.strip(" .!?")), flags=re.I)
    low = t.lower()
    if _draft:
        answer = _continue_draft(t, low)
        if answer:
            return answer
    answer = jadwal_kuliah.answer(low)
    if answer:
        return answer
    day = DAY_QUESTION.fullmatch(low)
    if day:
        return _list_day(day["day"] or day["day2"])
    if OPEN_PAGE.fullmatch(low):
        return open_agenda_page()
    if CLOSE_PAGE.fullmatch(low):
        return close_agenda_page()
    if START_DRAFT.fullmatch(low):
        return add_from_sentence("")
    # list / ask
    m = re.search(r"\bkapan (?:acara |agenda )?((?:ulang ?tahun|ultah|hari jadi|anniversary|rapat|meeting|ujian|wisuda|"
                  r"pernikahan|kondangan|arisan|seminar|deadline|konser|acara)(?: .+)?)$", low)
    if m:
        return list_events(keyword=re.sub(r"^acara ?", "", m[1]).strip(" ?") or "")
    # "kapan bayar listrik" -> the event "membayar listrik"; a general question ("kapan indonesia merdeka") goes on
    m = re.search(r"\bkapan (?:saya |aku |harus |jadwal |acara |agenda )*(.{3,}?)\s*\??$", low)
    if m:
        hit = _match_title(m[1])
        if hit:
            return list_events(keyword=hit)
    if LIST_QUESTION.search(low) and not ADD_TRIGGER_ONLY.search(low):
        if re.search(r"\bhari ini\b|\bsekarang\b", low):
            return list_events(0)
        if re.search(r"\bbesok\b", low):
            return list_events(tomorrow_offset(), start=tomorrow_offset())
        if re.search(r"\blusa\b", low):
            return list_events(tomorrow_offset() + 1, start=tomorrow_offset() + 1)
        days = 7 if re.search(r"minggu ini|pekan ini", low) else 31 if re.search(r"bulan ini", low) else \
            14 if re.search(r"minggu depan|pekan depan", low) else 60 if re.search(r"bulan depan", low) else 30
        return list_events(days)
    # delete
    m = re.search(r"\b(?:hapus|batalkan|buang|hilangkan)\b (?:acara|agenda|pengingat acara|jadwal) (.+)$", low)
    if m:
        return delete_event(m[1].strip())
    # add: "ingatkan / catat ..." or an event word with a real date ("ada rapat tanggal 15/11 jam 9")
    date, date_words = parse_date(low)
    if not date:
        return None
    asking = re.search(r"\b(?:apa|apakah|kapan|berapa|bagaimana|gimana|kenapa|siapa)\b|\?", low)
    # "ingatkan / catat ..." or a statement about an event on a day ("besok saya ada meeting jam 2")
    if not ADD_TRIGGER.search(low) and not (EVENT_WORD.search(low) and not asking):
        return None
    hhmm = parse_time(low)
    date = _still_ahead(date, hhmm, date_words)
    # "ingatkan saya besok jam 7 telepon ibu" (a clock reminder without an explicit date) stays with alarms.py
    if hhmm and not EXPLICIT_DATE.search(low) and not EVENT_WORD.search(low):
        return None
    if not EXPLICIT_DATE.search(low) and not EVENT_WORD.search(low) and not re.search(r"\b(?:minggu|bulan|tahun) depan\b", low):
        return None
    title = _title_from(t, date_words)          # the original text: names keep their capitals
    if len(title) < 3:
        return None
    return add_event(title, date.isoformat(), hhmm, yearly=bool(YEARLY_RE.search(low)), remind_days_before=_remind_days(low))


AI_TOOLS = [add_event, list_events, delete_event, open_agenda_page, close_agenda_page] + jadwal_kuliah.AI_TOOLS
