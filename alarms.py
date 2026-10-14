"""
Timers, alarms and reminders that behave like a phone's clock app.

- Survive restarts (alarms.json). Alarms/reminders missed while Digidaw was off are reported at start.
- Alarms ring (Windows alarm sound, looping) until "stop" / "matikan alarm" / Esc in the HUD / Ctrl+Alt+D,
  or "tunda 5 menit" (snooze). They stop by themselves after ALARM_RING_SECONDS.
- Repeating alarms: "setiap hari", "hari kerja", "akhir pekan", "setiap senin dan kamis".
- Indonesian time is understood locally (no AI round-trip): "timer 1 jam 30 menit", "alarm jam setengah 7 pagi",
  "bangunkan saya jam 5.45", "ingatkan saya 20 menit lagi untuk angkat jemuran", "ingatkan besok jam 8 rapat".
"""
import os
import re
import json
import time
import uuid
import datetime
import threading
from pathlib import Path

import config
import events
import oscompat

if oscompat.IS_WINDOWS:
    import winsound
else:
    import linux_desktop as ld

T = config.USER_TITLE
ANNOUNCE = None          # set by main (core.announce)
STATE_FILE = Path(config.BASE_DIR) / "alarms.json"
SOUNDS = {"alarm": "Alarm01.wav", "timer": "Alarm03.wav", "reminder": "Alarm02.wav"}
DAYS = ["senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu"]
DAY_ALIASES = {"jum'at": "jumat", "jumaat": "jumat", "ahad": "minggu"}

_items: list[dict] = []          # {id, kind, label, due (ts), repeat: [weekday ints] | None, time "HH:MM"}
_lock = threading.RLock()
_ringing: dict | None = None
_stop_ring = threading.Event()


# ───────────────────────────── persistence ─────────────────────────────

def _save():
    keep = [{k: v for k, v in i.items() if k != "action"} for i in _items if not i.get("action")]
    try:
        STATE_FILE.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _load() -> list[dict]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _changed():
    _save()
    events.emit("timers", timers=snapshot())


# ───────────────────────────── time helpers ─────────────────────────────

def _next_on(days: list[int], hour: int, minute: int, after: datetime.datetime) -> datetime.datetime:
    for offset in range(0, 8):
        day = after + datetime.timedelta(days=offset)
        when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when > after and when.weekday() in days:
            return when
    return after + datetime.timedelta(days=1)


def _repeat_text(days) -> str:
    if not days:
        return ""
    days = sorted(days)
    if days == list(range(7)):
        return "setiap hari"
    if days == list(range(5)):
        return "setiap hari kerja"
    if days == [5, 6]:
        return "setiap akhir pekan"
    return "setiap " + ", ".join(DAYS[d] for d in days)


def _fmt_left(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    parts = ([f"{h} jam"] if h else []) + ([f"{m} menit"] if m else []) + ([f"{s} detik"] if s and not h else [])
    return " ".join(parts) or "beberapa detik"


def _when_text(due: float) -> str:
    when = datetime.datetime.fromtimestamp(due)
    today = datetime.date.today()
    if when.date() == today:
        return f"pukul {when:%H.%M}"
    if when.date() == today + datetime.timedelta(days=1):
        return f"besok pukul {when:%H.%M}"
    return f"{DAYS[when.weekday()].capitalize()} {when:%d/%m} pukul {when:%H.%M}"


# ───────────────────────────── scheduling ─────────────────────────────

def add(kind: str, label: str, due: datetime.datetime, repeat=None, action=None) -> dict:
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label.strip(), "due": due.timestamp(),
            "repeat": sorted(set(repeat)) if repeat else None, "time": f"{due:%H:%M}", "created": time.time()}
    if action:
        item["action"] = action
    with _lock:
        _items.append(item)
        _items.sort(key=lambda i: i["due"])
    _changed()
    return item


def schedule_action(seconds: float, label: str, action) -> dict:
    """A one-off background action (sleep timer etc.); not persisted."""
    return add("action", label, datetime.datetime.now() + datetime.timedelta(seconds=seconds), action=action)


def _loop():
    while True:
        time.sleep(0.5)
        now = time.time()
        due = []
        with _lock:
            for item in list(_items):
                if item["due"] <= now:
                    due.append(item)
                    if item.get("repeat"):
                        hh, mm = map(int, item["time"].split(":"))
                        item["due"] = _next_on(item["repeat"], hh, mm, datetime.datetime.now()).timestamp()
                    else:
                        _items.remove(item)
            if due:
                _items.sort(key=lambda i: i["due"])
        if due:
            _changed()
        for item in due:
            threading.Thread(target=_fire, args=(item,), daemon=True, name="alarm-fire").start()


def _fire(item: dict):
    if item.get("action"):
        try:
            result = item["action"]()
        except Exception as e:
            result = f"gagal: {e}"
        _say(f"{item['label']}. {result}" if isinstance(result, str) and result else item["label"])
        return
    kind, label = item["kind"], item["label"]
    if kind == "timer":
        text = f"Timer {label} selesai, {T}." if label else f"Waktu habis, {T}. Timer sudah selesai."
    elif kind == "alarm":
        text = f"Alarm {item['time'].replace(':', '.')}, {T}." + (f" {label}." if label else "")
    else:
        text = f"Pengingat, {T}: {label}."
    _ring(item, text)


def _say(text: str, level: str = "reminder"):
    if ANNOUNCE:
        ANNOUNCE(text, level)
    else:
        print(f"⏰ {text}")


def _ring(item: dict, text: str):
    """Speak, then ring until stopped / snoozed / ALARM_RING_SECONDS."""
    global _ringing
    _stop_ring.set()                       # another alarm was ringing: it gives way
    time.sleep(0.2)
    _stop_ring.clear()
    _ringing = item
    info = {"ringing": True, "id": item["id"], "kind": item["kind"], "label": item["label"], "text": text,
            "time": item.get("time")}
    events.state["alarm"] = info
    events.emit("alarm", **info)
    try:
        import hud_server
        if config.ALARM_SHOW_HUD:
            hud_server.open_window()        # so the STOP / TUNDA buttons are right there
    except Exception:
        pass
    _say(text)
    seconds = config.ALARM_RING_SECONDS if item["kind"] == "alarm" else min(config.ALARM_RING_SECONDS, 20)
    sound = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media", SOUNDS.get(item["kind"], SOUNDS["alarm"]))
    try:
        import media
        media.duck()
    except Exception:
        media = None
    looping = None
    if oscompat.IS_WINDOWS:
        try:
            winsound.PlaySound(sound, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        except RuntimeError:
            pass
    else:
        looping = ld.LoopingSound(ld.sound_for(item["kind"]))
    _stop_ring.wait(seconds)
    if looping:
        looping.stop()
    else:
        winsound.PlaySound(None, 0)
    if media:
        media.unduck()
    if _ringing is item:
        _ringing = None
        events.state["alarm"] = {"ringing": False}
        events.emit("alarm", ringing=False, id=item["id"])


def is_ringing() -> bool:
    return _ringing is not None


def start():
    """Restore saved alarms; report the ones missed while Digidaw was off."""
    now = datetime.datetime.now()
    missed = []
    with _lock:
        for item in _load():
            if item["due"] > now.timestamp():
                _items.append(item)
            elif item.get("repeat"):
                hh, mm = map(int, item["time"].split(":"))
                item["due"] = _next_on(item["repeat"], hh, mm, now).timestamp()
                _items.append(item)
                if now.timestamp() - item["due"] < 6 * 3600:
                    missed.append(item)
            else:
                missed.append(item)
        _items.sort(key=lambda i: i["due"])
    _changed()
    threading.Thread(target=_loop, daemon=True, name="alarms").start()
    recent = [m for m in missed if m["kind"] != "timer"]
    if recent:
        names = "; ".join(f"{m['label'] or 'alarm'} ({m['time'].replace(':', '.')})" for m in recent[:3])
        threading.Timer(8, _say, args=(f"Saat Digidaw mati ada pengingat yang terlewat, {T}: {names}.", "reminder")).start()


def snapshot() -> list[dict]:
    with _lock:
        return [{"id": i["id"], "kind": i["kind"], "label": i["label"] or ("Timer" if i["kind"] == "timer" else
                 "Alarm" if i["kind"] == "alarm" else "Pengingat"), "message": i["label"] or i["kind"],
                 "due": datetime.datetime.fromtimestamp(i["due"]).isoformat(timespec="seconds"),
                 "repeat": _repeat_text(i.get("repeat"))} for i in _items]


# ───────────────────────────── natural language (Indonesian) ─────────────────────────────

_UNITS = {"nol": 0, "satu": 1, "se": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7,
          "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11}
_NUM_WORDS = r"(?:(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan) puluh(?: (?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan))?|" \
             r"(?:dua|tiga|empat|lima|enam|tujuh|delapan|sembilan) belas|sepuluh|sebelas|nol|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan)"
_NUM = rf"(?:\d+(?:[.,]\d+)?|{_NUM_WORDS})"


def _number(text: str) -> float:
    text = text.strip().lower().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        pass
    m = re.fullmatch(r"(\w+) puluh(?: (\w+))?", text)
    if m:
        return _UNITS.get(m.group(1), 0) * 10 + _UNITS.get(m.group(2) or "nol", 0)
    m = re.fullmatch(r"(\w+) belas", text)
    if m:
        return 10 + _UNITS.get(m.group(1), 0)
    return _UNITS.get(text, 0)


def parse_duration(text: str) -> float | None:
    """Seconds in '1 jam 30 menit', 'setengah jam', 'satu setengah jam', '90 detik', 'seperempat jam'."""
    t = text.lower()
    total, found = 0.0, False
    m = re.search(rf"({_NUM}) setengah jam", t)
    if m:
        total += (_number(m.group(1)) + 0.5) * 3600; found = True; t = t.replace(m.group(0), " ")
    if re.search(r"\bsetengah jam\b", t):
        total += 1800; found = True; t = re.sub(r"\bsetengah jam\b", " ", t)
    if re.search(r"\bseperempat jam\b", t):
        total += 900; found = True; t = re.sub(r"\bseperempat jam\b", " ", t)
    for pattern, factor in ((rf"({_NUM}) ?jam\b", 3600), (rf"({_NUM}) ?(?:menit|mnt)\b", 60),
                            (rf"({_NUM}) ?(?:detik|dtk)\b", 1)):
        m = re.search(pattern, t)
        if m:
            total += _number(m.group(1)) * factor; found = True
    return total if found and total > 0 else None


def parse_clock(text: str):
    """(hour, minute) from 'jam 6', 'jam 6 pagi', 'jam 06.30', 'pukul 19:15', 'jam setengah 7 malam',
    'jam 7 lewat 15', 'jam 8 kurang seperempat'. None if no time."""
    t = text.lower()
    m = None
    for cand in re.finditer(rf"(?:jam|pukul)\s+(setengah\s+)?(\d{{1,2}}|{_NUM_WORDS})(?:\s*[.:]\s*(\d{{2}}))?"
                            rf"(?:\s+(lewat|lebih|kurang)\s+(seperempat|{_NUM})(?:\s+menit)?)?"
                            r"(?:\s+(pagi|siang|sore|petang|malam|subuh|dini hari))?", t):
        before = t[:cand.start()].rstrip()
        if cand.group(0).startswith("jam") and re.search(rf"(?:{_NUM}|setengah|seperempat)$", before):
            continue            # "1 jam 5 menit" is a duration, not "at 5 o'clock"
        if re.match(r"\s*(?:menit|detik)\b", t[cand.end():]):
            continue
        m = cand
        break
    if not m:
        m2 = None
        for cand in re.finditer(r"\b(\d{1,2})[.:](\d{2})\b(?:\s+(pagi|siang|sore|petang|malam|subuh))?", t):
            if re.match(r"\s*(?:menit|detik|jam|mnt|dtk)\b", t[cand.end():]):
                continue           # "0.02 menit", "1.30 jam" are durations
            m2 = cand
            break
        if not m2:
            return None
        hour, minute, part = int(m2.group(1)), int(m2.group(2)), m2.group(3)
    else:
        hour = int(_number(m.group(2)))
        minute = int(m.group(3)) if m.group(3) else 0
        if m.group(1):                                   # "setengah 7" = 6:30
            hour, minute = hour - 1, 30
        if m.group(4):
            delta = 15 if m.group(5) == "seperempat" else int(_number(m.group(5)))
            if m.group(4) == "kurang":
                hour, minute = (hour - 1, 60 - delta) if minute == 0 else (hour, minute - delta)
            else:
                minute += delta
        part = m.group(6)
    if part in ("siang",) and hour < 11:
        hour += 12
    elif part in ("sore", "petang", "malam") and hour < 12:
        hour += 12
    elif part in ("pagi", "subuh", "dini hari") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_repeat(text: str):
    t = text.lower()
    for alias, name in DAY_ALIASES.items():
        t = t.replace(alias, name)
    if re.search(r"(?:setiap|tiap) hari kerja|senin (?:sampai|hingga|-) jumat|weekday", t):
        return list(range(5))
    if re.search(r"(?:setiap|tiap) (?:akhir pekan|weekend|sabtu (?:dan|&) minggu)", t):
        return [5, 6]
    if re.search(r"(?:setiap|tiap) hari\b|tiap pagi|setiap pagi|setiap malam|tiap malam|daily", t):
        return list(range(7))
    m = re.search(r"(?:setiap|tiap) ((?:(?:hari )?(?:senin|selasa|rabu|kamis|jumat|sabtu|minggu)(?:,|\s+dan\s+|\s+)?)+)", t)
    if m:
        return sorted({DAYS.index(d) for d in re.findall(r"senin|selasa|rabu|kamis|jumat|sabtu|minggu", m.group(1))})
    return None


def _day_offset(text: str, hour: int, minute: int) -> datetime.datetime:
    now = datetime.datetime.now()
    t = text.lower()
    for alias, name in DAY_ALIASES.items():
        t = t.replace(alias, name)
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.search(r"\blusa\b", t):
        return base + datetime.timedelta(days=2)
    if re.search(r"\bbesok\b", t):
        return base + datetime.timedelta(days=1)
    m = re.search(r"(?:hari |pada )?(senin|selasa|rabu|kamis|jumat|sabtu|minggu)\b", t)
    if m and not re.search(r"setiap|tiap", t):
        return _next_on([DAYS.index(m.group(1))], hour, minute, now)
    return base if base > now else base + datetime.timedelta(days=1)


_FILLER = re.compile(r"\b(?:tolong|dong|ya|saya|aku|nanti|lagi|dalam|selama|setelah|sesudah|untuk|buat|agar|supaya|"
                     r"pada|di|jam|pukul|pagi|siang|sore|petang|malam|subuh|besok|lusa|hari ini|setiap|tiap|hari kerja|"
                     r"akhir pekan|ingatkan|ingetin|pengingat|alarm|timer|set|pasang|atur|bikin|buat|bangunkan|"
                     r"bangunin|kan|mengingatkan|setel|stel|tolong|setengah|seperempat|lewat|lebih|kurang|sekarang|hitung mundur)\b",
                     re.IGNORECASE)


def _label(text: str, *remove: str) -> str:
    t = text
    for r in remove:
        if r:
            t = t.replace(r, " ")
    m = re.search(r"\b(?:untuk|buat|agar|supaya|bahwa|kalau)\s+(.+)$", t, re.IGNORECASE)
    if m:
        return m.group(1).strip(" .,")
    t = re.sub(rf"\b{_NUM}\s+setengah\s+jam\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:setiap|tiap)\s+(?:hari(?:\s+kerja)?|akhir pekan|pagi|malam)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\bhari\s+(?=senin|selasa|rabu|kamis|jumat|sabtu|minggu)", " ", t, flags=re.IGNORECASE)
    t = re.sub(rf"\b{_NUM}\s*(?:jam|menit|detik)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:setengah|seperempat)\s+jam\b", " ", t, flags=re.IGNORECASE)
    t = _FILLER.sub(" ", t)
    t = re.sub(r"\b(?:senin|selasa|rabu|kamis|jumat|sabtu|minggu|dan)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{1,2}[.:]\d{2}\b|\b\d+\b", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .,")


def handle(text: str) -> str | None:
    """Understand a timer / alarm / reminder sentence locally. None = not one of those."""
    t = text.lower().strip(" .!?")
    is_timer = re.search(r"\b(?:timer|hitung mundur|countdown)\b", t)
    is_alarm = re.search(r"\b(?:alarm|bangunkan|bangunin|weker)\b", t)
    is_reminder = re.search(r"\b(?:ingatkan|ingetin|pengingat|reminder)\b", t)
    if not (is_timer or is_alarm or is_reminder):
        return None
    if re.search(r"\b(?:batalkan|hapus|matikan semua|stop semua|cancel)\b", t) or \
            re.search(r"\b(?:apa saja|daftar|sisa waktu|berapa lama lagi|kapan)\b", t):
        return None                                   # handled by cancel / list routes
    repeat = parse_repeat(t)
    clock = parse_clock(t)
    duration = None if clock else parse_duration(t)
    if is_timer and duration:
        label = _label(text)
        return set_timer(duration / 60, label)
    if clock:
        hour, minute = clock
        if is_alarm or (is_timer and not is_reminder):
            return _set_alarm(f"{hour:02d}:{minute:02d}", _label(text), repeat_days=repeat, when_text=t)
        return _reminder_at(hour, minute, _label(text), t, repeat)
    if duration:
        label = _label(text)
        if is_alarm:
            return set_timer(duration / 60, label, kind="alarm")
        return set_timer(duration / 60, label, kind="reminder")
    return None


# ───────────────────────────── skills ─────────────────────────────

def set_timer(minutes: float, label: str = "", kind: str = "timer", message: str = "") -> str:
    """Countdown timer: 'timer 10 menit', 'timer 1 jam 30 menit untuk masak'. minutes may be fractional
    (0.5 = 30 seconds). label = what it's for (optional)."""
    label = label or message
    minutes = float(minutes)
    if minutes <= 0:
        return f"Durasi timer harus lebih dari nol, {T}."
    due = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    item = add(kind if kind in ("timer", "alarm", "reminder") else "timer", label, due)
    what = {"timer": "Timer", "alarm": "Alarm", "reminder": "Pengingat"}[item["kind"]]
    extra = f" untuk {label}" if label else ""
    return f"{what} {_fmt_left(minutes * 60)}{extra} diset, berbunyi {_when_text(item['due'])}, {T}."


def set_alarm(time_hhmm: str, label: str = "", repeat: str = "") -> str:
    """Alarm at a clock time (24h 'HH:MM'): 'alarm jam 6 pagi', 'bangunkan saya jam 5.30'. repeat:
    '' (once), 'setiap hari', 'hari kerja', 'akhir pekan', or day names ('senin kamis'). label = optional note."""
    return _set_alarm(time_hhmm, label, repeat)


def _set_alarm(time_hhmm: str, label: str = "", repeat: str = "", repeat_days: list | None = None,
               when_text: str = "") -> str:
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", time_hhmm or "")
    if not m:
        return f"Format jam alarm harus HH:MM, {T}."
    hour, minute = int(m.group(1)), int(m.group(2))
    days = repeat_days if repeat_days is not None else (parse_repeat("setiap " + repeat) if repeat else None)
    if days:
        due = _next_on(days, hour, minute, datetime.datetime.now())
    else:
        due = _day_offset(when_text, hour, minute)
    item = add("alarm", label, due, repeat=days)
    rep = f" {_repeat_text(days)}" if days else ""
    left = _fmt_left(item["due"] - time.time())
    return (f"Alarm {hour:02d}.{minute:02d}{rep}{' untuk ' + label if label else ''} diset, {T}. "
            f"Berbunyi {_when_text(item['due'])}, {left} lagi.")


def _reminder_at(hour: int, minute: int, label: str, text: str, repeat=None) -> str:
    due = _next_on(repeat, hour, minute, datetime.datetime.now()) if repeat else _day_offset(text, hour, minute)
    item = add("reminder", label or "pengingat", due, repeat=repeat)
    rep = f" {_repeat_text(repeat)}" if repeat else ""
    return f"Baik, {T}. Saya ingatkan '{item['label']}' {_when_text(item['due'])}{rep}."


def set_reminder_at(time_hhmm: str, message: str, date: str = "") -> str:
    """Reminder at a clock time (24h 'HH:MM') with a message; date: '' (next occurrence), 'besok',
    'lusa', a day name, or 'setiap hari'. Use for 'ingatkan saya jam 7 malam untuk telepon ibu'."""
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", time_hhmm or "")
    if not m:
        return f"Format waktu harus HH:MM, {T}."
    return _reminder_at(int(m.group(1)), int(m.group(2)), message, date, parse_repeat(date) if date else None)


def list_timers() -> str:
    """List active timers, alarms and reminders with their remaining time ('timer apa saja', 'alarm saya')."""
    with _lock:
        items = [i for i in _items if i["kind"] != "action"] + [i for i in _items if i["kind"] == "action"]
    if not items:
        return f"Tidak ada timer atau alarm yang aktif, {T}."
    parts = []
    for i in items[:8]:
        name = {"timer": "Timer", "alarm": "Alarm", "reminder": "Pengingat", "action": ""}[i["kind"]]
        what = f"{name} {i['label']}".strip() if i["kind"] != "action" else i["label"]
        if i["kind"] == "timer":
            parts.append(f"{what}: sisa {_fmt_left(i['due'] - time.time())}")
        else:
            parts.append(f"{what} {_when_text(i['due'])}{' ' + _repeat_text(i['repeat']) if i.get('repeat') else ''}")
    return "; ".join(parts) + f", {T}."


def cancel_timers(keyword: str = "") -> str:
    """Cancel timers / alarms / reminders: keyword matches the label, the time ('06.00') or the kind
    ('alarm', 'timer'); empty = all ('batalkan semua timer', 'hapus alarm jam 6')."""
    key = keyword.lower().strip()
    clock = parse_clock(key) if key else None
    with _lock:
        def matches(i):
            if not key or key in ("semua", "semuanya", "all"):
                return True
            if clock and i["time"] == f"{clock[0]:02d}:{clock[1]:02d}":
                return True
            kind_words = {"timer": "timer", "alarm": "alarm", "pengingat": "reminder", "reminder": "reminder"}
            if key in kind_words and i["kind"] == kind_words[key]:
                return True
            return bool(i["label"]) and (key in i["label"].lower() or i["label"].lower() in key)
        targets = [i for i in _items if matches(i)]
        for i in targets:
            _items.remove(i)
    if targets:
        _changed()
        names = ", ".join((i["label"] or i["kind"]) + (f" ({i['time'].replace(':', '.')})" if i["kind"] == "alarm" else "")
                          for i in targets[:3])
        return f"{len(targets)} dibatalkan: {names}, {T}."
    if key in ("", "semua", "semuanya", "all", "timer", "alarm", "pengingat", "reminder"):
        what = {"timer": "timer", "alarm": "alarm", "pengingat": "pengingat", "reminder": "pengingat"}.get(key, "timer atau alarm")
        return f"Tidak ada {what} yang sedang berjalan, {T}."
    return f"Tidak ada timer atau alarm yang cocok dengan '{keyword}', {T}."


def stop_alarm() -> str:
    """Stop the alarm / timer that is ringing ('stop', 'matikan alarm', 'oke sudah bangun')."""
    if not _ringing:
        return f"Tidak ada alarm yang sedang berbunyi, {T}."
    _stop_ring.set()
    return f"Alarm dimatikan, {T}."


def snooze_alarm(minutes: float = 5) -> str:
    """Snooze the ringing alarm ('tunda 5 menit', 'snooze', 'nanti dulu')."""
    item = _ringing
    if not item:
        return f"Tidak ada alarm yang sedang berbunyi, {T}."
    _stop_ring.set()
    minutes = float(minutes or config.ALARM_SNOOZE_MINUTES)
    due = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    add(item["kind"] if item["kind"] != "timer" else "reminder", item["label"] or "alarm", due)
    return f"Alarm ditunda {minutes:g} menit, berbunyi lagi pukul {due:%H.%M}, {T}."


AI_TOOLS = [set_timer, set_alarm, set_reminder_at, list_timers, cancel_timers, stop_alarm, snooze_alarm]
