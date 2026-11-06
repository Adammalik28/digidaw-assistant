"""
Names as speech recognition hears them -> the app, site or window the user means.

"buka termi" (cut off) -> Terminal, "pindah ke disco" -> Discord, "tutup file" -> File manager (not Firefox),
"tutup tito" -> probably TikTok. Everything Digidaw knows is a candidate: the app aliases, installed apps,
websites, the user's favourites and the windows that are open right now. Each candidate gets a score:

  1.0   the exact name or alias
  0.93  what was heard is the start of the name ("termi", "chro", "disco")
  0.92  what was heard is the first word of a longer name ("file" -> "file manager")
  0.90  the name is one of the words heard ("whatsapp lost" -> whatsapp)
  <0.9  spelling / sound similarity ("tito" ~ "tiktok")

SURE and above: act at once. Between MAYBE and SURE: ask "Maksud Anda TikTok?" (a "ya" then runs it and teaches
Digidaw the mishearing). Below MAYBE: not resolved here (the AI gets the request).
"""
import difflib
import re
import time
from dataclasses import dataclass

SURE = 0.85
MAYBE = 0.66
# short forms people say ("buka IG") and that speech recognition writes for some names
SHORT = {"ig": "instagram", "insta": "instagram", "yt": "youtube", "fb": "facebook", "wa": "whatsapp",
         "tele": "telegram", "gmaps": "google maps", "gdrive": "google drive", "vscode": "code",
         "vs code": "code", "file explorer": "file manager", "explorer": "file manager",
         # WasIstLos: the name it goes by, and the name as Google writes it
         "whatsapp bisnis": "wasistlos", "whatsapp business": "wasistlos", "wa bisnis": "wasistlos", "wa business": "wasistlos",
         "wasit": "wasistlos", "wasit loss": "wasistlos", "wasit lost": "wasistlos", "wasit los": "wasistlos", "wasit lord": "wasistlos",
         "wasis": "wasistlos", "wasis los": "wasistlos", "wasis lost": "wasistlos", "wasis loss": "wasistlos", "wasis lord": "wasistlos",
         "whatsapp lost": "wasistlos", "whatsapp loss": "wasistlos", "warship lo": "wasistlos", "whatsapp los": "wasistlos"}
# words that are never a name on their own (a fragment like "ho" or "aplikasi" must not open something random)
NOT_NAMES = {"aplikasi", "app", "apps", "program", "jendela", "window", "tab", "ini", "itu", "dia", "semua", "lagi",
             "dong", "deh", "aja", "saja", "yang", "dan", "atau", "ke", "di", "the", "this", "that", "situs", "web"}

_PHONETIC = [("ph", "f"), ("v", "f"), ("z", "s"), ("q", "k"), ("x", "ks"), ("ck", "k"), ("c", "k"), ("sh", "s"),
             ("th", "t"), ("w", "u"), ("y", "i"), ("ee", "i"), ("oo", "u"), ("ou", "u"), ("ai", "e"), ("ei", "e")]


@dataclass
class Match:
    kind: str        # "app" | "site" | "window"
    key: str         # what the skill gets: an app alias / site key / window's app name
    label: str       # how it is said back: "TikTok", "File manager"
    score: float
    target: str = "" # what it really opens: two names for the same program are not a choice to ask about


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def phonetic(text: str) -> str:
    """A rough sound key, so spellings that sound alike compare alike ("whatsapp" ~ "watsap")."""
    s = _norm(text)
    for a, b in _PHONETIC:
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def similarity(heard: str, name: str) -> float:
    h, n = _norm(heard), _norm(name)
    if not h or not n:
        return 0.0
    if h == n:
        return 1.0
    if len(h) >= 3 and n.startswith(h):                       # cut off: "termi", "chro", "disco"
        return 0.93 if len(h) >= 4 or len(h) * 2 >= len(n) else 0.8
    words = name.lower().split()
    if len(words) > 1 and len(h) >= 3 and _norm(words[0]) == h:  # "file" -> "file manager"
        return 0.92
    if len(words) > 1 and len(h) >= 4 and _norm(words[-1]) == h:  # "code" -> "vs code"
        return 0.9
    heard_words = heard.lower().split()
    if len(heard_words) > 1 and len(n) >= 3 and any(_norm(w) == n for w in heard_words):
        return 0.9                                               # "whatsapp lost" -> whatsapp
    if len(h) < 3:
        return 0.0
    spelled = difflib.SequenceMatcher(None, h, n).ratio()
    sounded = difflib.SequenceMatcher(None, phonetic(heard), phonetic(name)).ratio()
    return max(spelled, sounded) * 0.9


def _pretty(key: str) -> str:
    try:
        import persona
        return persona.brand(key)
    except Exception:
        return key[:1].upper() + key[1:]


_cache = {"at": 0.0, "apps": [], "sites": []}


def _program(command: str) -> str:
    """'/usr/bin/whatsie %U' and 'whatsie' are the same program: compare by the program's own name."""
    import os
    first = (command or "").split()[0] if command else ""
    return os.path.basename(first.removeprefix("TERM:")) or first


def _installed(commands) -> str:
    """The program behind an app alias, if it exists on this PC ('' = not installed)."""
    import shutil
    for command in commands if isinstance(commands, (list, tuple)) else [commands]:
        program = str(command).split()[0] if command else ""
        if program.startswith(("http", "trash:")):
            return str(command)
        if program.startswith("TERM:") and shutil.which(program[5:]) or program.startswith("/") or \
                (program and shutil.which(program)):
            return _program(str(command))
    return ""


def _static_candidates() -> tuple[list[tuple], list[tuple]]:
    """(name, key, label, target) for installed apps and known sites; rebuilt at most once a minute."""
    if time.time() - _cache["at"] < 60:
        return _cache["apps"], _cache["sites"]
    import system_control as sc
    apps: dict[str, tuple] = {}
    for alias, commands in sc.APPS.items():
        target = _installed(commands)
        if target:                                    # "chromium" is an alias, but is it installed here?
            apps.setdefault(alias, (alias, alias, _pretty(alias), target))
    try:
        for app in sc._start_apps():
            name = app["Name"].lower()
            if "uninstall" not in name and "digidaw" not in name:
                apps.setdefault(name, (name, name, app["Name"], _program(app.get("Exec", name))))
    except Exception:
        pass
    try:
        import habits
        for fav in habits.favourites("app", 12):
            if fav in apps or fav in sc.APPS:
                apps.setdefault(fav, (fav, fav, _pretty(fav), _installed(sc.APPS.get(fav, fav)) or fav))
    except Exception:
        pass
    sites = [(s, s, _pretty(s), sc.WEBSITES[s]) for s in sc.WEBSITES]
    _cache.update(at=time.time(), apps=list(apps.values()), sites=sites)
    return _cache["apps"], sites


def _window_candidates() -> list[tuple]:
    try:
        import window_manager as wm
        out = []
        for w in wm.closable_windows():
            label = wm._app_label(w)
            for name in {w["process"].lower(), (w.get("class") or "").lower().split(".")[-1]} - {""}:
                if "digidaw" not in name:
                    out.append((name, name, label, w["process"].lower()))
        return out
    except Exception:
        return []


def _clean(heard: str) -> str:
    heard = re.sub(r"\b(?:aplikasi|app|program|situs|website|web|jendela|tab)\b", " ", (heard or "").lower())
    heard = re.sub(r"\b(?:dong|deh|ya|aja|saja|lagi|sekarang|nya)\b", " ", heard)
    heard = re.sub(r"[^\w\s.-]", " ", heard)
    return re.sub(r"\s+", " ", heard).strip()


def rank(heard: str, kinds=("app", "site", "window")) -> list[Match]:
    """Every candidate that scores at least MAYBE, best first (one entry per label)."""
    heard = _clean(heard)
    if not heard or heard in NOT_NAMES or len(_norm(heard)) < 2:
        return []
    heard = SHORT.get(heard, heard)
    apps, sites = _static_candidates()
    pools = {"app": apps, "site": sites, "window": _window_candidates() if "window" in kinds else []}
    try:
        import habits
        favourites = set(habits.favourites("app", 12)) | set(habits.favourites("web", 8))
    except Exception:
        favourites = set()
    best: dict[str, Match] = {}
    for kind in kinds:
        for name, key, label, target in pools[kind]:
            score = similarity(heard, name)
            if kind == "window":
                score = min(1.0, score + 0.02)        # what is open now is the likelier meaning
            elif key in favourites and score < 1.0:
                score += 0.015                         # what the user often uses is the likelier meaning
            if score >= MAYBE and (label not in best or score > best[label].score):
                best[label] = Match(kind, key, label, round(score, 3), _norm(target) or _norm(key))
    ranked = sorted(best.values(), key=lambda m: -m.score)
    seen, unique = set(), []
    for m in ranked:                                  # "File explorer" and "File manager" are one program
        if m.target not in seen:
            seen.add(m.target)
            unique.append(m)
    return unique


def resolve(heard: str, kinds=("app", "site", "window"), keep=None, sure_only: bool = False) -> tuple[str, list[Match]]:
    """("sure", [match]) | ("ask", [1-2 candidates]) | ("none", []). keep: only candidates it accepts count
    (e.g. "tutup ...": only what is open)."""
    ranked = [m for m in rank(heard, kinds) if keep is None or keep(m)]
    if not ranked:
        return "none", []
    top = ranked[0]
    rivals = [m for m in ranked[1:] if top.score - m.score < 0.04]     # (targets are already distinct)
    if top.score >= SURE and (not rivals or top.score == 1.0 > rivals[0].score):
        return "sure", [top]
    if sure_only:
        return "none", []
    return "ask", [top] + rivals[:1]
