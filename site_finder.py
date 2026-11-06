"""
The address of a site the user names but Digidaw doesn't know ("medanpedia", "lms kampus"): first the sites this user really
visits (the browsers' own history, read here on this PC, nothing is sent anywhere), then the usual domain endings.
"""
import difflib
import glob
import os
import shutil
import socket
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
import re

ENDINGS = ("com", "id", "co.id", "ac.id", "net", "org", "io")
DNS_SECONDS = 2.0
FIRST_RESULT_SECONDS = 3.0
_found: dict[str, str] = {}


def _hosts_in(path: str, query: str) -> list[tuple[str, int]]:
    """(url, visits) from a browser's history, read from a copy: the browser keeps its own file locked."""
    folder = tempfile.mkdtemp(prefix="digidaw-history-")
    try:
        copy = os.path.join(folder, "history.sqlite")
        shutil.copyfile(path, copy)
        if os.path.exists(path + "-wal"):
            shutil.copyfile(path + "-wal", copy + "-wal")
        with sqlite3.connect(copy) as db:
            return db.execute(query).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _from_history(key: str) -> str | None:
    home = os.path.expanduser("~")
    sources = [(p, "SELECT url, visit_count FROM moz_places WHERE visit_count > 0 AND url LIKE 'http%'")
               for pattern in (f"{home}/.mozilla/firefox/*/places.sqlite", f"{home}/.config/mozilla/firefox/*/places.sqlite")
               for p in glob.glob(pattern)]
    sources += [(p, "SELECT url, visit_count FROM urls WHERE visit_count > 0 AND url LIKE 'http%'")
                for pattern in (f"{home}/.config/google-chrome/*/History", f"{home}/.config/BraveSoftware/Brave-Browser/*/History",
                                f"{home}/.config/chromium/*/History")
                for p in glob.glob(pattern)]
    visits: dict[str, int] = {}
    for path, query in sources:
        for url, count in _hosts_in(path, query):
            host = (urlparse(url).hostname or "").removeprefix("www.")
            if key in re.sub(r"[.\-]", "", host):
                visits[host] = visits.get(host, 0) + (count or 1)
    return f"https://{max(visits, key=visits.get)}/" if visits else None


def _resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _from_dns(key: str) -> str | None:
    hosts = [f"{key}.{ending}" for ending in ENDINGS]
    pool = ThreadPoolExecutor(max_workers=len(hosts))
    results = [pool.submit(_resolves, host) for host in hosts]
    pool.shutdown(wait=False)
    for host, result in zip(hosts, results):
        try:
            if result.result(timeout=DNS_SECONDS):
                return f"https://{host}/"
        except Exception:
            continue
    return None


def first_result(words: str) -> str | None:
    """Google's first result for these words (its "I'm feeling lucky"). Only the address is asked for, no page is opened."""
    import requests
    try:
        answer = requests.get("https://www.google.com/search", params={"q": words, "btnI": "1"}, allow_redirects=False,
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=FIRST_RESULT_SECONDS)
    except requests.RequestException:
        return None
    location = answer.headers.get("Location", "")
    target = (parse_qs(urlparse(location).query).get("q") or [location])[0]
    host = urlparse(target).hostname or ""
    return target if target.startswith("http") and host and not re.search(r"(?:^|\.)google\.", host) else None


def _named_result(words: str, key: str) -> str | None:
    """The first result, only when its address really carries that name: "uneedpanel" brought up some TikTok video."""
    url = first_result(words)
    host = re.sub(r"[^a-z0-9]", "", host_of(url)) if url else ""
    if not host:
        return None
    return url if key in host or difflib.SequenceMatcher(None, key, host.split(".")[0]).ratio() >= 0.75 else None


def is_address(text: str) -> bool:
    return re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", text.strip()) is not None


def find(name: str, online: bool = False) -> str | None:
    """The address, or None. What is found stays known until Digidaw restarts. online: when the browsers' history and the usual
    endings don't know it, Google's first result (for "buka situs X", where X is certainly a site, not an app heard wrong)."""
    text = re.sub(r"\s+titik\s+", ".", name.strip().lower())
    if is_address(text):
        return text if text.startswith("http") else f"https://{text}"
    key = re.sub(r"[^a-z0-9]", "", text)
    if len(key) < 3:
        return None
    if key not in _found:
        url = _from_history(key) or _from_dns(key) or (_named_result(text, key) if online and len(text.split()) <= 3 else None)
        if not url:
            return None
        _found[key] = url
    return _found[key]


def host_of(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def visited(name: str) -> str | None:
    """A site the user goes to under exactly this name ("komiku": komiku.org), for a name that is no app. From the browsers'
    history only: a word heard wrong mustn't open whatever happens to exist on the internet under it."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(key) < 4:
        return None
    url = _from_history(key)
    return url if url and key in host_of(url).split(".") else None
