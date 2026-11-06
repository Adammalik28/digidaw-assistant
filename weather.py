"""
Weather that looks ahead, like a butler glancing at the sky: "besok hujan nggak?" is answered from a real forecast
instead of a guess, and shortly before rain starts Digidaw says so ("sebentar lagi hujan, jemurannya diangkat").

Data: Open-Meteo (free, no key). The place is config.DEFAULT_CITY, looked up once with Open-Meteo's geocoding and
kept in logs/weather_place.json. The weather right now still comes from get_weather (wttr.in).
"""
import datetime
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import storage

T = config.USER_TITLE
ANNOUNCE = None                     # set by main (core.announce)
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PLACE_FILE = config.LOG_DIR / "weather_place.json"
CACHE_SECONDS = 10 * 60             # one forecast serves the answers and the rain watch this long
CHECK_EVERY = 15 * 60               # the rain watch looks this often
RAIN_CHANCE = 60                    # percent in the coming hour that counts as "it is going to rain"...
RAIN_MM = 0.2                       # ...with at least this much rain in that hour
SPELL_GAP = 3 * 3600                # one warning per rainy spell
ALERT_HOURS = range(6, 21)          # nobody wants a rain warning at 3 a.m.
AWAY_TOO_LONG = 2 * 3600            # away this long: probably out or asleep (the phone still gets it)
LIKELY = 50                         # a forecast answer says "kemungkinan hujan" from this chance on
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
# WMO weather codes (what Open-Meteo reports)
WMO = {0: "cerah", 1: "cerah berawan", 2: "berawan sebagian", 3: "berawan", 45: "berkabut", 48: "berkabut",
       51: "gerimis ringan", 53: "gerimis", 55: "gerimis lebat", 56: "gerimis beku", 57: "gerimis beku",
       61: "hujan ringan", 63: "hujan sedang", 65: "hujan lebat", 66: "hujan beku", 67: "hujan beku",
       71: "salju ringan", 73: "salju", 75: "salju lebat", 77: "butiran salju", 80: "hujan lokal ringan",
       81: "hujan lokal", 82: "hujan lokal sangat lebat", 85: "hujan salju", 86: "hujan salju lebat",
       95: "badai petir", 96: "badai petir disertai hujan es", 99: "badai petir disertai hujan es"}
RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

_places: dict = {}
_cache = {"key": None, "at": 0.0, "data": None}
_last_warning = 0.0
_started = False


def describe(code) -> str:
    return WMO.get(code, "berawan")


# ───────────────────────────── place & forecast ─────────────────────────────

def candidates(city: str) -> list[str]:
    """Names to look the place up by, the most specific first. "Kota Bekasi bantar gebang" is a district of a
    city: the geocoder knows neither the whole phrase nor "bantar gebang", but it does know "Bantargebang"."""
    words = city.split()
    names = []
    for n in range(len(words), 0, -1):
        for start in range(len(words) - n, -1, -1):          # the last words first: they name the smaller place
            part = words[start:start + n]
            names.append(" ".join(part))
            if n > 1:
                names.append("".join(part))
    return [name for name in dict.fromkeys(names) if len(name) >= 3]


def place(city: str = "") -> dict | None:
    """{'name', 'lat', 'lon'} of the city (default: the user's own, config.DEFAULT_CITY), or None when it is
    unknown or there is no internet."""
    city = " ".join((city or config.DEFAULT_CITY or "").split())
    key = city.lower()
    if not key:
        return None
    if key not in _places:
        try:
            _places.update(json.loads(PLACE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if key in _places:
        return _places[key]

    def look(name: str):
        try:
            return requests.get(GEOCODE_URL, params={"name": name, "count": 1, "language": "id"},
                                timeout=8).json().get("results") or []
        except (requests.RequestException, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:        # one by one took ~12 s for a district's name
        found = list(pool.map(look, candidates(city)[:12]))
    hit = next((hits[0] for hits in found if hits), None)
    if hit is None:
        if None not in found:
            _places[key] = None              # no such place: not looked up again until Digidaw restarts
        return None                          # (offline: looked up again next time)
    _places[key] = {"name": hit["name"], "lat": hit["latitude"], "lon": hit["longitude"]}
    try:
        storage.save_json(PLACE_FILE, {k: v for k, v in _places.items() if v})
    except OSError:
        pass
    return _places[key]


def forecast(where: dict) -> dict | None:
    key = (round(where["lat"], 3), round(where["lon"], 3))
    if _cache["key"] == key and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        data = requests.get(FORECAST_URL, timeout=8, params={
            "latitude": where["lat"], "longitude": where["lon"], "timezone": "auto", "forecast_days": 3,
            "current": "temperature_2m,precipitation,weather_code",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }).json()
    except (requests.RequestException, ValueError):
        return None
    if "hourly" not in data or "daily" not in data:
        return None
    _cache.update(key=key, at=time.time(), data=data)
    return data


def _local_now(data: dict) -> datetime.datetime:
    """The time at that place (the forecast's hours are in its own time zone)."""
    utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return utc + datetime.timedelta(seconds=data.get("utc_offset_seconds", 0))


def _hours(data: dict):
    """(end of the hour, chance %, mm, code). Open-Meteo reports each hour's rain at the hour's END: 15:00 = the
    rain between 14:00 and 15:00."""
    h = data["hourly"]
    for i, stamp in enumerate(h["time"]):
        yield (datetime.datetime.fromisoformat(stamp), h["precipitation_probability"][i] or 0,
               h["precipitation"][i] or 0, h["weather_code"][i])


def _raining(data: dict) -> bool:
    current = data.get("current") or {}
    return (current.get("precipitation") or 0) >= 0.1 or current.get("weather_code") in RAINY


def _first_rain(data: dict, after: datetime.datetime, until: datetime.datetime, chance: int = LIKELY):
    """(start of the first hour likely to be rainy, its chance) between the two times, or None."""
    for end, pct, mm, code in _hours(data):
        if after < end <= until and pct >= chance and (mm >= 0.1 or code in RAINY):
            return end - datetime.timedelta(hours=1), pct
    return None


# ───────────────────────────── answers ─────────────────────────────

def rain_soon(data: dict, now: datetime.datetime) -> tuple[str, int] | None:
    """It is dry now and rain is expected within about an hour: ("sebentar lagi" | "sekitar jam 14.00", chance)."""
    if _raining(data):
        return None                          # already raining: that's no news
    for end, pct, mm, code in _hours(data):
        start = end - datetime.timedelta(hours=1)
        if end <= now:
            continue
        if start > now + datetime.timedelta(minutes=60):
            break
        if pct >= RAIN_CHANCE and mm >= RAIN_MM:
            return ("sebentar lagi" if start <= now + datetime.timedelta(minutes=10) else f"sekitar jam {start:%H.%M}"), pct
    return None


def _day(data: dict, name: str, index: int) -> str:
    d = data["daily"]
    date = datetime.date.fromisoformat(d["time"][index])
    pct = d["precipitation_probability_max"][index] or 0
    import agenda
    text = (f"{'Besok' if index == agenda.tomorrow_offset() else 'Lusa'}, {HARI[date.weekday()]}, di {name}: {describe(d['weather_code'][index])}, "
            f"suhu {d['temperature_2m_min'][index]:.0f} sampai {d['temperature_2m_max'][index]:.0f} derajat")
    day = datetime.datetime.combine(date, datetime.time())
    rainy = [(end - datetime.timedelta(hours=1), p) for end, p, mm, code in _hours(data)
             if day < end <= day + datetime.timedelta(days=1) and p >= LIKELY and (mm >= 0.1 or code in RAINY)]
    daytime = [r for r in rainy if 6 <= r[0].hour < 22]            # rain at 2 a.m. matters less than at 2 p.m.
    if pct >= LIKELY:
        text += f", peluang hujan {pct} persen"
        if daytime:
            text += f", paling mungkin sekitar jam {max(daytime, key=lambda r: r[1])[0]:%H.%M}"
        elif rainy:
            text += ", terutama malam hari"
    else:
        text += f", peluang hujannya kecil ({pct} persen)"
    return text + f", {T}."


def _coming_hours(data: dict, name: str, with_day: bool) -> str:
    now = _local_now(data)
    current, d = data.get("current") or {}, data["daily"]
    text = f"Sekarang di {name} {describe(current.get('weather_code'))}, {current.get('temperature_2m', 0):.0f} derajat"
    if with_day:
        text += f"; hari ini {d['temperature_2m_min'][0]:.0f} sampai {d['temperature_2m_max'][0]:.0f} derajat"
    midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time())
    horizon = max(midnight, now + datetime.timedelta(hours=6))
    if _raining(data):
        dry = next((end - datetime.timedelta(hours=1) for end, pct, mm, _ in _hours(data)
                    if now < end <= horizon and pct < 30 and mm < 0.1), None)
        return text + (f". Hujannya kemungkinan reda sekitar jam {dry:%H.%M}, {T}." if dry else
                       f". Hujannya diperkirakan masih akan turun, {T}.")
    rain = _first_rain(data, now, horizon)
    if rain:
        start = "sebentar lagi" if rain[0] <= now + datetime.timedelta(minutes=10) else f"sekitar jam {rain[0]:%H.%M}"
        return text + f". Kemungkinan hujan {start}, peluangnya {rain[1]} persen, {T}."
    return text + f". Sampai {'malam' if horizon == midnight else 'beberapa jam ke depan'} sepertinya tidak hujan, {T}."


def get_forecast(when: str = "besok", city: str = "") -> str:
    """Weather forecast with the chance of rain and when it starts. when: "nanti" (the coming hours), "hari ini",
    "besok" or "lusa"; city: empty = the user's own city. Use for "besok hujan nggak", "nanti sore hujan?",
    "cuaca besok", "prakiraan cuaca lusa di Bandung"."""
    asked = city or config.DEFAULT_CITY
    where = place(city)
    if not where:
        return (f"Maaf {T}, kota '{asked}' tidak ditemukan di layanan cuaca, atau internet sedang putus." if asked else
                f"Maaf {T}, kota Anda belum diatur. Isi 'Kota untuk cuaca' di Control Panel.")
    data = forecast(where)
    if not data:
        return f"Maaf {T}, prakiraan cuaca sedang tidak bisa diakses."
    when = (when or "").lower()
    import agenda
    if "lusa" in when:
        return _day(data, where["name"], agenda.tomorrow_offset() + 1)
    if "besok" in when or "tomorrow" in when:
        return _day(data, where["name"], agenda.tomorrow_offset())
    return _coming_hours(data, where["name"], with_day="hari ini" in when or "today" in when)


def briefing_line() -> str:
    """For the morning summary, only when it matters: "Peluang hujan hari ini 80 persen, mulai sekitar jam 14.00." """
    where = place()
    data = where and forecast(where)
    if not data or (data["daily"]["precipitation_probability_max"][0] or 0) < LIKELY:
        return ""
    now = _local_now(data)
    rain = _first_rain(data, now, datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time()))
    pct = data["daily"]["precipitation_probability_max"][0]
    return f"Peluang hujan hari ini {pct} persen" + (f", kemungkinan mulai sekitar jam {rain[0]:%H.%M}." if rain else ".")


# ───────────────────────────── the rain watch ─────────────────────────────

def check_rain() -> str | None:
    """Warn once per rainy spell, shortly before it starts. Returns what was said."""
    global _last_warning
    if time.time() - _last_warning < SPELL_GAP:
        return None
    where = place()
    data = where and forecast(where)
    if not data:
        return None
    now = _local_now(data)
    soon = now.hour in ALERT_HOURS and rain_soon(data, now)
    if not soon:
        return None
    import presence
    if presence.away_for() > AWAY_TOO_LONG:
        return None
    tip = random.choice(["Kalau ada jemuran, sebaiknya diangkat.", "Kalau mau keluar, jangan lupa payung.",
                         "Jemuran di luar sebaiknya diangkat dulu."])
    text = f"{T}, {soon[0]} kemungkinan hujan di {where['name']}, peluangnya {soon[1]} persen. {tip}"
    _last_warning = time.time()
    ANNOUNCE(text, "reminder")
    return text


def _watch():
    time.sleep(20)
    try:
        place()                              # looked up early, so the first "besok hujan?" is quick
    except Exception:
        pass
    time.sleep(100)                          # let the start settle (network, the first greeting)
    while True:
        try:
            if config.RAIN_ALERTS and ANNOUNCE is not None:
                check_rain()
        except Exception as e:
            print(f"[cuaca] {e}")
        time.sleep(CHECK_EVERY)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_watch, daemon=True, name="rain-watch").start()


AI_TOOLS = [get_forecast]
