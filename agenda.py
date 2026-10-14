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
import time
import uuid
from pathlib import Path

import config
import events

T = config.USER_TITLE
ANNOUNCE = None                       # set by main (core.announce)
STATE_FILE = Path(config.BASE_DIR) / "agenda.json"
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
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    events.emit("agenda", items=snapshot())


def _load():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with _lock:
        _events[:] = data.get("events", [])
        _notified.update(data.get("notified", []))


# ───────────────────────────── dates ─────────────────────────────

def _today() -> datetime.date:
    return datetime.date.today()


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
    (re.compile(rf"\b(?:tanggal|tgl\.?|tg) {_DAY_NUM}\b(?!\s*(?:jam|menit))", re.IGNORECASE), "day_only"),
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
                offset = {"hari ini": 0, "nanti malam": 0, "malam ini": 0, "besok": 1, "lusa": 2, "besok lusa": 2}[m[1].lower()]
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
                ahead = (HARI.index(name) - today.weekday()) % 7 or 7
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
    if days == 0:
        return "hari ini"
    if days == 1:
        return "besok"
    if days == 2:
        return "lusa"
    if days < 7:
        return f"{HARI[date.weekday()].capitalize()} ini, {days} hari lagi"
    return f"{date_text(date)}, {days} hari lagi"


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
    return [{"id": e["id"], "title": _title(e), "date": d.isoformat(), "time": e.get("time", ""),
             "yearly": bool(e.get("yearly")), "days": (d - _today()).days} for d, e in upcoming(60)][:8]


def briefing_line() -> str:
    soon = upcoming(7)
    if not soon:
        return ""
    return "Agenda minggu ini: " + "; ".join(f"{e['title']} {when_text(d)}" for d, e in soon[:3]) + "."


# ───────────────────────────── skills ─────────────────────────────

def add_event(title: str, date: str, time: str = "", yearly: bool = False, remind_days_before: int = -1) -> str:
    """Save an event on a date and remind the user about it ('ingatkan saya ulang tahun ibu tanggal 20 Oktober',
    'rapat tanggal 15/11 jam 9'). date: 'YYYY-MM-DD' or Indonesian words ('20 oktober', 'besok', 'sabtu depan').
    time: 'HH:MM' (optional). yearly: repeats every year (birthdays are yearly automatically).
    remind_days_before: leave it out (-1) unless the user says how early to be reminded ("seminggu sebelumnya" = 7);
    by default the reminder comes the evening before."""
    title = re.sub(r"\s+", " ", (title or "").strip(" .,")) or "acara"
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
    before = ("" if not remind else " Saya kabari malam sebelumnya juga." if remind == 1 else
              f" Saya kabari juga {remind} hari sebelumnya.")
    at = f" jam {hhmm.replace(':', '.')}" if hhmm else ""
    return f"Siap, {T}. {_title(event)} saya catat {when_text(occurrence)}{at}.{before}{extra}"


def list_events(days: int = 30, keyword: str = "", start: int = 0) -> str:
    """The user's schedule / upcoming events ('apa saja jadwal saya', 'acara apa saja bulan ini', 'jadwal besok',
    'kapan ulang tahun ibu' with keyword). days: how far ahead (0 = today only); start: skip the first N days
    (start=1, days=1 = tomorrow only). ALWAYS call this to answer about the schedule; never guess."""
    items = [(d, e) for d, e in upcoming(366 if keyword else days, keyword) if (d - _today()).days >= start]
    if not items:
        if keyword:
            return f"Tidak ada acara '{keyword}' di agenda, {T}."
        span = ("hari ini" if days == 0 else "besok" if (start, days) == (1, 1) else "lusa" if (start, days) == (2, 2)
                else f"dalam {days} hari ke depan")
        later = upcoming(366)
        hint = f" Acara terdekat: {later[0][1]['title']} {when_text(later[0][0])}." if later else ""
        return f"Tidak ada jadwal {span}, {T}.{hint}"
    if keyword and len(items) == 1:
        d, e = items[0]
        return f"{_title(e)} {when_text(d)}" + (f" jam {e['time'].replace(':', '.')}" if e.get("time") else "") + f", {T}."
    parts = [f"{e['title']} {when_text(d)}" + (f" jam {e['time'].replace(':', '.')}" if e.get("time") else "")
             for d, e in items[:6]]
    more = f", dan {len(items) - 6} lainnya" if len(items) > 6 else ""
    return f"Ada {len(items)} acara, {T}: " + "; ".join(parts) + more + "."


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


def delete_event(keyword: str) -> str:
    """Remove an event from the agenda ('hapus acara wisuda', 'batalkan agenda rapat')."""
    key = keyword.lower().strip()
    with _lock:
        hits = [e for e in _events if key and (key in e["title"].lower() or e["id"] == key)]
        for e in hits:
            _events.remove(e)
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
    r"tambah|jadwalkan|masukkan|pasang|pengingat|reminder|agenda|di agenda|ke agenda|kalau|bahwa|ada|punya|acara|"
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
    t = text
    for part in used:
        if part:
            t = re.sub(re.escape(part), " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:seminggu|sehari|dua hari|tiga hari|\d+ hari|\d+ minggu) sebelum(?:nya)?\b", " ", t, flags=re.I)
    t = CLOCK_PHRASE.sub(" ", t)
    t = re.sub(r"\b(?:pagi|siang|sore|malam)\b", " ", t, flags=re.I)
    t = _TITLE_FILLER.sub(" ", t)
    t = re.sub(r"\b(?:tanggal|tgl)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,:-")


def handle(text: str) -> str | None:
    """Understand an agenda sentence locally. None = not about the agenda (alarms / the AI take it)."""
    t = re.sub(r"\s+", " ", text.strip(" .!?"))
    low = t.lower()
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
            return list_events(1, start=1)
        if re.search(r"\blusa\b", low):
            return list_events(2, start=2)
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
    # "ingatkan saya besok jam 7 telepon ibu" (a clock reminder without an explicit date) stays with alarms.py
    if hhmm and not EXPLICIT_DATE.search(low) and not EVENT_WORD.search(low):
        return None
    if not EXPLICIT_DATE.search(low) and not EVENT_WORD.search(low) and not re.search(r"\b(?:minggu|bulan|tahun) depan\b", low):
        return None
    title = _title_from(t, date_words)          # the original text: names keep their capitals
    if len(title) < 3:
        return None
    return add_event(title, date.isoformat(), hhmm, yearly=bool(YEARLY_RE.search(low)), remind_days_before=_remind_days(low))


AI_TOOLS = [add_event, list_events, delete_event]
