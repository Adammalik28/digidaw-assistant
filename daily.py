"""
The day in one breath: "rangkuman hari ini" (and by itself at config.DAILY_RECAP_HOUR, once, when the user is here).
How long the user worked at the computer and in which apps, how much Digidaw did, the messages that came in, and
what tomorrow brings (agenda, alarms, weather).

Screen time: every SAMPLE seconds, while the user is actually using the computer (input in the last ACTIVE_WINDOW
seconds, or something is playing while they are here), the app in front gets those seconds. Kept per day in
logs/daily/<date>.json: app names, minutes and counts only (no window titles, no text), KEEP_DAYS days.
"""
import datetime
import json
import re
import threading
import time

import config
import diag
import storage

T = config.USER_TITLE
ANNOUNCE = None                      # set by main (core.announce)
SAMPLE = 30
ACTIVE_WINDOW = 180                  # input this recent = the user is using the computer right now
SAVE_EVERY = 300
KEEP_DAYS = 14
DIR = config.LOG_DIR / "daily"
NICE = {"firefox": "Firefox", "google-chrome": "Chrome", "chrome": "Chrome", "chromium": "Chromium", "brave": "Brave",
        "code": "VS Code", "code-oss": "VS Code", "vscodium": "VSCodium", "kitty": "Terminal", "alacritty": "Terminal",
        "foot": "Terminal", "konsole": "Terminal", "org.gnome.terminal": "Terminal", "wezterm": "Terminal",
        "whatsie": "WhatsApp", "com.ktechpit.whatsie": "WhatsApp", "wasistlos": "WhatsApp", "whatsapp": "WhatsApp", "org.telegram.desktop": "Telegram",
        "telegramdesktop": "Telegram", "discord": "Discord", "vesktop": "Discord", "spotify": "Spotify",
        "digidawplayer": "YouTube (Digidaw)", "obsidian": "Obsidian", "libreoffice": "LibreOffice",
        "org.pwmt.zathura": "PDF", "thunar": "File manager", "nautilus": "File manager", "steam": "Steam",
        "waydroid": "Waydroid", "mpv": "mpv", "vlc": "VLC", "antigravity": "Antigravity"}
SKIP = {"hyprlock", "swaylock", "gtklock"}

_lock = threading.Lock()
_day = {"day": "", "active": 0, "apps": {}, "commands": 0, "tools": {}, "recap": False}
_started = False


def _today() -> str:
    return datetime.date.today().isoformat()


def _file(day: str):
    return DIR / f"{day}.json"


def _load(day: str) -> dict:
    try:
        data = json.loads(_file(day).read_text(encoding="utf-8"))
        if data.get("day") == day:
            return data
    except (OSError, ValueError):
        pass
    return {"day": day, "active": 0, "apps": {}, "commands": 0, "tools": {}, "recap": False, "since": time.time()}


def _current() -> dict:
    """Today's record (a new one after midnight). Call with _lock held."""
    day = _today()
    if _day["day"] != day:
        if _day["day"]:
            _save_locked()
        _day.clear()
        _day.update(_load(day))
    return _day


def _save_locked():
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        storage.save_json(_file(_day["day"]), _day, mode=storage.PRIVATE)
    except OSError:
        pass


def save():
    with _lock:
        if _day["day"]:
            _save_locked()


def _prune():
    cutoff = datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)
    for f in DIR.glob("*.json"):
        try:
            if datetime.date.fromisoformat(f.stem) < cutoff:
                f.unlink()
        except (ValueError, OSError):
            continue


# ───────────────────────────── counting ─────────────────────────────

def app_name(w: dict | None) -> str:
    if not w:
        return ""
    raw = (w.get("class") or w.get("process") or "").strip()
    key = raw.lower().removesuffix(".exe")
    if key in SKIP:
        return ""
    if key in NICE:
        return NICE[key]
    try:
        import window_manager
        if window_manager._is_digidaw(w):
            return "Digidaw"
    except Exception:
        pass
    name = key.split(".")[-1] if key else ""
    return name[:1].upper() + name[1:] if name else ""


def note_command():
    with _lock:
        _current()["commands"] += 1


def note_tool(name: str):
    with _lock:
        tools = _current()["tools"]
        tools[name] = tools.get(name, 0) + 1


def _using_now() -> bool:
    import presence
    if presence.is_away():
        return False
    if presence.idle_for() < ACTIVE_WINDOW:
        return True
    try:
        import media
        return media.is_playing()            # watching something: here, without touching anything
    except Exception:
        return False


def _sample():
    if not _using_now():
        return
    try:
        import screen_context
        app = app_name(screen_context.foreground())
    except Exception:
        app = ""
    with _lock:
        day = _current()
        day["active"] += SAMPLE
        if app:
            day["apps"][app] = day["apps"].get(app, 0) + SAMPLE


# ───────────────────────────── telling it ─────────────────────────────

def _span(seconds: float) -> str:
    hours, minutes = divmod(int(round(seconds / 60)), 60)
    if not hours:
        return f"{max(minutes, 1)} menit"
    return f"{hours} jam" + (f" {minutes} menit" if minutes else "")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " dan " + items[-1]


TOOL_TOPICS = [("lampu", ("lamp_",)), ("musik", ("play_", "media_control", "now_playing")), ("aplikasi",
               ("open_", "close_", "focus_window")), ("pengingat", ("set_timer", "set_alarm", "set_reminder")),
               ("pencarian", ("web_search", "google_search", "get_news")), ("gambar", ("generate_image",))]


def screen_time() -> str:
    """How long the user has been at the computer today and in which apps ("berapa lama saya di depan laptop")."""
    with _lock:
        day = json.loads(json.dumps(_current()))
    if day["active"] < 60:
        return f"Hari ini {T} belum lama memakai laptop."
    top = sorted(day["apps"].items(), key=lambda kv: -kv[1])
    top = [f"{name} {_span(sec)}" for name, sec in top[:3] if sec >= 120]
    text = f"Hari ini {T} aktif di depan laptop sekitar {_span(day['active'])}"
    since = datetime.datetime.fromtimestamp(day["since"]) if day.get("since") else None
    if since and since.hour >= 9:            # Digidaw only started counting then (a late start, the first day)
        text += f" (dihitung sejak pukul {since:%H.%M})"
    return text + (f", paling lama di {_join(top)}." if top else ".")


def _messages_today() -> str:
    try:
        import notifications
        midnight = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
        items = [i for i in notifications._fresh() if i["at"] >= midnight and notifications._is_chat(i["app"])]
    except Exception:
        return ""
    if not items:
        return ""
    counts: dict[str, int] = {}
    for i in items:
        name = notifications._name(i)
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(items) == 1:
        return f"Ada satu pesan masuk hari ini, dari {top[0][0]}."
    return f"Ada {len(items)} pesan masuk hari ini, terbanyak dari {top[0][0]}."


def _tomorrow() -> list[str]:
    parts = []
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    try:
        import agenda
        events = [e for d, e in agenda.upcoming(1) if d == tomorrow]
        if events:
            parts.append("Besok ada " + _join([agenda._title(e) + (f" pukul {e['time'].replace(':', '.')}"
                                                                  if e.get("time") else "") for e in events[:3]]) + ".")
    except Exception as e:
        diag.warn_once("daily.agenda", "agenda besok tidak masuk rangkuman", e)
    try:
        import alarms
        rings = [a for a in alarms.snapshot() if a["kind"] in ("alarm", "reminder")
                 and datetime.datetime.fromisoformat(a["due"]).date() == tomorrow]
        if rings:
            first = min(rings, key=lambda a: a["due"])
            when = datetime.datetime.fromisoformat(first["due"])
            what = "Alarm" if first["kind"] == "alarm" else f"Pengingat {first['message']}"
            parts.append(f"{what} besok pukul {when:%H.%M}" + (f", dan {len(rings) - 1} lagi" if len(rings) > 1 else "")
                         + ".")
    except Exception as e:
        diag.warn_once("daily.alarms", "alarm besok tidak masuk rangkuman", e)
    try:
        import weather
        if config.DEFAULT_CITY:
            line = weather.get_forecast("besok")
            if not line.startswith("Maaf"):
                parts.append(re.sub(r"^Besok, \w+, di [^:]+: ", "Cuaca besok ", line).rstrip(".") + ".")
    except Exception as e:
        diag.warn_once("daily.weather", "cuaca besok tidak masuk rangkuman", e)
    return parts


def daily_recap() -> str:
    """The day in one breath: time at the computer and the apps used, what Digidaw did, the messages that came in,
    and tomorrow's agenda, alarms and weather. Use for "rangkuman hari ini", "rekap hari ini", "hari ini ngapain aja"."""
    with _lock:
        day = json.loads(json.dumps(_current()))
    parts = []
    if day["active"] >= 300:
        parts.append(screen_time())
    if day["commands"]:
        topics = [name for name, prefixes in TOOL_TOPICS
                  if sum(n for t, n in day["tools"].items() if t.startswith(prefixes)) >= 2]
        parts.append(f"Saya menjalankan {day['commands']} perintah" + (f", paling sering soal {_join(topics[:2])}"
                                                                       if topics else "") + ".")
    messages = _messages_today()
    if messages:
        parts.append(messages)
    parts += _tomorrow()
    if not parts:
        return f"Belum banyak yang terjadi hari ini, {T}."
    hour = datetime.datetime.now().hour
    closing = f" Selamat beristirahat, {T}." if hour >= 20 or hour < 4 else ""
    return " ".join(parts) + closing


# ───────────────────────────── running ─────────────────────────────

def _loop():
    last_save = time.time()
    while True:
        time.sleep(SAMPLE)
        try:
            _sample()
            if time.time() - last_save >= SAVE_EVERY:
                last_save = time.time()
                save()
            _maybe_recap()
        except Exception as e:
            print(f"[daily] {e}")


def _maybe_recap():
    hour = config.DAILY_RECAP_HOUR
    now = datetime.datetime.now()
    if not hour or now.hour < hour or ANNOUNCE is None:
        return
    import presence
    with _lock:
        day = _current()
        if day.get("recap") or day["active"] < 600:
            return                           # once a day, and only on a day the computer was really used
    if presence.is_away() or presence.busy_reason():
        return                               # away, Do Not Disturb, full screen: tried again later, until midnight. Asking
                                             # core.announce anyway showed it on the HUD every 30 s without a sound.
    text = daily_recap()
    if ANNOUNCE(f"Rangkuman hari ini, {T}. {text}", "info", wait=60, push=False):
        with _lock:
            _current()["recap"] = True
            _save_locked()


def start():
    global _started
    if _started:
        return
    _started = True
    with _lock:
        _current()
    _prune()
    threading.Thread(target=_loop, daemon=True, name="daily").start()


AI_TOOLS = [daily_recap, screen_time]
