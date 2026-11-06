"""
The small things a personal assistant is asked all day, answered at once and the same way every time: how loud and how bright,
how much disk is left, what is making the laptop slow, how fast the internet is, copy and paste, a stopwatch, the notes.

They used to go to the small local model, which has to pick something: "salin" became set_clipboard, "hapus teks" closed a browser tab.
The sentences that lead here are in routes.py (EVERYDAY).
"""
import datetime
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import threading
import time

import psutil
import requests

import config
import linux_desktop as ld
import safety
import system_control as sc

T = config.USER_TITLE


# ───────────────────────────── sound and screen ─────────────────────────────

def get_volume() -> str:
    """How loud the PC is set right now ('volume berapa')."""
    state = ld.get_volume()
    if not state:
        return f"Volume tidak terbaca, {T}."
    level, muted = state
    return f"Volume {level} persen{', sedang dibisukan' if muted else ''}, {T}."


def _brightness_now() -> int | None:
    match = re.search(r",(\d+)%,", ld.out(["brightnessctl", "-m"], timeout=4) or "")
    return int(match[1]) if match else None


def get_brightness() -> str:
    """How bright the laptop screen is ('kecerahan berapa')."""
    level = _brightness_now()
    return f"Kecerahan layar {level} persen, {T}." if level is not None else f"Kecerahan layar tidak terbaca, {T}."


def brightness_step(change: int) -> str:
    """Make the screen brighter or dimmer by this many percent ('naikkan kecerahan': 15, 'layar lebih redup': -15)."""
    level = _brightness_now()
    if level is None:
        return f"Kecerahan layar tidak terbaca, {T}."
    return sc.set_brightness(max(5, min(100, level + int(change))))     # (not to 0: a black screen looks like a broken laptop)


def dark_mode(on: bool = True) -> str:
    """Dark or light look for the apps that follow the desktop setting ('mode gelap', 'mode terang')."""
    done = ld.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark" if on else "default"], timeout=5)
    if not done or done.returncode != 0:
        return f"Tampilan tidak bisa diubah, {T}."
    return f"Tampilan {'gelap' if on else 'terang'}, {T}. Aplikasi yang mengikuti pengaturan desktop langsung ikut."


# ───────────────────────────── the machine ─────────────────────────────

def _gb(size: float) -> str:
    return f"{size / 1024 ** 3:.0f}" if size >= 10 * 1024 ** 3 else f"{size / 1024 ** 3:.1f}".replace(".", ",")


def disk_space() -> str:
    """How much room is left on each drive ('sisa penyimpanan berapa')."""
    parts, seen = [], set()
    for disk in psutil.disk_partitions():
        if disk.device in seen or not re.match(r"/(?:$|mnt/|media/|run/media/|home$)", disk.mountpoint):
            continue
        seen.add(disk.device)
        try:
            use = psutil.disk_usage(disk.mountpoint)
        except OSError:
            continue
        name = "disk utama" if disk.mountpoint == "/" else os.path.basename(disk.mountpoint).replace("_", " ")
        parts.append(f"{name} sisa {_gb(use.free)} GB dari {_gb(use.total)} GB")
    said = ", ".join(parts) or "penyimpanan tidak terbaca"
    return f"{said[:1].upper()}{said[1:]}, {T}."


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, hours, minutes = seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60
    parts = [f"{days} hari"] * bool(days) + [f"{hours} jam"] * bool(hours) + [f"{minutes} menit"] * bool(minutes)
    return " ".join(parts) or f"{seconds} detik"


def uptime() -> str:
    """How long the laptop has been on ('berapa lama laptop menyala')."""
    return f"Laptop sudah menyala {_duration(time.time() - psutil.boot_time())}, {T}."


# many processes make up one app; what the user knows is the app
_APP_OF_PROCESS = {"isolated web co": "Firefox", "web content": "Firefox", "webextensions": "Firefox", "privileged cont": "Firefox",
                   "rdd process": "Firefox", "socket process": "Firefox", "utility process": "Firefox", "firefox": "Firefox", "firefox-bin": "Firefox",
                   "forkserver": "Firefox", "chrome": "Chrome", "chrome_crashpad": "Chrome", "brave": "Brave", "discord": "Discord", "telegram": "Telegram",
                   "whatsie": "WhatsApp", "qtwebengineproc": "WhatsApp", "wasistlos": "WasIstLos", "webkitwebproces": "WasIstLos", "webkitnetworkpr": "WasIstLos",
                   "llama-server": "Otak lokal Digidaw", "ollama": "Otak lokal Digidaw", "kitty": "Terminal", "soffice.bin": "LibreOffice", "code": "VS Code"}
NOT_TO_CLOSE = {"Digidaw", "Otak lokal Digidaw", "Hyprland", "Terminal", "systemd", "pipewire", "pipewire-pulse", "wireplumber", "waybar", "Xwayland", "cava"}


def _usage(seconds: float = 0.8) -> list[dict]:
    """Each app with its CPU (share of the whole machine) and memory, the heaviest first."""
    processes = list(psutil.process_iter(["name", "cmdline"]))
    for process in processes:
        try:
            process.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(seconds)
    cores = psutil.cpu_count() or 1
    apps: dict[str, dict] = {}
    for process in processes:
        try:
            if process.pid == os.getpid() or "main.py" in " ".join(process.info["cmdline"] or []) and "digidaw" in " ".join(process.info["cmdline"] or []):
                name = "Digidaw"
            else:
                raw = (process.info["name"] or "").lower()
                name = _APP_OF_PROCESS.get(raw) or _APP_OF_PROCESS.get(raw[:15]) or process.info["name"] or ""
            if process.memory_info().rss < 20 * 1024 ** 2 or not name or process.username() == "root":
                continue
            try:
                memory = process.memory_full_info().uss       # what only it holds: the many processes of a browser share a lot
            except psutil.Error:
                memory = process.memory_info().rss
            app = apps.setdefault(name, {"name": name, "cpu": 0.0, "ram": 0})
            app["cpu"] += process.cpu_percent(None) / cores
            app["ram"] += memory
        except psutil.Error:
            continue
    return sorted(apps.values(), key=lambda a: a["ram"] / psutil.virtual_memory().total * 100 + a["cpu"] * 2, reverse=True)


def heavy_apps() -> str:
    """What is using the laptop most right now ('aplikasi apa yang berat', 'kenapa laptop lambat')."""
    top = _usage()[:3]
    if not top:
        return f"Pemakaiannya tidak terbaca, {T}."
    said = ", ".join(f"{a['name']} dengan RAM {_gb(a['ram'])} GB dan CPU {a['cpu']:.0f} persen" for a in top)
    return (f"Paling berat sekarang: {said}. Keseluruhan CPU {psutil.cpu_percent(None):.0f} persen dan RAM {psutil.virtual_memory().percent:.0f} persen, {T}.")


def close_heaviest() -> str:
    """Close the app using the most, after asking ('tutup aplikasi yang berat'). Never Digidaw, the desktop or a terminal."""
    for app in _usage():
        if app["name"] not in NOT_TO_CLOSE:
            return safety.guard(f"menutup {app['name']} (RAM {_gb(app['ram'])} GB)", sc.close_application, name=app["name"].lower())
    return f"Tidak ada aplikasi berat yang bisa ditutup, {T}."


# ───────────────────────────── internet and updates ─────────────────────────────

SPEED_URL = "https://speed.cloudflare.com/__down?bytes=30000000"
SPEED_SECONDS = 6.0


def _latency() -> int | None:
    best = None
    for _ in range(3):
        started = time.time()
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=2):
                pass
        except OSError:
            continue
        taken = (time.time() - started) * 1000
        best = taken if best is None else min(best, taken)
    return round(best) if best is not None else None


def internet_speed() -> str:
    """Measure the internet right now: how fast it downloads and how quickly it answers ('tes kecepatan internet', 'internet lambat gak')."""
    latency = _latency()
    if latency is None:
        return f"Tidak ada koneksi internet, {T}."
    got, started = 0, time.time()
    try:
        with requests.get(SPEED_URL, stream=True, timeout=5) as answer:
            answer.raise_for_status()
            for chunk in answer.iter_content(64 * 1024):
                got += len(chunk)
                if time.time() - started > SPEED_SECONDS:
                    break
    except requests.RequestException:
        if not got:
            return f"Kecepatan tidak bisa diukur, {T}. Internet tersambung, responsnya {latency} milidetik."
    speed = got * 8 / max(time.time() - started, 0.1) / 1e6
    verdict = "lambat" if speed < 5 else "cukup" if speed < 25 else "kencang"
    return f"Internet {verdict}, {T}: unduh sekitar {speed:.0f} megabit per detik, respons {latency} milidetik."


def check_updates() -> str:
    """How many system updates are waiting ('cek update'). Installing them is system_update."""
    if not shutil.which("checkupdates"):
        return f"Pengecek update (pacman-contrib) belum terpasang, {T}."
    waiting = [line.split()[0] for line in (ld.out(["checkupdates"], timeout=30) or "").splitlines() if line.strip()]
    if not waiting:
        return f"Sistem sudah terbaru, {T}."
    return f"Ada {len(waiting)} pembaruan paket, {T}, termasuk {', '.join(waiting[:3])}. Bilang update sistem kalau mau dipasang."


def system_update() -> str:
    """Update Arch ('update sistem', 'perbarui sistem'): says how many packages wait and asks; after "ya" and the password in the system's window it runs in the background and reports when done."""
    import sysadmin
    return sysadmin.system_update()


# ───────────────────────────── keyboard ─────────────────────────────

TERMINALS = {"kitty", "alacritty", "foot", "konsole", "wezterm", "org.gnome.console", "gnome-terminal-server", "xterm"}
EDIT_KEYS = {"copy": "ctrl+c", "cut": "ctrl+x", "paste": "ctrl+v", "undo": "ctrl+z", "redo": "ctrl+shift+z", "save": "ctrl+s", "select_all": "ctrl+a"}
TERMINAL_KEYS = {"copy": "ctrl+shift+c", "paste": "ctrl+shift+v", "clear": "ctrl+u"}      # ctrl+c would stop what's running there
EDIT_SAID = {"copy": "Disalin", "cut": "Dipotong", "paste": "Ditempel", "undo": "Dibatalkan", "redo": "Diulang", "save": "Disimpan",
             "select_all": "Semua dipilih", "clear": "Teksnya dihapus"}


def edit_action(action: str) -> str:
    """Edit in the active window with the right keys for it. action: copy, cut, paste, undo, redo, save, select_all, clear."""
    action = action.lower().strip()
    if action not in EDIT_SAID:
        return f"Perintah {action} belum saya kenal, {T}."
    if action in ("copy", "cut"):
        _sync_clipboard()
    window = ld.active_window() or {}
    in_terminal = {str(window.get("class", "")).lower(), str(window.get("initialClass", "")).lower()} & TERMINALS
    if in_terminal:
        if action not in TERMINAL_KEYS:
            return f"Itu tidak ada di terminal, {T}."
        sc.press_keys(TERMINAL_KEYS[action])
    elif action == "clear":
        sc.press_keys("ctrl+a")
        sc.press_keys("backspace")
    else:
        sc.press_keys(EDIT_KEYS[action])
    if action in ("copy", "cut"):
        threading.Timer(0.15, _sync_clipboard).start()
    return f"{EDIT_SAID[action]}, {T}."


# ───────────────────────────── clipboard history, OCR & dev tools ─────────────────────────────

MAX_CLIP_HISTORY = 10
_clip_history: list[str] = []
_clip_lock = threading.Lock()
# what the history must not keep: it is read out on request, and what Digidaw says also goes to the log, the voice
# service and the next question to the AI
_SECRET_TEXT_RE = re.compile(
    r"\b(?:ghp_|gho_|ghs_|github_pat_|sk-|AIza|xox[abpr]-|glpat-|ya29\.)[\w-]{16,}|\beyJ[\w-]{10,}\.[\w-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"\b(?:password|passwd|kata sandi)\b|(?<![a-z])(?:sandi|api[ _-]?key|secret|token)\s*[:=]", re.IGNORECASE)
_NOT_SECRET_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+|(?:~|\.{0,2})/\S*|[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)   # a link, a path, an e-mail address


def _looks_secret(text: str) -> bool:
    """A key or a password, as far as one can tell from the text. A wrong yes costs one line of history; a wrong no, a
    password said out loud. (It used to be any word of 28+ letters: every link and path was dropped, "MyP@ssw0rd2026" kept.)"""
    if text.startswith("__digidaw_") or _SECRET_TEXT_RE.search(text):     # (__digidaw_: screen_context's own marker)
        return True
    if re.search(r"\s", text) or len(text) < 8 or _NOT_SECRET_RE.fullmatch(text):
        return False
    if re.fullmatch(r"[A-Fa-f0-9]{24,}", text) or (len(text) >= 28 and re.search(r"\d", text) and re.fullmatch(r"[\w+/=-]+", text)):
        return True                      # a hash, a generated key
    if re.fullmatch(r"[A-Za-z0-9]+", text):
        return bool(re.search(r"[A-Za-z]", text) and re.search(r"\d", text))   # letters and digits in one word: "hunter22"
    if re.fullmatch(r"[a-z0-9._-]+", text):
        return False                     # a name: test_control.py, python3.12, 10.0.0.18
    kinds = [r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"]
    return sum(bool(re.search(kind, text)) for kind in kinds) >= 3


def _clip_text(primary: bool = False) -> str:
    """The clipboard (or the highlighted text) as text; empty when it holds a picture."""
    try:
        return (ld.clipboard_get(primary=primary) or "").strip()
    except Exception:
        return ""


def _sync_clipboard() -> list[str]:
    """Record the current clipboard text in the ring buffer (ignoring long blobs and secrets)."""
    cur = _clip_text()
    if not cur or len(cur) > 4000 or _looks_secret(cur):
        return _clip_history
    with _clip_lock:
        if not _clip_history or _clip_history[0] != cur:
            if cur in _clip_history:
                _clip_history.remove(cur)
            _clip_history.insert(0, cur)
            del _clip_history[MAX_CLIP_HISTORY:]
    return _clip_history


def _clip_preview(text: str, limit: int = 45) -> str:
    one = re.sub(r"\s+", " ", text.strip())
    link = re.fullmatch(r"https?://(?:www\.)?([^/\s?#]+)\S*", one)
    if link:
        return f"link {link[1]}"         # (said aloud, the rest of an address is only noise)
    if re.fullmatch(r"(?:~|\.{0,2})/\S+", one):
        return f"file {os.path.basename(one.rstrip('/'))}"
    return one if len(one) <= limit else one[:limit].rstrip() + "..."


def clipboard_history() -> str:
    """Read recent items copied to the clipboard ('riwayat clipboard', 'apa saja yang saya copy tadi')."""
    items = _sync_clipboard()
    if not items:
        return f"Belum ada riwayat clipboard, {T}."
    parts = [f"ke-{i}: {_clip_preview(txt, 40)}" for i, txt in enumerate(items[:5], 1)]
    return f"Ada {len(items)} salinan di riwayat clipboard, {T}. " + "; ".join(parts) + "."


def paste_previous(index: int = 2) -> str:
    """Paste an earlier item from the clipboard history ('tempel salinan kedua', 'paste yang sebelumnya').
    index: 1 = latest, 2 = previous (default), 3 = third."""
    items = _sync_clipboard()
    idx = max(1, int(index))
    if not items:
        return f"Riwayat clipboard masih kosong, {T}."
    if idx > len(items):
        return f"Hanya ada {len(items)} salinan di riwayat clipboard, {T}."
    chosen = items[idx - 1]
    if not ld.clipboard_set(chosen):
        return f"Clipboard tidak bisa diubah, {T}."
    edit_action("paste")
    return f"Salinan ke-{idx} ditempel ({_clip_preview(chosen, 35)}), {T}."


def _ocr_lang() -> str:
    out = ld.out(["tesseract", "--list-langs"], timeout=5)
    langs = [line.strip() for line in out.splitlines()[1:] if line.strip() and line.strip() != "osd"]
    if "ind" in langs and "eng" in langs:
        return "ind+eng"
    for preferred in ("ind", "eng", "afr"):
        if preferred in langs:
            return preferred
    return langs[0] if langs else ""


def _ocr_screen_text() -> tuple[str, str]:
    """(extracted_text, error_or_offer) from the current monitor via tesseract."""
    import tempfile
    import pc_control
    import sysadmin
    if not shutil.which("tesseract"):
        return "", sysadmin.offer_install("tesseract tesseract-data-ind tesseract-data-eng", "OCR pembaca teks layar")
    lang = _ocr_lang()
    if not lang:
        return "", sysadmin.offer_install("tesseract-data-ind tesseract-data-eng", "Data bahasa OCR")
    try:
        image = pc_control.focused_picture(hide_own=True)[0]     # (whole size and no JPEG: small letters stay readable)
    except Exception:
        return "", f"Layar tidak bisa ditangkap saat ini, {T}."
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        image.save(tmp.name, format="PNG")
        res = ld.run(["tesseract", tmp.name, "stdout", "-l", lang, "--psm", "3"], timeout=15)
        if res is None:                  # (it took too long)
            return "", f"Teks di layar tidak sempat terbaca, {T}."
        return (res.stdout or "").strip(), ""
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def copy_screen_text() -> str:
    """Copy visible text from the screen to the clipboard using OCR ('salin teks di layar', 'ocr layar')."""
    text, err = _ocr_screen_text()
    if err:
        return err
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    words = cleaned.split()
    if not words:
        return f"Tidak ada teks yang terbaca jelas di layar, {T}."
    ld.clipboard_set(cleaned)
    _sync_clipboard()
    return f"{len(words)} kata dari layar sudah disalin ke clipboard, {T}. Cuplikannya: {_clip_preview(cleaned, 70)}."


def _kill_port_pid(pid: int, name: str, port: int) -> str:
    try:
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=2)
        except psutil.TimeoutExpired:
            p.kill()
        return f"Proses {name} (PID {pid}) di port {port} sudah dihentikan, {T}."
    except psutil.NoSuchProcess:
        return f"Proses {name} di port {port} sudah berhenti, {T}."
    except Exception as e:
        return f"Gagal menghentikan {name} di port {port}, {T}: {e}"


# who usually listens there: a process of another user (root, postgres, ollama) shows no name to Digidaw
KNOWN_PORTS = {22: "SSH", 53: "DNS", 80: "server web", 443: "server web", 631: "layanan printer", 3306: "MySQL",
               5432: "PostgreSQL", 6379: "Redis", 11434: "Ollama", 27017: "MongoDB"}
MOST_PORTS_SAID = 6


def _port_open(port: int) -> bool:
    """Something listens there, whoever it is."""
    return bool(ld.out(["ss", "-tulnH", f"sport = :{port}"], timeout=5))


def _find_port_owner(port: int) -> tuple[int, str] | None:
    try:
        conns = psutil.net_connections(kind="inet")
        listening = [c for c in conns if c.laddr and c.laddr.port == port and c.status == "LISTEN"]
        hit = (listening or [c for c in conns if c.laddr and c.laddr.port == port] or [None])[0]
        if hit and hit.pid:
            return hit.pid, psutil.Process(hit.pid).name()
    except Exception:
        pass
    out = ld.out(["ss", "-tlpn", f"sport = :{port}"], timeout=5)
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
    if m:
        return int(m[2]), m[1]
    return None


def port_process(port: int, kill: bool = False) -> str:
    """Check which process uses a network port, or stop it ('siapa pakai port 8080', 'matikan port 3000').
    port: port number. kill: True to stop that process (asks confirmation first)."""
    port = int(port)
    if not 1 <= port <= 65535:
        return f"Nomor port {port} tidak valid, {T}."
    owner = _find_port_owner(port)
    if not owner:
        if not _port_open(port):
            return f"Tidak ada proses yang memakai port {port}, {T}."
        # (Ollama and PostgreSQL were "tidak ada proses" here: their owner just isn't visible to this user)
        usual = f", biasanya {KNOWN_PORTS[port]}" if port in KNOWN_PORTS else ""
        if kill:
            return f"Port {port} dipakai proses milik sistem{usual}, {T}. Tidak bisa saya hentikan dari sini; matikan saja layanannya."
        return f"Port {port} terbuka{usual}, {T}, tapi prosesnya milik sistem jadi namanya tidak terlihat dari sini."
    pid, name = owner
    if not kill:
        return f"Port {port} sedang dipakai oleh {name} dengan PID {pid}, {T}."
    if pid == os.getpid() or port in (getattr(config, "HUD_PORT", 8765), getattr(config, "MOBILE_PORT", 8766)):
        return f"Port {port} dipakai oleh Digidaw sendiri, {T}, jadi tidak saya matikan."
    return safety.guard(f"menghentikan proses {name} (PID {pid}) di port {port}", _kill_port_pid,
                        pid=pid, name=name, port=port)


def _listening() -> list[dict]:
    """The TCP ports open on this PC: [{port, address, pid, name}], one row per port (a process of another user has no name)."""
    rows: dict[int, dict] = {}
    for line in ld.out(["ss", "-tlnpH"], timeout=5).splitlines():
        fields = line.split()
        if len(fields) < 4 or ":" not in fields[3]:
            continue
        address, _, number = fields[3].rpartition(":")
        if not number.isdigit():
            continue
        owner = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        row = {"port": int(number), "address": address, "pid": int(owner[2]) if owner else 0, "name": owner[1] if owner else ""}
        seen = rows.get(row["port"])
        if not seen or (_local_only(seen["address"]) and not _local_only(address)):     # (IPv4 and IPv6: the widest one counts)
            rows[row["port"]] = row
    return sorted(rows.values(), key=lambda r: r["port"])


def _local_only(address: str) -> bool:
    """Not reachable from other devices: this PC itself, or its own virtual networks (virtual machines, Docker)."""
    ip = address.split("%")[0].strip("[]")
    if ip.startswith("127.") or ip in ("::1", "localhost"):
        return True
    return any(name.startswith(("lo", "virbr", "docker", "br-", "veth")) and any(a.address == ip for a in addresses)
               for name, addresses in psutil.net_if_addrs().items())


def open_ports() -> str:
    """Which network ports are open on this PC, and which of them other devices can reach ('port apa saja yang terbuka', 'daftar port')."""
    import persona
    rows = _listening()
    if not rows:
        return f"Tidak ada port TCP yang terbuka, {T}."

    def label(row: dict) -> str:
        who = "Digidaw" if row["pid"] == os.getpid() else (persona.brand(row["name"]) if row["name"] else KNOWN_PORTS.get(row["port"], "proses sistem"))
        return f"{row['port']} {who}"

    def listed(items: list[dict]) -> str:
        more = len(items) - MOST_PORTS_SAID
        return ", ".join(label(r) for r in items[:MOST_PORTS_SAID]) + (f", dan {more} lainnya" if more > 0 else "")
    reachable = [r for r in rows if not _local_only(r["address"])]
    local = [r for r in rows if _local_only(r["address"])]
    parts = [f"Ada {len(rows)} port TCP terbuka, {T}."]
    parts.append(f"Bisa dicapai dari jaringan: {listed(reachable)}." if reachable else "Tidak ada yang bisa dicapai dari jaringan.")
    if local:
        parts.append(f"Hanya di komputer ini: {listed(local)}.")
    return " ".join(parts)


def _run_user_service(unit: str, act: str, label: str) -> str:
    verb_done = {"start": "dinyalakan", "stop": "dihentikan", "restart": "di-restart"}[act]
    res = ld.run(["systemctl", "--user", act, unit], timeout=15)
    if res and res.returncode == 0:
        return f"Layanan user {label} sudah {verb_done}, {T}."
    err = ((res.stderr or res.stdout or "") if res else "").strip().splitlines()
    return f"Gagal mengubah layanan {label}, {T}: {err[-1] if err else 'tidak ada jawaban dari systemctl'}."


def _unit_state(unit: str, user: bool) -> tuple[str, str]:
    """(LoadState, ActiveState). `systemctl show` always answers; `is-active` exits with an error for a stopped or failed
    service, and ld.out empties that, so a failed swaync was "tidak ditemukan"."""
    out = ld.out(["systemctl", *(["--user"] if user else []), "show", unit, "-p", "LoadState", "-p", "ActiveState"], timeout=5)
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return values.get("LoadState", ""), values.get("ActiveState", "")


def _find_service(name: str) -> tuple[str, bool, list[str]]:
    """(unit, is it the user's, other units it could be). The exact name first; a part of it is enough when one fits
    ("hermes" is hermes-gateway.service). unit "" = no such service, or (with the list) more than one fits."""
    unit = f"{name}.service"
    for user in (True, False):
        if _unit_state(unit, user)[0] == "loaded":
            return unit, user, []
    for user in (True, False):
        out = ld.out(["systemctl", *(["--user"] if user else []), "list-unit-files", "--no-legend", "--type=service", f"*{name}*"], timeout=5)
        names = [line.split()[0] for line in out.splitlines() if line.strip() and not line.split()[0].endswith("@.service")]
        if len(names) == 1:
            return names[0], user, []
        if names:
            return "", user, names
    return "", False, []


def service_control(name: str, action: str = "status") -> str:
    """Check status, start, stop, or restart a systemd service ('status service docker', 'restart layanan bluetooth',
    'matikan service postgresql'). name: service name. action: 'status', 'start', 'stop', 'restart'."""
    import sysadmin
    clean = re.sub(r"^(?:service|servis|layanan|daemon)\s+", "", (name or "").strip().lower())
    clean = re.sub(r"\.service$", "", clean).strip()
    if not clean or not re.fullmatch(r"[\w@.-]+", clean):
        return f"Nama layanan '{name}' tidak valid, {T}."
    act = (action or "status").strip().lower()
    act = {"nyalakan": "start", "hidupkan": "start", "mulai": "start", "jalankan": "start",
           "matikan": "stop", "hentikan": "stop", "mulai ulang": "restart", "cek": "status"}.get(act, act)
    if act not in ("status", "start", "stop", "restart"):
        act = "status"
    unit, is_user, others = _find_service(clean)
    if not unit:
        if others:
            return f"Ada beberapa layanan yang mirip {clean}, {T}: {', '.join(n.removesuffix('.service') for n in others[:5])}. Yang mana?"
        return f"Layanan {clean} tidak ada di komputer ini, {T}."
    label = unit.removesuffix(".service")
    if act == "status":
        state = _unit_state(unit, is_user)[1]
        said = {"active": "aktif dan berjalan", "inactive": "sedang mati", "failed": "gagal berjalan",
                "activating": "sedang menyala", "deactivating": "sedang berhenti"}.get(state, f"berstatus {state or 'tidak diketahui'}")
        return f"Layanan {'user ' if is_user else ''}{label} saat ini {said}, {T}."
    if label == "digidaw" and act == "stop":
        return f"Layanan Digidaw sendiri tidak saya matikan dari sini, {T}."
    verb = {"start": "menyalakan", "stop": "menghentikan", "restart": "me-restart"}[act]
    if is_user:
        return safety.guard(f"{verb} layanan user {label}", _run_user_service, unit=unit, act=act, label=label)
    return sysadmin.run_as_root(f"systemctl {act} {unit}")


# what an error looks like, not any text with the word in it: at 15:30 on 6 Nov the text still highlighted (a summary
# that mentions "error" and "traceback") was explained, and "jelaskan error ini" answered that all was well
_ERROR_LINE_RE = re.compile(
    r"^\s*(?:Traceback \(most recent call last\)|[\w.]*(?:Error|Exception)\b|fatal\b|panic\b|E:\s|FAILED\b|failed\b|gagal\b|npm ERR)|"
    r"^\S+: (?:cannot|could not|error|failed|tidak (?:dapat|bisa))\b|"
    r"\b(?:command not found|perintah tidak ditemukan|(?:berkas|file|folder|modul|paket) tidak ditemukan|No such file or directory|"
    r"Permission denied|Segmentation fault|core dumped|exit (?:code|status) [1-9])\b", re.IGNORECASE | re.MULTILINE)
_NO_HELP_RE = re.compile(r"tidak (?:bisa|dapat) membantu|tidak ada (?:konteks|error)|minta maaf|maaf, ", re.IGNORECASE)
_EXPLAIN_SYSTEM = ("Kamu ahli Linux Arch dan pemrograman. Pengguna memberimu pesan error dari layar. Pesan itu sudah cukup: selalu sebutkan "
                   "penyebabnya lalu perintah atau langkah untuk memperbaikinya, dalam dua kalimat bahasa Indonesia lisan tanpa markdown. "
                   "Jangan menolak dan jangan minta informasi tambahan.")
_EXPLAIN_EXAMPLES = [("Pesan error:\nModuleNotFoundError: No module named 'flask'",
                      "Modul flask belum terpasang di Python yang dipakai. Pasang dengan perintah pip install flask."),
                     ("Pesan error:\nzsh: permission denied: ./run.sh",
                      "Berkas run.sh belum boleh dijalankan. Beri izinnya dengan chmod +x run.sh lalu jalankan lagi.")]


def _error_part(text: str, before: int = 900, after: int = 500) -> str:
    """The part of a long text around its last error: the lines before it say where, those after it often why."""
    at = [m.start() for m in _ERROR_LINE_RE.finditer(text)]
    return text[max(0, at[-1] - before):at[-1] + after] if at else ""


def explain_last_error() -> str:
    """Explain the error currently selected, in the clipboard, or visible in the active terminal/screen, and how to fix it
    ('jelaskan error ini', 'kenapa perintah tadi gagal', 'error apa ini')."""
    import ai_skills
    import local_llm
    source = _error_part(_clip_text(primary=True)) or _error_part(_clip_text())
    if not source:
        source = _error_part(_ocr_screen_text()[0])
    if source and local_llm.available():
        # (with a plain instruction the small model answered 4 of 6 errors with "tidak ada konteks"; like this, 10 of 10)
        reply = local_llm.generate(_EXPLAIN_SYSTEM, f"Pesan error:\n{source}", max_tokens=160, temperature=0.2, history=_EXPLAIN_EXAMPLES)
        if reply and not _NO_HELP_RE.search(reply):
            return reply.replace("*", "").replace("`", "").strip()
    return ai_skills.analyze_screen("Jelaskan singkat error yang terlihat di layar ini dan cara memperbaikinya.")


# ───────────────────────────── stopwatch ─────────────────────────────

_watch = {"started": 0.0, "kept": 0.0}


def stopwatch(action: str = "start") -> str:
    """A stopwatch. action: 'start', 'stop' (says the time), 'read' (the time so far)."""
    action = action.lower().strip()
    running = _watch["started"] > 0
    elapsed = _watch["kept"] + (time.time() - _watch["started"] if running else 0)
    if action == "start":
        if running:
            return f"Stopwatch sudah jalan, {_seconds(elapsed)}, {T}."
        _watch.update(started=time.time(), kept=0.0)
        return f"Stopwatch mulai, {T}."
    if not running and not elapsed:
        return f"Stopwatch belum dimulai, {T}."
    if action == "stop":
        _watch.update(started=0.0, kept=elapsed)
        return f"Stopwatch berhenti di {_seconds(elapsed)}, {T}."
    return f"Stopwatch {_seconds(elapsed)}, {T}."


def _seconds(seconds: float) -> str:
    minutes, secs = int(seconds) // 60, int(seconds) % 60
    hours, minutes = minutes // 60, minutes % 60
    return " ".join([f"{hours} jam"] * bool(hours) + [f"{minutes} menit"] * bool(minutes) + [f"{secs} detik"] * bool(secs or not (hours or minutes)))


# ───────────────────────────── notes ─────────────────────────────

def _notes() -> list:
    return sorted(sc._notes_dir().glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)


def _first_line(note) -> str:
    try:
        return note.read_text(encoding="utf-8").strip().split("\n")[0][:60] or "catatan kosong"
    except OSError:
        return note.stem


def list_notes() -> str:
    """What the user has noted ('catatan saya apa saja'): how many, and the latest ones."""
    notes = _notes()
    if not notes:
        return f"Belum ada catatan, {T}."
    return f"Ada {len(notes)} catatan, {T}. Yang terakhir: {'; '.join(_first_line(n) for n in notes[:3])}."


def open_last_note() -> str:
    """Show the latest note in the text editor ('buka catatan')."""
    notes = _notes()
    if not notes:
        return f"Belum ada catatan, {T}."
    sc._open_note(notes[0])
    return f"Catatan terakhir saya buka, {T}."


def _remove_note(path: str) -> str:
    os.remove(path)
    return f"Catatan itu saya hapus, {T}."


def delete_last_note() -> str:
    """Delete the latest note, after asking ('hapus catatan terakhir')."""
    notes = _notes()
    if not notes:
        return f"Belum ada catatan, {T}."
    return safety.guard(f"menghapus catatan: {_first_line(notes[0])}", _remove_note, path=str(notes[0]))


# ───────────────────────────── about time, about Digidaw ─────────────────────────────

def days_until(when: str) -> str:
    """How many days until a date ('berapa hari lagi tanggal 10 Desember')."""
    import agenda
    date, _ = agenda.parse_date(when.lower())
    if not date:
        return f"Tanggal {when} tidak saya kenali, {T}."
    days = (date - datetime.date.today()).days
    if days == 0:
        return f"Itu hari ini, {T}."
    return f"{abs(days)} hari {'lagi' if days > 0 else 'yang lalu'}, {T}."


HELP = ("Banyak, {t}. Aplikasi, jendela, dan workspace; musik dan volume; lampu, kipas, wifi, dan bluetooth; timer, alarm, agenda, dan jadwal; "
        "catatan; pesan WhatsApp; cuaca dan berita; rutinitas buatan sendiri; mengerjakan tugas di layar; juga membuat gambar, dokumen, dan project. "
        "Coba saja bilang yang Anda mau.")


def _part_of_day() -> str:
    hour = datetime.datetime.now().hour
    return "pagi" if 4 <= hour < 11 else "siang" if hour < 15 else "sore" if hour < 18 else "malam"


def small_talk(kind: str, said: str = "") -> str:
    """A greeting answered right away, and right for the time of day. kind: hello, part (said: pagi, siang, sore, malam), how, thanks, praise."""
    now = _part_of_day()
    if kind == "part":
        if said == now:
            return random.choice([f"Selamat {now}, {T}.", f"Selamat {now}, {T}. Gimana harinya?"] if now != "malam" else [f"Selamat malam, {T}."])
        return f"Selamat {now}, {T}. Sekarang {'masih' if ['pagi', 'siang', 'sore', 'malam'].index(now) < ['pagi', 'siang', 'sore', 'malam'].index(said) else 'sudah'} {now}."
    if kind == "praise":
        return random.choice([f"Hehe, terima kasih, {T}.", "Terima kasih, senang kalau bisa membantu.", f"Makasih, {T}."])
    if kind == "how":
        return random.choice([f"Baik, {T}. Kamu sendiri gimana?", f"Baik-baik saja, {T}. Harinya gimana?"])
    if kind == "thanks":
        return random.choice([f"Sama-sama, {T}.", "Sama-sama.", "Iya, sama-sama."])
    return random.choice([f"Halo, {T}.", f"Hai, {T}. Gimana harinya?", f"Halo, {T}, aku dengar."])


def what_i_can_do() -> str:
    """What Digidaw can do, in short ('kamu bisa apa', 'bantuan')."""
    return HELP.format(t=T)


def repeat_reply() -> str:
    """Say the last thing again ('tadi kamu bilang apa', 'ulangi')."""
    import brain
    import tts
    said, at = tts.last_spoken()
    turn = brain._dialog[-1] if brain._dialog else None
    if turn and turn.get("at", 0) > at:
        said = turn["reply"]
    return said or f"Belum ada yang saya katakan, {T}."


def last_command() -> str:
    """What the user asked last ('apa perintah terakhir')."""
    import brain
    return f"Perintah terakhir: {brain._dialog[-1]['user']}, {T}." if brain._dialog else f"Belum ada perintah, {T}."


# ───────────────────────────── recording the screen ─────────────────────────────

_recording: dict = {"process": None, "path": ""}


def screen_record(action: str = "start") -> str:
    """Record the screen to the Videos folder. action: 'start' or 'stop'."""
    process = _recording["process"]
    if action.lower().strip() == "stop":
        if not process or process.poll() is not None:
            return f"Tidak ada rekaman yang berjalan, {T}."
        process.send_signal(signal.SIGINT)
        _recording["process"] = None
        return f"Rekaman disimpan di folder Videos, {T}."
    if process and process.poll() is None:
        return f"Layar sudah sedang direkam, {T}."
    if not shutil.which("wf-recorder"):
        import sysadmin
        return sysadmin.offer_install("wf-recorder", "Perekam layar")
    folder = sc._known_folder("videos")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Rekaman_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
    _recording.update(process=subprocess.Popen(["wf-recorder", "-f", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL), path=str(path))
    return f"Layar mulai direkam, {T}. Bilang hentikan rekaman kalau sudah."


def apps_open(names: str) -> str:
    """Whether apps are running ('apakah discord dan telegram masih berjalan'). names: one or several, joined by "dan" or commas."""
    import persona
    import window_manager
    open_now, closed = [], []
    for name in (n.strip() for n in re.split(r",|\bdan\b|\batau\b", names)):
        if name:
            (open_now if window_manager.find_app(name) or sc._matching_processes(name) else closed).append(persona.brand(name))
    if not open_now and not closed:
        return f"Aplikasi yang mana, {T}?"
    parts = ([f"{' dan '.join(open_now)} masih berjalan"] if open_now else []) + ([f"{' dan '.join(closed)} tidak"] if closed and open_now else [f"{' dan '.join(closed)} tidak berjalan"] if closed else [])
    return f"{', '.join(parts)}, {T}."


COUNT_MOST = 300


def count_aloud(start: int = 1, end: int = 10) -> str:
    """Count out loud ('berhitung 1 sampai 50', 'hitung mundur dari 10'). Upward or downward."""
    start, end = int(start), int(end)
    if abs(end - start) >= COUNT_MOST:
        return f"Itu terlalu panjang untuk diucapkan, {T}. Paling banyak {COUNT_MOST} angka."
    step = 1 if end >= start else -1
    return ", ".join(str(n) for n in range(start, end + step, step)) + "."


AI_TOOLS = [apps_open, count_aloud, small_talk, get_volume, get_brightness, brightness_step, dark_mode, disk_space, uptime, heavy_apps, close_heaviest, internet_speed, check_updates,
            system_update, edit_action, stopwatch, list_notes, open_last_note, delete_last_note, days_until, what_i_can_do, repeat_reply,
            last_command, screen_record, clipboard_history, paste_previous, copy_screen_text, port_process, open_ports, service_control,
            explain_last_error]
