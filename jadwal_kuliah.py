"""
The user's weekly schedule: their own script (~/myskrip/pengingat_jadwal.sh) keeps it in a TSV file, one line per item:
id, day (0 = every day, 1-7 = Monday-Sunday), HH:MM, text ("Pancasila (Ruang B2, 2 SKS)"), urgency. Digidaw reads that file,
never writes it: the script stays the one place to change it, and a change counts at once.

"besok kuliah apa", "Pancasila jam berapa", the agenda's answers and page, the morning summary, and a reminder before each
one (the script only shows a desktop notification at the very minute, which nobody sees with the laptop closed).
"""
import datetime
import re
import time

import config

T = config.USER_TITLE
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
CLASS_RE = re.compile(r"\b(?:sks|ruang|kelas|kuliah|dosen|praktikum)\b", re.IGNORECASE)
CLASS_WORD = r"(?:kuliah|kelas|matkul|mata kuliah|praktikum)"
ADDING_RE = re.compile(r"\b(?:ingatkan|ingetin|ingatin|catat|catatkan|simpan|tambahkan|tambah|jadwalkan|masukkan|pasang|hapus|batalkan|"
                       r"buang|buat|bikin)\b")
ASKING_RE = re.compile(r"\b(?:apa|apakah|ada|kapan|jam berapa|pukul berapa|berapa|mana|cek|lihat|jadwal|semua|daftar)\b")
QUIET_HOURS = (6, 22)     # no reminder for an item before this hour or from that hour on (a test item at 02:46 stays silent)


def entries() -> list[dict]:
    """Every item of the file, by day and time. No file, or a line the script wouldn't write either: left out."""
    try:
        lines = config.WEEKLY_SCHEDULE_FILE.expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    items = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 4 or not parts[1].strip().isdigit() or not 0 <= int(parts[1]) <= 7 \
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", parts[2].strip()) or not parts[3].strip():
            continue
        text = parts[3].strip()
        m = re.fullmatch(r"(.+?)\s*\((.+)\)", text)
        detail = m[2].strip() if m else ""
        room = re.search(r"\bruang\s+[^,)]+", detail or text, re.IGNORECASE)
        items.append({"id": parts[0].strip(), "day": int(parts[1]), "time": parts[2].strip(), "text": text,
                      "name": m[1].strip() if m else text, "detail": detail, "room": room[0].strip() if room else "",
                      "urgency": parts[4].strip() if len(parts) > 4 else "normal", "is_class": bool(CLASS_RE.search(text))})
    return sorted(items, key=lambda e: (e["day"] or 8, e["time"]))


def on_date(date: datetime.date, classes_only: bool = False, items: list[dict] | None = None) -> list[dict]:
    """The items of that day, by time."""
    day = date.isoweekday()
    found = [e for e in (entries() if items is None else items)
             if e["day"] in (0, day) and (e["is_class"] or not classes_only)]
    return sorted(found, key=lambda e: e["time"])


def _at(date: datetime.date, item: dict) -> datetime.datetime:
    return datetime.datetime.combine(date, datetime.time(*map(int, item["time"].split(":"))))


def upcoming(days: int = 7, classes_only: bool = False) -> list[tuple[datetime.date, dict]]:
    """(date, item) from now on: what is over today is left out."""
    now = datetime.datetime.now()
    items = entries()
    out = []
    for ahead in range(days):
        date = now.date() + datetime.timedelta(days=ahead)
        out += [(date, e) for e in on_date(date, classes_only, items) if _at(date, e) > now]
    return out


def _said(item: dict) -> str:
    return f"{item['name']} jam {item['time'].replace(':', '.')}" + (f" di {item['room']}" if item["room"] else "")


def _day_said(date: datetime.date, today: datetime.date | None = None) -> str:
    import agenda
    ahead, tomorrow = (date - (today or datetime.date.today())).days, agenda.tomorrow_offset()
    return {0: "Hari ini", tomorrow: "Besok", tomorrow + 1: "Lusa"}.get(ahead, HARI[date.weekday()])


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f", lalu {parts[-1]}"


def day_sentence(date: datetime.date, classes_only: bool = True) -> str:
    """"Besok kuliah Kalkulus jam 09.00 di Ruang B2." For today, what's already over isn't said."""
    today = datetime.date.today()
    items = on_date(date, classes_only)
    word = "kuliah" if items and all(e["is_class"] for e in items) else "ada"
    if date == today:
        now = datetime.datetime.now()
        left = [e for e in items if _at(date, e) > now]
        if items and not left:
            return f"Hari ini tidak ada {'kuliah' if classes_only else 'jadwal'} lagi, {T}."
        items = left
    if not items:
        return f"{_day_said(date, today)} tidak ada {'kuliah' if classes_only else 'jadwal'}, {T}."
    return f"{_day_said(date, today)} {word} {_join([_said(e) for e in items])}, {T}."


def next_sentence(classes_only: bool = True) -> str:
    coming = upcoming(8, classes_only)
    if not coming:
        return f"Tidak ada {'kuliah' if classes_only else 'jadwal'} dalam seminggu ini, {T}."
    date, item = coming[0]
    day = _day_said(date).lower() if (date - datetime.date.today()).days <= 2 else f"hari {HARI[date.weekday()]}"
    return f"{'Kuliah' if item['is_class'] else 'Jadwal'} berikutnya {item['name']}, {day} jam {item['time'].replace(':', '.')}" \
           + (f" di {item['room']}" if item["room"] else "") + f", {T}."


def week_sentence(classes_only: bool = True) -> str:
    items = [e for e in entries() if e["is_class"] or not classes_only]
    if not items:
        return f"Jadwal {'kuliah ' if classes_only else ''}tidak ada, {T}."
    days = []
    for number, name in enumerate(HARI, 1):
        today = [e for e in items if e["day"] in (0, number)]
        if today:
            days.append(f"{name}: " + ", ".join(f"{e['name']} {e['time'].replace(':', '.')}" for e in today))
    word = "Kuliah" if all(e["is_class"] for e in items) or classes_only else "Jadwal"
    return f"{word} minggu ini, {T}. " + ". ".join(days) + "."


def _course(low: str) -> list[dict]:
    """The item this sentence names ("pancasila jam berapa")."""
    found = []
    for item in entries():
        words = [w for w in re.findall(r"\w+", item["name"].lower()) if len(w) >= 4]
        if words and all(w in low for w in words[:2]):
            found.append(item)
    return found


def answer(low: str) -> str | None:
    """A question about the weekly schedule, answered. None: it isn't one (a statement, or nothing to do with it)."""
    if ADDING_RE.search(low) or re.search(r"\b(?:mode|rutinitas|protokol)\b", low):
        return None
    if not entries():                 # (nothing saved: still a question, "ada kuliah hari ini" must not become an event called kuliah)
        asks = re.search(rf"\b{CLASS_WORD}\b", low) and ASKING_RE.search(low) and not re.search(r"\b(?:jam|pukul) \d", low)
        return f"Jadwal kuliah sedang kosong, {T}. Isinya diatur lewat skrip pengingat jadwal." if asks else None
    asks_time = re.search(r"\b(?:jam berapa|pukul berapa|kapan|ruang berapa|ruang mana|di ruang|di mana|dimana)\b", low)
    named = _course(low)
    if named and asks_time:
        days = " dan ".join(HARI[e["day"] - 1] if e["day"] else "setiap hari" for e in named)
        first = named[0]
        return f"{first['name']} {days} jam {first['time'].replace(':', '.')}" + (f" di {first['room']}" if first["room"] else "") + f", {T}."
    if not re.search(rf"\b{CLASS_WORD}\b", low):
        return None
    if re.search(r"\b(?:jam|pukul) \d", low):           # "besok ada kuliah jam 2": news, for the agenda
        return None
    today = datetime.date.today()
    if re.search(r"\b(?:berikutnya|selanjutnya|habis ini|sesudah ini|setelah ini|terdekat|lagi kapan)\b", low):
        return next_sentence()
    if re.search(r"\bminggu (?:ini|depan)\b|\bseminggu\b|\bpekan ini\b|\bsemua\b", low):
        return week_sentence()
    import agenda
    if re.search(r"\bbesok\b", low):
        return day_sentence(today + datetime.timedelta(days=agenda.tomorrow_offset()))
    if re.search(r"\blusa\b", low):
        return day_sentence(today + datetime.timedelta(days=agenda.tomorrow_offset() + 1))
    m = re.search(r"\b(senin|selasa|rabu|kamis|jumat|jum'at|sabtu|hari minggu)\b", low)
    if m:
        name = m[1].replace("hari ", "").replace("jum'at", "jumat").capitalize()
        return day_sentence(today + datetime.timedelta(days=(HARI.index(name) - today.weekday()) % 7 or (7 if agenda.tomorrow_offset() else 0)))
    if re.search(r"\b(?:hari ini|sekarang)\b", low):
        return day_sentence(today)
    if re.search(r"\bjadwal\b", low):
        return week_sentence()
    if ASKING_RE.search(low):
        return day_sentence(today)
    return None


def agenda_sentence(days: int, start: int = 0) -> str:
    """What the weekly schedule adds to an answer about the agenda: the day asked about, or today and tomorrow."""
    if not entries():
        return ""
    today = datetime.date.today()
    if days <= start + 1 and (start or not days):       # one day: today (0), tomorrow (1, 1), the day after (2, 2)
        date = today + datetime.timedelta(days=start)
        return day_sentence(date, classes_only=False) if on_date(date) else ""
    if days in (7, 14):                                 # "minggu ini", "minggu depan": the week itself
        return week_sentence(classes_only=False)
    lines = [day_sentence(d, classes_only=False) for d in (today, today + datetime.timedelta(days=1)) if on_date(d)]
    if lines:
        return " ".join(lines)
    return next_sentence(classes_only=False)


def briefing_line() -> str:
    """For the morning summary."""
    return day_sentence(datetime.date.today()) if on_date(datetime.date.today(), classes_only=True) else ""


def due_reminders(said: set) -> list[tuple[str, str]]:
    """(key, sentence) for the items starting within WEEKLY_REMIND_MINUTES and not announced yet."""
    minutes = config.WEEKLY_REMIND_MINUTES
    if minutes <= 0:
        return []
    now = datetime.datetime.now()
    out = []
    for item in on_date(now.date()):
        if item["urgency"] == "low" or not QUIET_HOURS[0] <= int(item["time"][:2]) < QUIET_HOURS[1]:
            continue
        left = (_at(now.date(), item) - now).total_seconds() / 60
        key = f"weekly|{item['id']}|{now.date().isoformat()}"
        if 0 < left <= minutes and key not in said:
            what = f"kuliah {_said(item)}" if item["is_class"] else _said(item)
            out.append((key, f"{T}, {max(1, round(left))} menit lagi {what}."))
    return out


def list_classes(day: str = "") -> str:
    """The user's weekly class schedule ('kuliah hari ini apa', 'besok kuliah jam berapa', 'jadwal kuliah senin').
    day: 'hari ini', 'besok', 'lusa', a weekday name, or empty for the whole week."""
    return answer(f"jadwal kuliah {day}".strip().lower()) or f"Jadwal kuliah tidak ditemukan, {T}."


def open_editor() -> str:
    """Change the weekly schedule in the user's own script."""
    import pc_control
    return pc_control.run_script("pengingat jadwal")


AI_TOOLS = [list_classes]
