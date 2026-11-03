"""
A DNS cache inside Digidaw. On a lossy network (measured 31 Oct 2026 on this laptop: 19 of 40 lookups of
www.google.com took 5-8 s; every server - the ISP's, 1.1.1.1, 8.8.8.8 - lost 15-33 % of the queries) glibc waits
5 s before it asks again. Every request starts with a lookup - Google speech recognition one per sentence - so a
lost packet became a 5-second pause before Digidaw had even understood the command.

- socket.getaddrinfo is wrapped: a host asked for before is answered from memory at once. An answer older than
  REFRESH is renewed in the background (the old one is used meanwhile); after EXPIRE it is looked up again.
- The hosts Digidaw needs all the time are looked up at start, before the first command (warm()).
- main.py also sets RES_OPTIONS, so the system resolver gives a lost query up after 1 s instead of 5.
Only TCP lookups of names are cached (what HTTP, websocket and API clients do); everything else goes straight through.
"""
import ipaddress
import socket
import threading
import time

REFRESH = 120                  # older than this: renewed in the background on the next use
EXPIRE = 6 * 3600              # older than this: not used any more (the network may have changed)
HOSTS = ("www.google.com", "speech.platform.bing.com", "generativelanguage.googleapis.com", "wttr.in",
         "api.open-meteo.com", "geocoding-api.open-meteo.com", "html.duckduckgo.com")
CACHEABLE_FLAGS = (0, socket.AI_ADDRCONFIG)

_real = socket.getaddrinfo
_cache: dict[tuple, tuple[float, list]] = {}       # (host, family) -> (when, results with port 0)
_renewing: set = set()
_lock = threading.Lock()


def _cacheable(host, port, type, proto, flags) -> bool:
    if not isinstance(host, str) or not host or host == "localhost" or host.endswith(".local"):
        return False
    if type != socket.SOCK_STREAM or proto not in (0, socket.IPPROTO_TCP) or flags not in CACHEABLE_FLAGS:
        return False
    if port is not None and not isinstance(port, int):
        return False                                # a service name ("https"): rare, let the real one handle it
    try:
        ipaddress.ip_address(host.split("%")[0])
        return False                                # already a number
    except ValueError:
        return True


def _lookup(host: str, family: int) -> list:
    return _real(host, None, family, socket.SOCK_STREAM, 0, socket.AI_ADDRCONFIG)


def _with_port(results: list, port) -> list:
    port = port or 0
    out = []
    for fam, kind, proto, canon, addr in results:
        out.append((fam, kind, proto, canon, (addr[0], port, *addr[2:])))
    return out


def _renew(key: tuple):
    try:
        results = _lookup(*key)
        with _lock:
            _cache[key] = (time.time(), results)
    except OSError:
        pass                                        # keep the old answer; tried again on a later use
    finally:
        with _lock:
            _renewing.discard(key)


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if not _cacheable(host, port, type, proto, flags):
        return _real(host, port, family, type, proto, flags)
    key = (host.lower().rstrip("."), family)
    with _lock:
        hit = _cache.get(key)
        age = time.time() - hit[0] if hit else None
        renew = hit is not None and age > REFRESH and key not in _renewing
        if renew:
            _renewing.add(key)
    if hit and age < EXPIRE:
        if renew:
            threading.Thread(target=_renew, args=(key,), daemon=True, name="dns-renew").start()
        return _with_port(hit[1], port)
    results = _lookup(*key)                         # (raises socket.gaierror like the real one)
    with _lock:
        _cache[key] = (time.time(), results)
    return _with_port(results, port)


def warm(hosts=HOSTS):
    """Look the usual hosts up now, in the background (each on its own thread: one lost query delays only itself)."""
    for host in hosts:
        threading.Thread(target=lambda h=host: _try(h), daemon=True, name="dns-warm").start()


def _try(host: str):
    try:
        getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
    except OSError:
        pass


def install():
    """Route every lookup in this process through the cache (call once, early)."""
    if socket.getaddrinfo is not getaddrinfo:
        socket.getaddrinfo = getaddrinfo
