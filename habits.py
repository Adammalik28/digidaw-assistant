"""Habits Digidaw learns by itself: what the user opens and plays, and words speech recognition keeps mishearing.

- usage ...... every app / website / song the user opens or plays is counted, so the AI knows the favourites
               ("sering membuka firefox") and an ambiguous name picks the app the user really uses.
- aliases .... "bukan itu, maksud saya hotspot" after "buka ho" teaches ho -> hotspot; from then on the misheard
               word is replaced before the command is understood. A few known mishearings are built in.
"""
import json
import re
import threading
import time

import config

HABITS_FILE = config.BASE_DIR / "habits.json"
TOP_IN_PROMPT = 6
FAILED_RESULT = re.compile(r"tidak ditemukan|tidak ada|gagal|belum terpasang|tidak dikenal|error", re.IGNORECASE)
# misheard often enough to fix from the start; learned aliases are added to these
BUILTIN_ALIASES = {
    "white droid": "waydroid", "way droid": "waydroid", "wey droid": "waydroid", "weidroid": "waydroid",
    "wasap": "whatsapp", "watsap": "whatsapp", "whatsap": "whatsapp", "wapsi": "whatsapp",
    "work space": "workspace", "workspes": "workspace", "wokspace": "workspace",
    "aplikasi digital": "aplikasi digidaw", "jendela digital": "jendela digidaw",
}
TRACKED = {"open_application": ("app", "name"), "open_website": ("web", "site"), "play_youtube": ("music", "query"),
           "open_many": ("app", "names")}

# the name in a skill's result sentence: "Membuka Mousepad, KING." / "Memutar The Ronettes - Be My Baby, KING."
RESULT_NAME = {"open_application": re.compile(r"^(?:Membuka|Menjalankan) (.+?),"),
               "play_youtube": re.compile(r"^Memutar (.+?)(?: dalam mode mix)?,")}

_lock = threading.Lock()
_data: dict | None = None


def _load() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(HABITS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _data = {}
        _data.setdefault("usage", {})
        _data.setdefault("aliases", {})
    return _data


def _save():
    try:
        HABITS_FILE.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ───────────────────────────── usage ─────────────────────────────

def record(tool: str, kwargs: dict, result: str):
    """Count a successful open / play (called for every tool call)."""
    if tool not in TRACKED or FAILED_RESULT.search(str(result)):
        return
    kind, arg = TRACKED[tool]
    values = kwargs.get(arg) or []
    resolved = RESULT_NAME.get(tool) and RESULT_NAME[tool].match(str(result))
    if resolved and not isinstance(values, list):
        values = resolved[1]            # what really opened / played, not the misheard words ("mouse" -> Mousepad)
    for value in (values if isinstance(values, list) else [values]):
        name = re.sub(r"^(?:https?://)?(?:www\.)?|\.(?:com|co\.id|id|org|net)(?:/.*)?$", "", str(value).lower().strip())
        if not name:
            continue
        if tool == "open_many":                 # each part was an app or a website
            import system_control
            kind = "web" if system_control._match_key(name, system_control.WEBSITES) else "app"
        with _lock:
            entry = _load()["usage"].setdefault(kind, {}).setdefault(name, {"count": 0, "last": 0})
            entry["count"] += 1
            entry["last"] = int(time.time())
    with _lock:
        _save()


def usage_count(kind: str, name: str) -> int:
    with _lock:
        return _load()["usage"].get(kind, {}).get(name.lower().strip(), {}).get("count", 0)


def favourites(kind: str, n: int = TOP_IN_PROMPT) -> list[str]:
    with _lock:
        items = _load()["usage"].get(kind, {})
        ranked = sorted(items.items(), key=lambda kv: (kv[1]["count"], kv[1]["last"]), reverse=True)
    return [name for name, v in ranked[:n] if v["count"] >= 2]


# ───────────────────────────── aliases ─────────────────────────────

def aliases() -> dict[str, str]:
    with _lock:
        return {**BUILTIN_ALIASES, **_load()["aliases"]}


def learn_alias(heard: str, meant: str) -> bool:
    heard, meant = heard.lower().strip(), meant.lower().strip()
    if not heard or not meant or heard == meant or len(heard.split()) > 3:
        return False
    with _lock:
        _load()["aliases"][heard] = meant
        _save()
    return True


def forget_alias(heard: str) -> bool:
    with _lock:
        found = _load()["aliases"].pop(heard.lower().strip(), None) is not None
        if found:
            _save()
    return found


def apply_aliases(text: str) -> str:
    """Replace misheard words/phrases (whole words only, longest first)."""
    table = aliases()
    for heard in sorted(table, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", table[heard], text, flags=re.IGNORECASE)
    return text


# ───────────────────────────── for the AI ─────────────────────────────

def prompt_hint() -> str:
    lines = []
    for kind, label in (("app", "aplikasi yang sering dibuka"), ("web", "situs yang sering dibuka"),
                        ("music", "lagu/musik yang sering diputar")):
        fav = favourites(kind)
        if fav:
            lines.append(f"- {label}: {', '.join(fav)}")
    learned = _load()["aliases"]
    if learned:
        lines.append("- kata yang sering salah dengar (sudah dikoreksi otomatis): "
                     + ", ".join(f"'{k}' = {v}" for k, v in list(learned.items())[-10:]))
    if not lines:
        return ""
    return ("\nKebiasaan user (dipelajari otomatis; pakai untuk menebak maksud yang ambigu, misalnya 'putar lagu "
            "favorit saya' atau nama aplikasi yang tidak jelas):\n" + "\n".join(lines))


def summary() -> str:
    """What Digidaw has learned, in words (for 'apa yang kamu pelajari tentang saya')."""
    parts = []
    for kind, label in (("app", "Aplikasi favorit"), ("web", "Situs favorit"), ("music", "Musik favorit")):
        fav = favourites(kind, 5)
        if fav:
            parts.append(f"{label}: {', '.join(fav)}.")
    learned = _load()["aliases"]
    if learned:
        parts.append("Koreksi yang saya ingat: " + ", ".join(f"{k} berarti {v}" for k, v in learned.items()) + ".")
    return " ".join(parts) or "Saya belum mempelajari kebiasaan apa pun."
