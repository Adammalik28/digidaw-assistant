"""
"Kabari saya kalau …": short-lived watches that end with one spoken sentence, like a butler asked to keep an eye on
something.

  unduhan    the downloads in progress in ~/Downloads are finished ("kabari kalau unduhannya selesai")
  pesan      a message from someone (or any chat) comes in ("kabari kalau Rina membalas")
  hujan_reda it stops raining here; hujan: rain starts (or is about to)
  baterai    the laptop's battery is full, or below / above a level ("kalau baterai di bawah 20 persen")
  program    a program has ended ("kabari kalau ffmpeg selesai")
  internet   the internet is back
  situs      a website answers again ("kabari kalau situs kampus bisa dibuka")

Watches live in memory only (gone after a restart) and give up after MAX_HOURS. A watch the user asked for is said
even while they are away (level "reminder": the phone gets it too).
"""
import datetime
import re
import socket
import threading
import time
import uuid
from pathlib import Path

import config
import diag

T = config.USER_TITLE
ANNOUNCE = None                      # set by main (core.announce)
CHECK_EVERY = 10                     # seconds; each kind also has its own pace (PACE)
PACE = {"hujan_reda": 300, "hujan": 300, "situs": 60, "internet": 15, "baterai": 30, "program": 10, "unduhan": 10}
MAX_HOURS = {"unduhan": 6, "pesan": 24, "hujan_reda": 12, "hujan": 12, "baterai": 12, "program": 12, "internet": 12,
             "situs": 12}
DOWNLOAD_WAIT = 180                  # "kabari kalau unduhan selesai" before the download started: wait this long for it
PARTIAL = (".part", ".crdownload", ".download", ".partial", ".aria2", ".opdownload", ".tmp")
KIND_WORDS = {"unduhan": "unduhan selesai", "pesan": "pesan masuk", "hujan_reda": "hujan reda", "hujan": "hujan turun",
              "baterai": "baterai", "program": "program selesai", "internet": "internet tersambung",
              "situs": "situs bisa dibuka"}

_lock = threading.Lock()
_watches: list[dict] = []
_started = False


# ───────────────────────────── understanding the request ─────────────────────────────

_NAME = r"([a-z][\w .'-]{0,30}?)"
_PATTERNS = [
    (r"(?:semua )?(?:unduhan|download|downloadan|unduhannya|downloadnya)(?: (?:saya|ku|aku|nya|itu|ini|tadi))?"
     r" (?:sudah |udah )?(?:selesai|beres|kelar|rampung|komplit|complete)", lambda m: ("unduhan", "", "")),
    (r"(?:file|berkas)(?: (?:saya|nya|itu|ini|tadi))? (?:sudah |udah )?(?:selesai|beres|kelar) (?:di ?download|diunduh)",
     lambda m: ("unduhan", "", "")),
    (r"ada (?:pesan|chat|wa)(?: (?:masuk|baru))?|ada yang (?:chat|nge-?chat|kirim pesan|ngirim pesan|nge-?wa|wa|"
     r"menghubungi(?: saya| aku)?)", lambda m: ("pesan", "", "")),
    (rf"(?:ada )?(?:pesan|chat|wa|whatsapp|balasan) (?:baru )?(?:dari|dr) {_NAME}", lambda m: ("pesan", m[1], "")),
    (rf"{_NAME} (?:sudah |udah )?(?:membalas|balas|bales|balesin|ngebales|membalas pesan(?: saya| ku| aku)?|"
     r"mengirim pesan|kirim pesan|ngirim pesan|ngechat|chat|nge-?wa|wa|menghubungi(?: saya| aku)?)",
     lambda m: ("pesan", m[1], "")),
    (r"hujan(?:nya)? (?:sudah |udah )?(?:berhenti|reda|selesai|stop|tidak turun lagi)", lambda m: ("hujan_reda", "", "")),
    (r"(?:(?:mau|akan|sudah|udah|mulai) )?(?:turun )?hujan|hujan (?:turun|mulai|datang)", lambda m: ("hujan", "", "")),
    (r"(?:baterai|batre|batere|batrai|daya)(?: laptop| (?:saya|ku|aku))? (?:sudah |udah )?(?:penuh|full|terisi penuh)",
     lambda m: ("baterai", "", "penuh")),
    (r"(?:baterai|batre|batere|batrai|daya)(?: laptop| (?:saya|ku|aku))? (?:sudah |udah )?(?:di ?bawah|kurang dari|tinggal|"
     r"turun ke|sisa) (\d{1,3})(?: ?(?:persen|%))?", lambda m: ("baterai", "", f"<{m[1]}")),
    (r"(?:baterai|batre|batere|batrai|daya)(?: laptop| (?:saya|ku|aku))? (?:sudah |udah )?(?:di ?atas|lebih dari|sampai|"
     r"sudah|udah|mencapai|terisi) (\d{1,3})(?: ?(?:persen|%))?", lambda m: ("baterai", "", f">{m[1]}")),
    (r"(?:internet|wifi|wi-fi|koneksi|jaringan)(?:nya)? (?:sudah |udah )?(?:nyala|menyala|tersambung|terhubung|kembali|"
     r"normal|on|hidup|pulih|lancar)(?: lagi| kembali)?", lambda m: ("internet", "", "")),
    (r"(?:situs|website|web|laman) ([\w.-]+(?:\.[a-z]{2,})?)(?: sudah| udah)? (?:bisa (?:dibuka|diakses)|"
     r"normal|nyala|aktif|up|online)(?: lagi| kembali)?", lambda m: ("situs", m[1], "")),
    (r"(?:program |aplikasi |proses )?([\w.-]{2,30}) (?:sudah |udah )?(?:selesai|ditutup|berhenti|tertutup|beres|kelar)"
     r"(?: (?:jalan|berjalan|bekerja|diproses|memproses))?", lambda m: ("program", m[1], "")),
]
_PATTERNS = [(re.compile(p), f) for p, f in _PATTERNS]
ASK_RE = re.compile(r"^(?:tolong |coba |bisa |bisakah |mohon )?(?:kabari|kabarin|kabarkan|beri ?tahu|beritahu|kasih ?tahu|"
                    r"kasih ?tau|bilang(?:in)?|ingatkan|ingetin|panggil|info(?:kan|in)?)(?: (?:saya|aku|ku|gue|gw|tuan))?"
                    r"(?: ya)? (?:kalau|kalo|jika|bila|apabila|begitu|saat|ketika|pas|waktu|kapan|setelah|sesudah) (.+)$")
STOP_WORDS = {"saya", "aku", "dia", "ada", "yang", "itu", "ini", "sudah", "udah", "lagi", "semua", "nya"}


def parse(text: str) -> tuple[str, str, str] | None:
    """"kabari saya kalau Rina membalas" -> ("pesan", "rina", ""). None when it isn't such a request."""
    t = re.sub(r"[,!?]+|\.(?!\w)", " ", text.lower())          # (the dots inside "siakad.unila.ac.id" stay)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+(?:ya|dong|deh|tuan|digidaw|nanti|nanti ya)$", "", t)
    m = ASK_RE.match(t)
    if not m:
        return None
    condition = re.sub(r"\s+(?:ya|dong|deh|nanti)$", "", m[1]).strip()
    for pattern, make in _PATTERNS:
        hit = pattern.fullmatch(condition)
        if hit:
            kind, target, value = make(hit)
            target = target.strip()
            if kind in ("pesan", "program") and target in STOP_WORDS:
                continue
            if kind == "program" and target in ("unduhan", "download", "hujan", "baterai", "internet", "rapat", "meeting",
                                                "kelas", "kuliah", "film", "lagu", "video", "pertandingan"):
                continue                     # not a program on this PC
            return kind, target, value
    return None


# ───────────────────────────── the checks ─────────────────────────────

def _downloads_dir() -> Path:
    try:
        import system_control
        return Path(system_control._known_folder("downloads"))
    except Exception:
        return Path.home() / "Downloads"


def _partials(folder: Path) -> set[str]:
    try:
        return {p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PARTIAL}
    except OSError:
        return set()


def _check_download(w: dict) -> str | None:
    folder = Path(w["state"].setdefault("folder", str(_downloads_dir())))
    now_partial = _partials(folder)
    seen = w["state"].setdefault("seen", [])
    for name in now_partial:
        if name not in seen:
            seen.append(name)
    if now_partial:
        w["state"]["active"] = True
        return None
    if not w["state"].get("active"):
        if time.time() - w["created"] > DOWNLOAD_WAIT:
            return f"{T}, saya tidak melihat unduhan yang berjalan di folder Downloads, jadi saya berhenti memantaunya."
        return None
    names = []
    for name in seen:
        final = folder / re.sub(r"\.(?:part|crdownload|download|partial|aria2|opdownload|tmp)$", "", name, flags=re.I)
        if final.exists() and not final.name.lower().startswith("unconfirmed"):
            names.append(final.name)
    if not names:                            # Chrome renames "Unconfirmed 123.crdownload": the newest file then
        try:
            newest = max((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
            if newest.stat().st_mtime >= w["created"] - 60:
                names.append(newest.name)
        except (OSError, ValueError):
            pass
    if not names:
        return f"{T}, unduhan di folder Downloads sudah selesai."
    if len(names) == 1:
        return f"{T}, unduhan {names[0]} sudah selesai."
    return f"{T}, {len(names)} unduhan sudah selesai, termasuk {names[-1]}."


def _rain_now() -> bool | None:
    try:
        import weather
        where = weather.place()
        data = weather.forecast(where) if where else None
        return weather._raining(data) if data else None
    except Exception:
        return None


def _check_rain_stop(w: dict) -> str | None:
    raining = _rain_now()
    if raining is None:
        return None
    if raining:
        w["state"]["dry"] = 0
        return None
    w["state"]["dry"] = w["state"].get("dry", 0) + 1
    return f"{T}, hujannya sudah reda." if w["state"]["dry"] >= 2 else None    # dry twice in a row: really stopped


def _check_rain_start(w: dict) -> str | None:
    try:
        import weather
        where = weather.place()
        data = weather.forecast(where) if where else None
        if not data:
            return None
        if weather._raining(data):
            return f"{T}, sekarang sudah mulai hujan."
        soon = weather.rain_soon(data, weather._local_now(data))
        if soon:
            return f"{T}, {soon[0]} kemungkinan hujan, peluangnya {soon[1]} persen."
    except Exception:
        return None
    return None


def _check_battery(w: dict) -> str | None:
    import psutil
    battery = psutil.sensors_battery()
    if not battery:
        return f"{T}, laptop ini tidak melaporkan baterainya, jadi saya berhenti memantaunya."
    pct, value = battery.percent, w["value"] or "penuh"
    if value == "penuh":
        if battery.power_plugged and pct >= 99.5 or pct >= 100:
            return f"{T}, baterai laptop sudah penuh. Charger boleh dicabut."
        if not battery.power_plugged:
            w["state"]["unplugged"] = w["state"].get("unplugged", 0) + 1
            if w["state"]["unplugged"] == 6:         # a minute unplugged: it won't get full like this
                return f"{T}, charger laptop tidak terpasang, baterai {pct:.0f} persen. Saya berhenti menunggu baterai penuh."
        return None
    limit = float(value[1:])
    if value.startswith("<") and pct <= limit:
        return f"{T}, baterai laptop tinggal {pct:.0f} persen."
    if value.startswith(">") and pct >= limit:
        return f"{T}, baterai laptop sudah {pct:.0f} persen."
    return None


def _matching_processes(name: str) -> list:
    import psutil
    key = name.lower().removesuffix(".exe")
    found = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            pname = (p.info["name"] or "").lower().removesuffix(".exe")
            cmd = " ".join(p.info["cmdline"] or [])[:300].lower()
        except Exception:
            continue
        if key == pname or (len(key) >= 4 and (key in pname or re.search(rf"(?:^|[/\s]){re.escape(key)}(?:\s|$)", cmd))):
            found.append(p.pid)
    return found


def _check_program(w: dict) -> str | None:
    pids = _matching_processes(w["target"])
    if "pids" not in w["state"]:
        w["state"]["pids"] = pids
        if not pids:
            return f"{T}, saya tidak menemukan program {w['target']} yang sedang berjalan."
        return None
    if not pids:
        return f"{T}, {w['target']} sudah selesai."
    return None


def _online() -> bool:
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=3).close()
            return True
        except OSError:
            continue
    return False


def _check_internet(w: dict) -> str | None:
    if _online():
        return f"{T}, internet sudah tersambung lagi."
    return None


def _site_url(target: str) -> str:
    t = target.strip().lower()
    if t.startswith("http"):
        return t
    return f"https://{t if '.' in t else t + '.com'}"


def _check_site(w: dict) -> str | None:
    import requests
    try:
        r = requests.get(_site_url(w["target"]), timeout=8, allow_redirects=True)
        if r.status_code < 500:
            return f"{T}, situs {w['target']} sudah bisa dibuka lagi."
    except requests.RequestException:
        pass
    return None


CHECKS = {"unduhan": _check_download, "hujan_reda": _check_rain_stop, "hujan": _check_rain_start,
          "baterai": _check_battery, "program": _check_program, "internet": _check_internet, "situs": _check_site}


# ───────────────────────────── messages (from notifications.py) ─────────────────────────────

def on_notification(item: dict) -> bool:
    """A notification came in: does a "pesan" watch wait for it? True = it was announced here (the usual "ada pesan
    dari …" is then skipped)."""
    try:
        import notifications
    except Exception:
        return False
    if not notifications._is_chat(item.get("app", "")):
        return False
    with _lock:
        waiting = [w for w in _watches if w["kind"] == "pesan"]
    for w in waiting:
        target = w["target"]
        if target and not notifications._from(item, [target.lower()]):
            continue
        name = notifications._name(item)
        notifications.offer_reading(notifications._chat(item)[0])       # "ya" then reads this chat
        notifications.note_chat(item)                                    # "balas: …" answers it
        text = f"{T}, {name} sudah {'membalas' if target else 'mengirim pesan'} di {notifications._label(item['app'])}. " \
               f"Mau saya bacakan?"
        threading.Thread(target=_finish, args=(w, text, True), daemon=True, name="watch-message").start()
        return True
    return False


# ───────────────────────────── running them ─────────────────────────────

def _finish(w: dict, text: str, listen: bool = False):
    with _lock:
        if w not in _watches:
            return
        _watches.remove(w)
    _publish()
    if ANNOUNCE:
        try:
            ANNOUNCE(text, "reminder", listen=config.FOLLOWUP_SECONDS if listen else 0)
        except TypeError:
            ANNOUNCE(text, "reminder")
    else:
        print(f"[watch] {text}")


def _loop():
    while True:
        time.sleep(CHECK_EVERY)
        now = time.time()
        with _lock:
            due = [w for w in _watches if w["kind"] in CHECKS and now - w["checked"] >= PACE.get(w["kind"], 30)]
            expired = [w for w in _watches if now > w["expires"]]
        for w in expired:
            with _lock:
                if w in _watches:
                    _watches.remove(w)
            print(f"[watch] berhenti memantau ({KIND_WORDS.get(w['kind'], w['kind'])}): sudah {MAX_HOURS[w['kind']]} jam")
            _publish()
        for w in due:
            if w in expired:
                continue
            w["checked"] = now
            try:
                text = CHECKS[w["kind"]](w)
            except Exception as e:
                print(f"[watch] {w['kind']}: {e}")
                continue
            if text:
                _finish(w, text)


def _publish():
    try:
        import events
        events.state["watches"] = snapshot()
        events.emit("watches", items=events.state["watches"])
    except Exception:
        pass


def snapshot() -> list[dict]:
    with _lock:
        return [{"id": w["id"], "kind": w["kind"], "label": describe(w), "since": w["created"]} for w in _watches]


def describe(w: dict) -> str:
    kind, target, value = w["kind"], w["target"], w["value"]
    if kind == "pesan":
        return f"pesan dari {target}" if target else "pesan masuk"
    if kind == "baterai":
        return "baterai penuh" if not value or value == "penuh" else \
            f"baterai {'di bawah' if value.startswith('<') else 'di atas'} {value[1:]} persen"
    if kind in ("program", "situs"):
        return f"{KIND_WORDS[kind].split()[0]} {target} {'selesai' if kind == 'program' else 'bisa dibuka'}"
    return KIND_WORDS.get(kind, kind)


def start():
    global _started
    if _started:
        return
    _started = True
    try:
        import notifications
        notifications.on_new(on_notification)
    except Exception as e:
        diag.warn_once("watchers.hook", "pantauan pesan tidak bisa dipasang", e)
    threading.Thread(target=_loop, daemon=True, name="watchers").start()


# ───────────────────────────── skills ─────────────────────────────

def watch_for(what: str, target: str = "", value: str = "") -> str:
    """Keep an eye on something and say it when it happens: "kabari saya kalau unduhan selesai",
    "kabari kalau Rina membalas", "kabari kalau hujan reda", "kabari kalau baterai penuh".
    what: unduhan | pesan | hujan_reda | hujan | baterai | program | internet | situs.
    target: the person (pesan), program name (program) or website (situs). value: for baterai "penuh", "<20" or ">80"."""
    kind = (what or "").strip().lower().replace(" ", "_")
    kind = {"download": "unduhan", "message": "pesan", "chat": "pesan", "rain_stop": "hujan_reda", "reda": "hujan_reda",
            "rain": "hujan", "battery": "baterai", "process": "program", "aplikasi": "program", "wifi": "internet",
            "website": "situs", "web": "situs"}.get(kind, kind)
    if kind not in MAX_HOURS:
        return f"Saya belum bisa memantau '{what}', {T}. Yang bisa: unduhan, pesan, hujan, baterai, program, internet, situs."
    target, value = (target or "").strip(), (value or "").strip().replace(" ", "")
    if kind in ("program", "situs") and not target:
        return f"Program atau situs apa yang perlu saya pantau, {T}?"
    if kind == "hujan_reda" and _rain_now() is False:
        return f"Saat ini tidak sedang hujan, {T}."
    if kind == "hujan" and _rain_now() is True:
        return f"Sekarang sedang hujan, {T}."
    if kind == "internet" and _online():
        return f"Internet sedang tersambung, {T}."
    if kind == "situs":
        w = {"target": target}
        if _check_site(w):
            return f"Situs {target} bisa dibuka sekarang, {T}."
    if kind == "baterai":
        if value and not re.fullmatch(r"penuh|[<>]\d{1,3}", value):
            value = "penuh"
        w = {"value": value, "state": {}}
        now_text = _check_battery(w)
        if now_text and "tidak melaporkan" in now_text:
            return now_text
    if kind == "program":
        if not _matching_processes(target):
            return f"Saya tidak menemukan program {target} yang sedang berjalan, {T}."
    now = time.time()
    w = {"id": uuid.uuid4().hex[:6], "kind": kind, "target": target, "value": value, "created": now, "checked": 0.0,
         "expires": now + MAX_HOURS[kind] * 3600, "state": {}}
    with _lock:
        same = [x for x in _watches if (x["kind"], x["target"].lower(), x["value"]) == (kind, target.lower(), value)]
        if same:
            return f"Itu sudah saya pantau, {T}."
        _watches.append(w)
    start()
    _publish()
    if kind == "unduhan" and not _partials(_downloads_dir()):
        return f"Baik, {T}. Saya kabari begitu unduhan di folder Downloads selesai."
    return f"Baik, {T}. Saya kabari kalau {describe(w)}."


def list_watches() -> str:
    """What Digidaw is keeping an eye on ("apa saja yang sedang kamu pantau")."""
    items = snapshot()
    if not items:
        return f"Tidak ada yang sedang saya pantau, {T}."
    return f"Saya sedang memantau: " + ", ".join(i["label"] for i in items) + "."


def cancel_watch(what: str = "") -> str:
    """Stop watching ("tidak usah dipantau lagi", "batalkan pantauan hujan"). what: a word of it; empty = all."""
    key = (what or "").lower().strip()
    with _lock:
        gone = [w for w in _watches if not key or key in describe(w) or key in w["kind"] or key in w["target"].lower()]
        for w in gone:
            _watches.remove(w)
    _publish()
    if not gone:
        return f"Tidak ada pantauan seperti itu, {T}."
    return f"Baik, {T}, saya berhenti memantau " + ", ".join(describe(w) for w in gone) + "."


def handle(text: str) -> str | None:
    """The fast route: a "kabari saya kalau …" Digidaw understands without the AI. None = not understood here."""
    parsed = parse(text)
    if not parsed:
        return None
    return watch_for(*parsed)


AI_TOOLS = [watch_for, list_watches, cancel_watch]
