"""
The address of the tab the user is looking at, on Linux.

Windows reads it from the address bar with UI Automation; Wayland has no equivalent, so the window title (the
page title) is looked up in the browser's own history: Firefox places.sqlite, Chromium-family History. The
databases are locked while the browser runs, so a copy is read. The most recently visited page with exactly
that title wins.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import configparser
from pathlib import Path

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")

CHROMIUM_ROOTS = {
    "chrome": [CONFIG / "google-chrome", CONFIG / "google-chrome-beta"],
    "brave": [CONFIG / "BraveSoftware" / "Brave-Browser"],
    "chromium": [CONFIG / "chromium"],
    "msedge": [CONFIG / "microsoft-edge"],
    "vivaldi": [CONFIG / "vivaldi"],
    "opera": [CONFIG / "opera"],
}
FIREFOX_ROOTS = [CONFIG / "mozilla" / "firefox", HOME / ".mozilla" / "firefox", HOME / ".librewolf"]


def _family(process: str, wm_class: str) -> str:
    text = f"{process} {wm_class}".lower()
    if "firefox" in text or "librewolf" in text:
        return "firefox"
    for family in ("brave", "chromium", "msedge", "edge", "vivaldi", "opera", "chrome"):
        if family in text:
            return "msedge" if family == "edge" else family
    return ""


def _firefox_profiles() -> list[Path]:
    profiles = []
    for root in FIREFOX_ROOTS:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(ini, encoding="utf-8")
        except configparser.Error:
            continue
        first = [parser[s].get("Default") for s in parser.sections() if s.startswith("Install") and parser[s].get("Default")]
        others = [parser[s].get("Path") for s in parser.sections() if s.startswith("Profile") and parser[s].get("Path")]
        for rel in first + others:
            path = Path(rel) if Path(rel).is_absolute() else root / rel
            if (path / "places.sqlite").exists() and path not in profiles:
                profiles.append(path)
    return profiles


def _chromium_histories(family: str) -> list[Path]:
    found = []
    for root in CHROMIUM_ROOTS.get(family, []):
        if root.is_dir():
            found += [p for p in root.glob("*/History") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _query_copy(db: Path, sql: str, params: tuple) -> list[tuple]:
    """Run a query on a copy of a (locked) browser database, WAL included."""
    with tempfile.TemporaryDirectory(prefix="digidaw-hist-") as tmp:
        copy = Path(tmp) / db.name
        try:
            shutil.copy2(db, copy)
            for suffix in ("-wal", "-journal"):
                extra = db.with_name(db.name + suffix)
                if extra.exists():
                    shutil.copy2(extra, copy.with_name(copy.name + suffix))
            con = sqlite3.connect(f"file:{copy}?immutable=0", uri=True, timeout=2)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            return []


def url_for_title(title: str, process: str, wm_class: str = "") -> str:
    """URL of the most recently visited page called `title` in that browser ('' when not found)."""
    title = re.sub(r"^\(\d+\+?\)\s*", "", (title or "").strip())          # "(3) WhatsApp" -> "WhatsApp"
    if not title:
        return ""
    family = _family(process, wm_class)
    candidates: list[tuple[int, str]] = []
    if family == "firefox":
        for profile in _firefox_profiles():
            rows = _query_copy(profile / "places.sqlite",
                               "SELECT last_visit_date, url FROM moz_places WHERE title = ? OR title = ? "
                               "ORDER BY last_visit_date DESC LIMIT 1", (title, f"({title})"))
            candidates += [(int(r[0] or 0), r[1]) for r in rows]
    elif family:
        for history in _chromium_histories(family):
            rows = _query_copy(history, "SELECT last_visit_time, url FROM urls WHERE title = ? "
                                        "ORDER BY last_visit_time DESC LIMIT 1", (title,))
            # Chromium counts microseconds since 1601, Firefox since 1970: only compare within one browser
            candidates += [(int(r[0] or 0), r[1]) for r in rows]
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else ""
