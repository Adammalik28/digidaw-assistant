"""
The lamp follows the day and the user, like a house that knows its owner (smarthome.py does the talking to the lamp):

- Shortly before sunset (SUNSET_LEAD minutes), with the user here and the lamp off: warm white on, and said once
  ("Sudah mulai gelap, Tuan. Lampu saya nyalakan.").
- At config.LAMP_NIGHT_HOUR a lamp that is still bright and white gets dim and warm, once a night.
- Back at the computer after dark (presence.py) with the lamp off: it comes on. Not after midnight (someone reaching
  for the laptop in bed doesn't want the light) and not when the user turned it off themselves this evening.
- Away for config.LAMP_AWAY_MINUTES with the lamp on: dimmed to AWAY_BRIGHTNESS; back = the brightness it had.
  "Away" means the room seems empty: the laptop untouched AND nobody heard talking for that long (away from the laptop
  isn't out of the room: on the phone, reading, chatting with someone).
- A wake-up alarm: the lamp comes on like a sunrise. A timer or reminder: it blinks (only when it is on).

Nothing is done shortly after the user changed the lamp themselves (USER_GRACE). Sunrise and sunset are computed here
(NOAA's formula, ~1 minute accurate) for the weather city's coordinates, so no internet is needed for them.
"""
import datetime
import json
import math
import threading
import time

import config
import diag
import storage
import presence
import smarthome

T = config.USER_TITLE
ANNOUNCE = None                      # set by main (core.announce)
CHECK_EVERY = 30
SUNSET_LEAD = 15                     # minutes before sunset the light comes on
EVENING_END_HOUR = 24                # "back after dark" turns the lamp on until midnight, not later
AWAY_BRIGHTNESS = 10                 # percent while the user is away long
USER_GRACE = 20 * 60                 # the user changed the lamp less than this ago: the automations leave it alone
FALLBACK_PLACE = (-6.2, 106.8)       # Jakarta, when the weather city is unknown
STATE_FILE = config.LOG_DIR / "home_auto.json"

_lock = threading.Lock()
_away = {"dimmed_at": 0.0, "brightness": None, "looked_at": 0.0}
AWAY_LOOK_EVERY = 300                # while away with the lamp off, its state is looked at only this often
_started = False


# ───────────────────────────── the sun ─────────────────────────────

def sun_times(lat: float, lon: float, day: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """(sunrise, sunset) on that day, as local times (NOAA's simplified sunrise equation)."""
    noon_utc = datetime.datetime(day.year, day.month, day.day, 12, tzinfo=datetime.timezone.utc).timestamp()
    n = round(noon_utc / 86400 + 2440587.5 - 2451545.0)
    mean_noon = n - lon / 360
    anomaly = math.radians((357.5291 + 0.98560028 * mean_noon) % 360)
    centre = 1.9148 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly) + 0.0003 * math.sin(3 * anomaly)
    longitude = math.radians((math.degrees(anomaly) + centre + 180 + 102.9372) % 360)
    transit = 2451545.0 + mean_noon + 0.0053 * math.sin(anomaly) - 0.0069 * math.sin(2 * longitude)
    declination = math.asin(math.sin(longitude) * math.sin(math.radians(23.4397)))
    lat_r = math.radians(lat)
    cos_hour = (math.sin(math.radians(-0.833)) - math.sin(lat_r) * math.sin(declination)) / \
               (math.cos(lat_r) * math.cos(declination))
    hour_angle = math.degrees(math.acos(max(-1.0, min(1.0, cos_hour))))

    def local(julian: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp((julian - 2440587.5) * 86400)
    return local(transit - hour_angle / 360), local(transit + hour_angle / 360)


def _place() -> tuple[float, float]:
    try:
        import weather
        where = weather.place()
        if where:
            return where["lat"], where["lon"]
    except Exception:
        pass
    return FALLBACK_PLACE


def today_sun(day: datetime.date | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    return sun_times(*_place(), day or datetime.date.today())


def is_dark(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    rise, sunset = today_sun(now.date())
    return now < rise or now >= sunset - datetime.timedelta(minutes=SUNSET_LEAD)


def sun_today() -> str:
    """When the sun rises and sets today (also "kapan matahari terbenam", "jam berapa matahari terbit")."""
    rise, sunset = today_sun()
    return f"Hari ini matahari terbit pukul {rise:%H.%M} dan terbenam pukul {sunset:%H.%M}, {T}."


# ───────────────────────────── what was done today ─────────────────────────────

def _today() -> dict:
    """{"day", "sunset", "night"}: each happens once a day (also across a restart of Digidaw)."""
    day = datetime.date.today().isoformat()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if data.get("day") == day else {"day": day}


def _mark(key: str):
    data = _today()
    data[key] = True
    try:
        storage.save_json(STATE_FILE, data)
    except OSError:
        pass


def _say(text: str):
    if ANNOUNCE:
        try:
            ANNOUNCE(text, "info", push=False)
        except Exception as e:
            diag.warn_once("home_auto.say", "pengumuman lampu otomatis gagal", e)


def _user_changed_recently() -> bool:
    return smarthome.changed_by_user_since(time.time() - USER_GRACE)


# ───────────────────────────── the rules ─────────────────────────────

def check(now: datetime.datetime | None = None):
    """One look at the clock and the user (every CHECK_EVERY seconds)."""
    if not config.LAMP_AUTO or not smarthome.has_lamps() or not smarthome.reachable():
        return                           # (an unplugged lamp isn't searched for every half minute)
    now = now or datetime.datetime.now()
    rise, sunset = today_sun(now.date())
    done = _today()
    with _lock:
        # 1. dusk: the light comes on for the user who is here
        if not done.get("sunset") and sunset - datetime.timedelta(minutes=SUNSET_LEAD) <= now < sunset + \
                datetime.timedelta(hours=3):
            _mark("sunset")
            if not presence.is_away() and not _user_changed_recently() and not smarthome.turned_off_by_user_since(
                    time.time() - USER_GRACE):
                s = smarthome.state(fresh=True)
                if s and s.get("online") and not s.get("on"):
                    if not smarthome.set_state(brightness=_evening_brightness(now), warmth=0):
                        _say(f"Sudah mulai gelap, {T}. Lampu saya nyalakan.")
        # 2. late evening: dim and warm, once
        night = config.LAMP_NIGHT_HOUR
        if night and not done.get("night") and now.hour >= night:
            _mark("night")
            s = smarthome.state(fresh=True)
            if s and s.get("on") and s.get("mode") == "white" and s.get("brightness", 0) > config.LAMP_NIGHT_BRIGHTNESS \
                    and not _user_changed_recently():
                if not smarthome.set_state(brightness=config.LAMP_NIGHT_BRIGHTNESS, warmth=0):
                    _say(f"Sudah malam, {T}. Lampu saya redupkan supaya lebih nyaman.")
        # 3. away a long time: dimmed until the user is back
        minutes = config.LAMP_AWAY_MINUTES
        if minutes and not _away["dimmed_at"] and presence.away_for() >= minutes * 60 \
                and presence.room_quiet_for() >= minutes * 60 \
                and time.time() - _away["looked_at"] >= AWAY_LOOK_EVERY:
            _away["looked_at"] = time.time()
            s = smarthome.state(fresh=True)
            if s and s.get("on") and s.get("brightness", 0) > AWAY_BRIGHTNESS and s.get("mode") in ("white", "colour"):
                if not smarthome.set_state(brightness=AWAY_BRIGHTNESS):
                    _away.update(dimmed_at=time.time(), brightness=s.get("brightness"))


def _evening_brightness(now: datetime.datetime) -> int:
    night = config.LAMP_NIGHT_HOUR
    return config.LAMP_NIGHT_BRIGHTNESS if night and now.hour >= night else 100


def on_return(seconds: float, how: str, media_played: bool, left_at: float, now: datetime.datetime | None = None):
    """presence.py: the user is back after being away."""
    if not config.LAMP_AUTO or not smarthome.has_lamps():
        return
    now = now or datetime.datetime.now()
    with _lock:
        dimmed_at, before = _away["dimmed_at"], _away["brightness"]
        _away.update(dimmed_at=0.0, brightness=None, looked_at=0.0)
        if dimmed_at:
            if not smarthome.changed_by_user_since(dimmed_at) and before:
                smarthome.set_state(brightness=before)       # the brightness it had before the user left
            return
        rise, sunset = today_sun(now.date())
        evening = sunset - datetime.timedelta(minutes=SUNSET_LEAD) <= now and now.hour < EVENING_END_HOUR
        if not evening or smarthome.turned_off_by_user_since(sunset.timestamp() - SUNSET_LEAD * 60):
            return
        s = smarthome.state(fresh=True)
        if s and s.get("online") and not s.get("on"):
            smarthome.set_state(brightness=_evening_brightness(now), warmth=0)


def on_alarm(kind: str):
    """alarms.py: an alarm rings (the lamp rises like the sun) or a timer / reminder is due (the lamp blinks)."""
    if not config.LAMP_ALARM_LIGHT or not smarthome.has_lamps():
        return
    try:
        if kind == "alarm":
            smarthome.sunrise()
        else:
            smarthome.flash()
    except Exception as e:
        diag.warn_once("home_auto.alarm", "lampu alarm / pengingat gagal", e)


def _loop():
    while True:
        try:
            check()
        except Exception as e:
            print(f"[home_auto] {e}")
        time.sleep(CHECK_EVERY)


def start():
    global _started
    if _started or not smarthome.has_lamps():
        return
    _started = True
    presence.on_return(on_return)
    threading.Thread(target=_loop, daemon=True, name="home-auto").start()


AI_TOOLS = [sun_today]
