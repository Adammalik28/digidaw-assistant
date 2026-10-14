"""
Skills powered by Gemini vision or live internet data:
screen analysis, web search, news headlines, weather data for the HUD, daily briefing.
"""
import io
import re
import html
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import llm

T = config.USER_TITLE
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# ─────────────────────────────── vision ───────────────────────────────

def _capture_screen(max_side: int = 1600) -> bytes:
    import oscompat
    image = oscompat.grab_screen()
    image.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


def analyze_screen(question: str = "Apa yang ada di layar saya?") -> str:
    """Look at the user's screen and answer a question about it: describe what's visible, read
    text or error messages, explain code, translate on-screen text, summarize an article, etc."""
    from google.genai import types
    image = _capture_screen()
    prompt = (f"Ini tangkapan layar komputer user saat ini. Pertanyaan user: {question}\n"
              "Jawab akurat berdasarkan isi gambar, ringkas (maksimal 4 kalimat), dalam bahasa yang "
              "dipakai user, tanpa markdown. Abaikan jendela DIGIDAW sendiri kecuali ditanya.")
    return llm.generate([types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt], max_tokens=700)


# ───────────────────────────── web & news ─────────────────────────────

WIKI_HEADERS = {"User-Agent": "DigidawDesktopAssistant/1.0 (personal assistant; local use)"}
STOPWORDS = {"yang", "dan", "di", "ke", "dari", "untuk", "hari", "ini", "itu", "apa", "siapa", "berapa",
             "kapan", "dimana", "bagaimana", "sekarang", "terbaru", "the", "what", "who", "how", "is", "of"}


def _rss_items(url: str, params: dict, limit: int) -> list[dict]:
    r = requests.get(url, params=params, headers=HEADERS, timeout=10)
    items = []
    for item in ET.fromstring(r.content).iter("item"):
        try:
            date = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            date = None
        items.append({"title": _strip_html(item.findtext("title")), "date": date})
        if len(items) >= limit:
            break
    return items


def _news_search(query: str, days: int = 7) -> list[str]:
    items = _rss_items("https://news.google.com/rss/search",
                       {"q": f"{query} when:{days}d", "hl": "id", "gl": "ID", "ceid": "ID:id"}, limit=8)
    items.sort(key=lambda i: i["date"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
    return [f"[{i['date']:%d-%m-%Y}] {i['title']}" if i["date"] else i["title"] for i in items]


def _wikipedia(query: str) -> str:
    api = "https://id.wikipedia.org/w/api.php"
    hits = requests.get(api, params={"action": "query", "list": "search", "srsearch": query, "format": "json",
                                     "srlimit": 1, "utf8": 1}, headers=WIKI_HEADERS, timeout=8).json()
    results = hits.get("query", {}).get("search", [])
    if not results:
        return ""
    title = results[0]["title"]
    pages = requests.get(api, params={"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                                      "titles": title, "format": "json", "exchars": 700},
                         headers=WIKI_HEADERS, timeout=8).json()["query"]["pages"]
    extract = next(iter(pages.values())).get("extract", "")
    return f"{title}: {extract}" if extract else ""


def _bing(query: str) -> list[str]:
    """Bing sometimes answers automated requests with unrelated pages, so keep only results
    that actually mention the query's key words."""
    r = requests.get("https://www.bing.com/search", params={"q": query, "setlang": "id", "cc": "ID"},
                     headers=HEADERS, timeout=10)
    keywords = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 4 and w not in STOPWORDS]
    results = []
    for block in re.findall(r'<li class="b_algo"(.*?)</li>', r.text, re.S):
        title = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        snippet = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        text = f"{_strip_html(title.group(1)) if title else ''} — {_strip_html(snippet.group(1)) if snippet else ''}"
        hits = sum(1 for k in keywords if k in text.lower())
        if keywords and hits >= max(1, len(keywords) // 2):
            results.append(text)
    return results[:5]


def web_search(query: str) -> str:
    """Search the internet for up-to-date information: news, prices (emas, kurs, crypto, BBM), scores,
    schedules, public figures, facts after your knowledge cutoff, or anything the user wants
    'dicari/dicek'. Returns dated news headlines, a Wikipedia summary and web snippets; answer
    the user from them (prefer the newest dated information)."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn, query) for name, fn in
                   (("news", _news_search), ("wiki", _wikipedia), ("web", _bing))}
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=12)
            except Exception:
                results[name] = None

    sections = [f"Hasil pencarian untuk '{query}' (hari ini {datetime.date.today():%d-%m-%Y}):"]
    if results["news"]:
        sections.append("Berita terbaru (Google News):\n" + "\n".join(f"- {n}" for n in results["news"]))
    if results["wiki"]:
        sections.append("Wikipedia: " + results["wiki"])
    if results["web"]:
        sections.append("Web:\n" + "\n".join(f"- {w}" for w in results["web"]))
    if len(sections) == 1:
        return f"Pencarian untuk '{query}' tidak menemukan hasil atau internet sedang bermasalah."
    return "\n\n".join(sections)


def get_news(topic: str = "") -> str:
    """Latest news headlines in Indonesia, optionally about a topic (e.g. 'teknologi', 'bola')."""
    if topic:
        titles = _news_search(topic, days=3)[:6]
    else:
        titles = [i["title"] for i in _rss_items("https://news.google.com/rss",
                                                 {"hl": "id", "gl": "ID", "ceid": "ID:id"}, limit=6)]
    if not titles:
        return f"Belum ada berita{' tentang ' + topic if topic else ''}, {T}."
    return f"Berita terkini{' tentang ' + topic if topic else ''}: " + " | ".join(titles)


def weather_data(city: str = "") -> dict:
    """Structured weather for the HUD. Like get_weather, an unknown long name ("Bandar Lampung tanjung bintang") is
    shortened word by word, then the connection's own location is used."""
    import system_control as sc
    words = (city or config.DEFAULT_CITY or "").split()
    for place in dict.fromkeys([" ".join(words[:n]) for n in range(len(words), 0, -1)] + [""]):
        try:
            text = requests.get(f"https://wttr.in/{urllib.parse.quote(place)}?format=%l|%t|%C|%h|%w&lang=id",
                                timeout=8).text.strip()
        except Exception:
            return {}
        parts = text.split("|")
        if len(parts) != 5 or re.search(r"unknown location|not found|sorry", text, re.IGNORECASE):
            continue
        name, temp, cond, hum, wind = parts
        cond = sc.WEATHER_ID.get(cond.strip().lower(), cond.strip())
        return {"place": name.split(",")[0].strip(), "temp": temp.replace("+", "").strip(), "condition": cond,
                "humidity": hum.strip(), "wind": re.sub(r"[←↑→↓↖↗↘↙]", "", wind).replace("km/h", " km/j").strip()}
    return {}


# ───────────────────────────── briefing ─────────────────────────────

def greeting() -> str:
    hour = datetime.datetime.now().hour
    if 4 <= hour < 11:
        return "Selamat pagi"
    if 11 <= hour < 15:
        return "Selamat siang"
    if 15 <= hour < 18:
        return "Selamat sore"
    return "Selamat malam"


def daily_briefing() -> str:
    """Daily briefing ('briefing pagi', 'apa kabar hari ini', 'laporan harian'): date, weather,
    battery/system, active reminders and top news headlines."""
    import psutil
    import system_control as sc
    parts = [f"{greeting()}, {T}.", sc.get_date().removesuffix(f", {T}.") + "."]
    weather = weather_data()
    if weather:
        parts.append(f"Cuaca di {weather['place']} {weather['condition'].lower()}, suhu {weather['temp']}.")
    battery = psutil.sensors_battery()
    if battery:
        parts.append(f"Baterai {battery.percent:.0f} persen{' dan sedang dicas' if battery.power_plugged else ''}.")
    import alarms
    if alarms.snapshot():
        parts.append(alarms.list_timers())
    import agenda
    if agenda.briefing_line():
        parts.append(agenda.briefing_line())
    try:
        parts.append(get_news())
    except Exception:
        pass
    return " ".join(parts)


def morning_briefing() -> str:
    """The summary for the first "Digidaw" of the day: today's agenda, alarms, weather and battery."""
    import datetime as _dt
    import psutil
    import system_control as sc
    import agenda
    import alarms
    parts = [f"{greeting()}, {T}.", sc.get_date().removesuffix(f", {T}.") + "."]
    today = agenda.upcoming(0)
    if today:
        parts.append("Agenda hari ini: " + "; ".join(
            e["title"] + (f" jam {e['time'].replace(':', '.')}" if e.get("time") else "") for _, e in today[:4]) + ".")
    else:
        parts.append("Tidak ada agenda hari ini.")
    tomorrow = [(d, e) for d, e in agenda.upcoming(1) if (d - _dt.date.today()).days == 1]
    if tomorrow:
        parts.append("Besok: " + "; ".join(e["title"] for _, e in tomorrow[:3]) + ".")
    ringing_today = [i for i in alarms.snapshot() if i["kind"] in ("alarm", "reminder")
                     and i["due"][:10] == _dt.date.today().isoformat()]
    if ringing_today:
        parts.append(alarms.list_timers())
    weather = None
    try:
        import hud_server
        weather = hud_server._latest.get("weather") or None      # already fetched for the HUD
    except Exception:
        pass
    weather = weather or weather_data()
    if weather:
        place = " ".join(weather["place"].split()[:2])         # "Bandar Lampung tanjung" -> "Bandar Lampung"
        parts.append(f"Cuaca di {place} {weather['condition'].lower()}, {weather['temp']}.")
    battery = psutil.sensors_battery()
    if battery:
        state = "sedang dicas" if battery.power_plugged else (
            "sebaiknya segera dicas" if battery.percent < 30 else "tanpa charger")
        parts.append(f"Baterai {battery.percent:.0f} persen, {state}.")
    return " ".join(parts)


def startup_briefing() -> str:
    """Short spoken summary for the first start of the day (no news: that's 'briefing harian')."""
    import psutil
    import system_control as sc
    parts = [f"{greeting()}, {T}. Digidaw online.", sc.get_date().removesuffix(f", {T}.") + "."]
    weather = None
    try:
        import hud_server
        weather = hud_server._latest.get("weather") or None      # already fetched for the HUD
    except Exception:
        pass
    for _ in range(2):
        if weather:
            break
        weather = weather_data()
    if weather:
        parts.append(f"Cuaca di {weather['place']} {weather['condition'].lower()}, {weather['temp']}.")
    battery = psutil.sensors_battery()
    if battery and not battery.power_plugged and battery.percent < 40:
        parts.append(f"Baterai tinggal {battery.percent:.0f} persen.")
    import alarms
    if [i for i in alarms.snapshot() if i["kind"] != "action"]:
        parts.append(alarms.list_timers())
    try:
        import security_scan
        state = security_scan._load_state()
        if state.get("score") is not None and state["score"] < 80:
            parts.append(f"Skor keamanan terakhir {state['score']}; detailnya ada di panel Keamanan.")
    except Exception:
        pass
    try:
        import project_builder
        busy = [j for j in project_builder._jobs.values() if j["status"] in ("building", "queued")]
        if busy:
            parts.append(f"{len(busy)} project masih dikerjakan.")
    except Exception:
        pass
    parts.append("Panggil saya kapan saja.")
    return " ".join(parts)


import rag_skills
import diagnostics

AI_TOOLS = [analyze_screen, web_search, get_news, daily_briefing] + rag_skills.AI_TOOLS + diagnostics.AI_TOOLS
