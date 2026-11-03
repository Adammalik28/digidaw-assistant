"""
How well Digidaw hears and answers, measured every day: so a change can be judged by numbers instead of guesses
("Digidaw, laporan kinerja hari ini" / "minggu ini"; a summary also shows on the HUD).

Counted (no words, no names — only how often and how fast):
- calls: a chime ("ting") and what came of it: a command, nothing (back to standby), or taken back (the laptop heard
  the name, Google didn't); a call with the command in one breath ("Digidaw, buka Firefox")
- phrases dropped: after a doubtful call ("digital"), Digidaw's own voice coming back, not addressed at all
- speed: speech -> text, processing per route (fast / local AI / Gemini), the first sound of the answer
- trouble: Gemini busy, the backup voice, the lamp not answering, the local AI handing over to Gemini

Kept per day in logs/metrics/<date>.json (readable for this user only), KEEP_DAYS days.
"""
import datetime
import threading
import time

import config
import storage

DIR = config.LOG_DIR / "metrics"
KEEP_DAYS = 30
MAX_SAMPLES = 400                 # timing values kept per kind per day (the oldest go first)
SAVE_AFTER = 60                   # seconds: changes are written at most this often

_lock = threading.Lock()
_day: dict = {}
_dirty = {"since": 0.0}
_timer: threading.Timer | None = None


def _today() -> dict:
    """Today's record (loaded once; a new one after midnight). Call with _lock held."""
    day = datetime.date.today().isoformat()
    if _day.get("day") != day:
        if _day.get("day"):
            _write_locked()
        _day.clear()
        loaded = storage.load_json(DIR / f"{day}.json")
        _day.update(loaded if isinstance(loaded, dict) and loaded.get("day") == day
                    else {"day": day, "counts": {}, "times": {}})
    return _day


def count(name: str, n: int = 1):
    with _lock:
        counts = _today()["counts"]
        counts[name] = counts.get(name, 0) + n
    _changed()


def timing(name: str, seconds: float):
    if seconds is None or seconds < 0:
        return
    with _lock:
        values = _today()["times"].setdefault(name, [])
        values.append(round(float(seconds), 2))
        del values[:-MAX_SAMPLES]
    _changed()


def _changed():
    global _timer
    with _lock:
        if _timer is None:
            _timer = threading.Timer(SAVE_AFTER, save)
            _timer.daemon = True
            _timer.start()


def _write_locked():
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        storage.save_json(DIR / f"{_day['day']}.json", _day, mode=storage.PRIVATE)
    except OSError:
        pass


def save():
    global _timer
    with _lock:
        _timer = None
        if _day.get("day"):
            _write_locked()
    _publish()


def prune():
    cutoff = datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)
    for f in DIR.glob("*.json"):
        try:
            if datetime.date.fromisoformat(f.stem) < cutoff:
                f.unlink()
        except (ValueError, OSError):
            continue


# ───────────────────────────── reading it back ─────────────────────────────

def _days(n: int) -> list[dict]:
    """The records of the last n days (today included)."""
    with _lock:
        today = dict(_today())
        today = {"day": today["day"], "counts": dict(today["counts"]),
                 "times": {k: list(v) for k, v in today["times"].items()}}
    records = [today]
    for back in range(1, n):
        day = (datetime.date.today() - datetime.timedelta(days=back)).isoformat()
        loaded = storage.load_json(DIR / f"{day}.json")
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def _merged(n: int) -> tuple[dict, dict]:
    counts, times = {}, {}
    for record in _days(n):
        for k, v in record.get("counts", {}).items():
            counts[k] = counts.get(k, 0) + v
        for k, v in record.get("times", {}).items():
            times.setdefault(k, []).extend(v)
    return counts, times


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[len(values) // 2]


def _sec(value: float | None) -> str:
    return f"{value:.1f}".replace(".", ",") + " detik" if value is not None else "-"


def summary(days: int = 1) -> dict:
    """Numbers for the HUD: {"calls", "call_rate", "commands", "heard", "voice", "gemini"} (rate in %)."""
    counts, times = _merged(days)
    rings = counts.get("ting", 0)
    good = counts.get("ting_berhasil", 0)
    finished = good + counts.get("ting_tanpa_perintah", 0) + counts.get("ting_dibatalkan", 0)
    commands = sum(counts.get(k, 0) for k in ("perintah_suara", "perintah_hud", "perintah_hp"))
    return {"calls": rings, "call_rate": round(100 * good / finished) if finished else None, "commands": commands,
            "heard": _median(times.get("dikenali", [])), "voice": _median(times.get("suara_mulai", [])),
            "gemini": _median(times.get("proses_gemini", []))}


def performance_report(days: int = 1) -> str:
    """How well Digidaw heard and answered today (days=1) or over the last days (days=7): calls that worked,
    speed and trouble. Use for "laporan kinerja", "performa kamu hari ini", "seberapa bagus kamu minggu ini"."""
    days = max(1, min(int(days or 1), KEEP_DAYS))
    counts, times = _merged(days)
    span = "Hari ini" if days == 1 else f"Selama {days} hari terakhir"
    T = config.USER_TITLE
    rings, good = counts.get("ting", 0), counts.get("ting_berhasil", 0)
    nothing, undone = counts.get("ting_tanpa_perintah", 0), counts.get("ting_dibatalkan", 0)
    direct = counts.get("panggilan_langsung", 0)
    commands = sum(counts.get(k, 0) for k in ("perintah_suara", "perintah_hud", "perintah_hp"))
    if not rings and not commands and not direct:
        return f"{span} belum ada percakapan yang bisa saya ukur, {T}."
    parts = []
    finished = good + nothing + undone
    if finished:
        parts.append(f"{span} saya dipanggil {rings} kali; {good} langsung berlanjut ke perintah "
                     f"({round(100 * good / finished)} persen), {nothing} tanpa perintah"
                     + (f", dan {undone} saya batalkan karena namanya ternyata tidak terdengar jelas" if undone else "")
                     + ".")
    if direct:
        parts.append(f"{direct} kali Tuan memanggil sekaligus memberi perintah.")
    routes = [(counts.get("rute_cepat", 0), "jalur cepat"), (counts.get("rute_lokal", 0), "otak lokal"),
              (counts.get("rute_gemini", 0), "Gemini")]
    done = [f"{n} lewat {label}" for n, label in routes if n]
    if commands:
        parts.append(f"Ada {commands} perintah" + (f": {', '.join(done)}." if done else "."))
    speed = []
    if times.get("dikenali"):
        speed.append(f"suara Tuan dikenali dalam {_sec(_median(times['dikenali']))}")
    if times.get("suara_mulai"):
        speed.append(f"suara saya mulai {_sec(_median(times['suara_mulai']))} kemudian")
    if times.get("proses_gemini"):
        speed.append(f"jawaban Gemini {_sec(_median(times['proses_gemini']))}")
    if speed:
        parts.append("Rata-rata " + ", ".join(speed) + ".")
    trouble = [(counts.get("gemini_sibuk", 0), "Gemini sibuk"), (counts.get("suara_cadangan", 0), "suara cadangan dipakai"),
               (counts.get("lampu_gagal", 0), "lampu tidak menjawab"), (counts.get("mikrofon_macet", 0), "mikrofon macet")]
    trouble = [f"{label} {n} kali" for n, label in trouble if n]
    if trouble:
        parts.append("Gangguan: " + ", ".join(trouble) + ".")
    return " ".join(parts).replace("Tuan", T)


def _publish():
    try:
        import events
        events.state["metrics"] = summary(1)
        events.emit("metrics", **events.state["metrics"])
    except Exception:
        pass


def start():
    prune()
    _publish()


AI_TOOLS = [performance_report]
