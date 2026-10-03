"""
Linux checks and actions for security_scan.py ("analisis keamanan" on Arch Linux / Hyprland).

Read-only checks, same finding format and scoring as the Windows version:
  sistem ......... firewall (ufw / firewalld / nftables), pending pacman updates and the last full upgrade,
                   kernel updated but not rebooted, /etc/hosts hijacking, proxy variables, ClamAV
  autostart ...... XDG autostart, enabled systemd user units, Hyprland exec-once, crontab, shell rc files:
                   programs run from /tmp or Downloads, download-and-run / base64 one-liners, orphaned entries
  proses/jaringan  programs running from risky folders or deleted from disk, crypto miners, ports open to the
                   network (SSH, VNC, RDP, databases ...), remote-access tools
  file ........... disguised files and scripts in Downloads, AUR packages
Actions: remove orphaned autostart entries, close remote-access apps, ClamAV scans (instead of Defender).
"""
import os
import re
import json
import shutil
import datetime
import subprocess
from pathlib import Path

import psutil

import config
import events
import safety

T = config.USER_TITLE
HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
RISKY_DIRS = ["/tmp/", "/dev/shm/", "/var/tmp/", str(HOME / "Downloads") + "/"]
REMOTE_TOOLS = {"teamviewer": "TeamViewer", "anydesk": "AnyDesk", "rustdesk": "RustDesk", "x11vnc": "x11vnc",
                "wayvnc": "wayvnc", "krfb": "KDE Desktop Sharing", "vncserver": "VNC", "xrdp": "xrdp",
                "sunshine": "Sunshine", "ngrok": "ngrok", "remmina": None}      # Remmina is a client: not flagged
MINERS = re.compile(r"xmrig|minerd|cpuminer|ethminer|nbminer|t-rex|lolminer|phoenixminer", re.I)
DOWNLOAD_AND_RUN = re.compile(r"(?:curl|wget)[^|;]*\|\s*(?:ba|z)?sh\b|base64\s+(?:-d|--decode)[^|]*\|\s*(?:ba|z)?sh|"
                              r"\bnc\b.*\s-e\s|/dev/tcp/\d|python[23]?\s+-c\s+.*(?:socket|base64)", re.I)
SENSITIVE_PORTS = {22: "SSH", 23: "Telnet", 21: "FTP", 139: "Samba", 445: "Samba", 3389: "Remote Desktop (RDP)",
                   5900: "VNC", 5901: "VNC", 3306: "MySQL", 5432: "PostgreSQL", 6379: "Redis", 27017: "MongoDB",
                   2375: "Docker API (tanpa TLS)", 9200: "Elasticsearch", 11211: "Memcached"}
POPULAR_DOMAINS = re.compile(r"google|facebook|youtube|instagram|whatsapp|microsoft|apple|github|paypal|"
                             r"bca|bri|bni|mandiri|tokopedia|shopee|archlinux", re.I)
SCRIPT_EXT = {".sh", ".run", ".bin", ".appimage", ".desktop", ".py", ".pl", ".jar", ".deb", ".rpm"}
WINDOWS_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".com", ".pif", ".lnk", ".reg"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".txt", ".mp4", ".mp3", ".zip", ".rar"}


def _add(findings, severity, category, title, detail="", advice=""):
    findings.append({"severity": severity, "category": category, "title": title, "detail": detail, "advice": advice})


def _run(args: list[str], timeout: float = 30) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _risky(path: str) -> bool:
    return any((path or "").startswith(d) for d in RISKY_DIRS)


# ───────────────────────────── checks ─────────────────────────────

def check_system(findings):
    # firewall
    active = [name for name in ("ufw", "firewalld", "nftables")
              if _run(["systemctl", "is-active", name]).strip() == "active"]
    ufw_enabled = "ENABLED=yes" in (Path("/etc/ufw/ufw.conf").read_text(errors="replace")
                                    if Path("/etc/ufw/ufw.conf").exists() else "")
    if active:
        _add(findings, "ok", "Firewall", f"Firewall aktif ({', '.join(active)})")
    elif ufw_enabled:
        _add(findings, "sedang", "Firewall", "ufw disetel aktif tetapi layanannya tidak berjalan",
             "/etc/ufw/ufw.conf berisi ENABLED=yes, namun ufw.service tidak aktif, jadi aturan firewall tidak dimuat.",
             "Jalankan: sudo systemctl enable --now ufw")
    else:
        _add(findings, "sedang", "Firewall", "Tidak ada firewall yang aktif",
             "ufw, firewalld, dan nftables tidak berjalan.",
             "Di jaringan publik (kafe, kampus) firewall mencegah layanan Anda diakses orang lain. "
             "Pasang & aktifkan: sudo pacman -S ufw && sudo systemctl enable --now ufw && sudo ufw enable")

    # updates
    log = Path("/var/log/pacman.log")
    last_upgrade = None
    if log.exists():
        try:
            with open(log, "rb") as f:
                f.seek(max(0, log.stat().st_size - 2_000_000))
                tail = f.read().decode(errors="replace")
            stamps = re.findall(r"\[(\d{4}-\d{2}-\d{2})T[^\]]*\] \[PACMAN\] starting full system upgrade", tail)
            last_upgrade = datetime.date.fromisoformat(stamps[-1]) if stamps else None
        except (OSError, ValueError):
            pass
    if last_upgrade:
        days = (datetime.date.today() - last_upgrade).days
        if days > 30:
            _add(findings, "sedang", "Update", f"Sistem terakhir diperbarui {days} hari lalu",
                 f"Full upgrade terakhir: {last_upgrade:%d-%m-%Y}.",
                 "Arch adalah rolling release; perbarui rutin dengan: sudo pacman -Syu")
        else:
            _add(findings, "ok", "Update", f"Full upgrade terakhir {days} hari lalu")
    if shutil.which("checkupdates"):
        pending = [l.split()[0] for l in _run(["checkupdates"], timeout=90).splitlines() if l.strip()]
        critical = [p for p in pending if re.fullmatch(r"linux(?:-\w+)?|openssl|openssh|glibc|sudo|systemd|"
                                                         r"firefox|chromium|google-chrome|brave.*|curl|gnutls|nss", p)]
        if critical:
            _add(findings, "sedang", "Update", f"{len(pending)} update tertunda, termasuk paket penting",
                 ", ".join(critical[:8]), "Jalankan: sudo pacman -Syu, lalu restart bila kernel ikut diperbarui.")
        elif len(pending) > 50:
            _add(findings, "rendah", "Update", f"{len(pending)} update tertunda", "", "Jalankan: sudo pacman -Syu")

    # kernel installed but not booted yet
    running = os.uname().release
    if Path("/usr/lib/modules").is_dir() and not Path(f"/usr/lib/modules/{running}").exists():
        _add(findings, "rendah", "Update", "Kernel sudah diperbarui tetapi komputer belum direstart",
             f"Kernel berjalan: {running}.", "Restart agar perbaikan keamanan kernel aktif.")

    # hosts file / proxy
    try:
        hosts = Path("/etc/hosts").read_text(errors="replace").splitlines()
    except OSError:
        hosts = []
    hijacked = []
    for line in hosts:
        parts = line.split("#")[0].split()
        if len(parts) >= 2 and parts[0] not in ("127.0.0.1", "::1", "0.0.0.0", "127.0.1.1", "::") \
                and any(POPULAR_DOMAINS.search(name) for name in parts[1:]):
            hijacked.append(" ".join(parts[:3]))
    if hijacked:
        _add(findings, "tinggi", "Jaringan", "File /etc/hosts mengalihkan situs populer ke alamat lain",
             "; ".join(hijacked[:4]), "Ini teknik pembajakan DNS. Periksa dan hapus baris itu dari /etc/hosts.")
    proxy = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy") and v}
    if proxy:
        _add(findings, "info", "Jaringan", "Proxy diatur lewat variabel lingkungan", ", ".join(f"{k}={v}" for k, v in proxy.items()),
             "Pastikan proxy ini memang Anda pasang sendiri.")

    # antivirus (optional on Linux)
    if not shutil.which("clamscan"):
        _add(findings, "info", "Antivirus", "ClamAV belum terpasang (opsional)", "",
             "Untuk memindai file unduhan: sudo pacman -S clamav && sudo freshclam")


def _hypr_vars(files: list[Path]) -> dict:
    variables = {"HOME": str(HOME)}
    for f in files:
        for line in f.read_text(errors="replace").splitlines():
            m = re.match(r"^\s*\$(\w+)\s*=\s*(.+?)\s*(?:#.*)?$", line)
            if m:
                variables[m.group(1)] = m.group(2)
    return variables


def _expand(command: str, variables: dict) -> str:
    for _ in range(3):
        command = re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m.group(1), os.environ.get(m.group(1), m.group(0))),
                         command)
    return command.replace("~/", str(HOME) + "/")


def _program(command: str) -> str:
    words = [w for w in re.split(r"\s+", command.strip()) if w and "=" not in w.split("/")[0]]
    for w in words:
        if w in ("env", "nohup", "exec", "setsid", "uwsm", "app", "--", "sh", "bash", "-c"):
            continue
        return w.strip("'\"")
    return ""


def startup_entries() -> list[dict]:
    """Everything that starts with the session: {name, command, exe, kind, file?, unit?}."""
    entries = []
    autostart = CONFIG / "autostart"
    for f in sorted(autostart.glob("*.desktop")) if autostart.is_dir() else []:
        text = f.read_text(errors="replace")
        if re.search(r"^Hidden\s*=\s*true", text, re.M):
            continue
        m = re.search(r"^Exec\s*=\s*(.+)$", text, re.M)
        command = m.group(1).strip() if m else ""
        entries.append({"name": f.stem, "command": command, "exe": _program(command), "kind": "autostart", "file": str(f)})
    for line in _run(["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--type=service"]).splitlines():
        unit = line.split()[0] if line.split() else ""
        if not unit:
            continue
        unit_file = next((p for p in (CONFIG / "systemd" / "user" / unit,) if p.exists()), None)
        if not unit_file:
            continue                                  # system-provided user units (pipewire, ...) are trusted
        m = re.search(r"^ExecStart\s*=\s*[-@:+!]*(.+)$", unit_file.read_text(errors="replace"), re.M)
        command = m.group(1).strip() if m else ""
        entries.append({"name": unit, "command": command, "exe": _program(command), "kind": "systemd", "unit": unit})
    hypr_files = sorted((CONFIG / "hypr").rglob("*.conf")) if (CONFIG / "hypr").is_dir() else []
    variables = _hypr_vars(hypr_files)
    for f in hypr_files:
        for line in f.read_text(errors="replace").splitlines():
            m = re.match(r"^\s*exec(?:-once)?\s*=\s*(.+?)\s*(?:#.*)?$", line)
            if m:
                command = _expand(m.group(1), variables)
                entries.append({"name": Path(_program(command)).name or command[:30], "command": command,
                                "exe": _program(command), "kind": "hyprland", "source": str(f)})
    if shutil.which("crontab"):
        for line in _run(["crontab", "-l"]).splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                command = " ".join(line.split()[5:]) if not line.startswith("@") else " ".join(line.split()[1:])
                entries.append({"name": "crontab", "command": command, "exe": _program(command), "kind": "cron"})
    return entries


def _exists(exe: str) -> bool:
    if not exe:
        return True
    if "/" in exe:
        return os.path.exists(os.path.expanduser(exe))
    return bool(shutil.which(exe))


def is_orphan(entry: dict) -> bool:
    return entry["kind"] in ("autostart", "systemd") and not _exists(entry["exe"])


def check_autostart(findings):
    entries = startup_entries()
    risky, obfuscated, orphans = [], [], []
    for e in entries:
        if DOWNLOAD_AND_RUN.search(e["command"]):
            obfuscated.append(e)
        elif _risky(os.path.expanduser(e["exe"])):
            risky.append(e)
        elif is_orphan(e):
            orphans.append(e)
    for e in obfuscated:
        _add(findings, "tinggi", "Startup", f"Perintah startup mengunduh/menjalankan kode tersamar: {e['name']}",
             e["command"][:200], "Pola seperti 'curl ... | sh' atau base64 yang dijalankan biasa dipakai malware. "
                                 "Hapus entri itu kalau Anda tidak memasangnya sendiri.")
    for e in risky:
        _add(findings, "tinggi", "Startup", f"Program startup berjalan dari folder rawan: {e['name']}",
             e["exe"], "Program yang otomatis jalan dari /tmp atau Downloads patut dicurigai.")
    if orphans:
        _add(findings, "rendah", "Startup", f"{len(orphans)} entri startup menunjuk ke program yang sudah tidak ada",
             ", ".join(e["name"] for e in orphans[:6]), "Ucapkan 'bersihkan startup yatim' untuk menghapusnya.")
    if not (obfuscated or risky):
        _add(findings, "ok", "Startup", f"{len(entries)} program startup diperiksa, tidak ada yang mencurigakan")
    rc_hits = []
    for rc in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile", ".config/fish/config.fish"):
        p = HOME / rc
        if p.is_file():
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if DOWNLOAD_AND_RUN.search(line) and not line.lstrip().startswith("#"):
                    rc_hits.append(f"~/{rc}:{n}: {line.strip()[:120]}")
    if rc_hits:
        _add(findings, "tinggi", "Startup", "File konfigurasi shell menjalankan kode dari internet",
             "; ".join(rc_hits[:3]), "Periksa baris tersebut; hapus kalau bukan Anda yang menambahkan.")


def check_processes_and_network(findings):
    risky, deleted, miners, remote = [], [], [], set()
    me = os.getuid()
    for p in psutil.process_iter(["pid", "name", "exe", "uids"]):
        name = (p.info["name"] or "").lower()
        exe = p.info["exe"] or ""
        if MINERS.search(name) or MINERS.search(exe):
            miners.append(f"{p.info['name']} (PID {p.info['pid']})")
        for key, label in REMOTE_TOOLS.items():
            if label and key in name:
                remote.add(label)
        if not exe or (p.info["uids"] and p.info["uids"].real != me):
            continue
        if _risky(exe):
            risky.append(f"{p.info['name']} ({exe})")
        elif exe.endswith(" (deleted)") and not re.search(r"/usr/|/opt/|\.cache/|electron|chrome|firefox", exe):
            deleted.append(f"{p.info['name']} ({exe})")
    if miners:
        _add(findings, "tinggi", "Proses", "Program penambang kripto sedang berjalan", ", ".join(miners[:4]),
             "Kalau bukan Anda yang menjalankannya, ini malware. Hentikan prosesnya dan periksa program startup.")
    if risky:
        _add(findings, "sedang", "Proses", f"{len(risky)} program berjalan dari folder rawan (/tmp, Downloads)",
             "; ".join(risky[:5]), "Pastikan Anda mengenali program-program ini.")
    if deleted:
        _add(findings, "rendah", "Proses", "Program berjalan padahal filenya sudah dihapus", "; ".join(deleted[:4]),
             "Biasanya karena aplikasi diperbarui saat sedang berjalan; restart aplikasinya. Kalau tidak dikenal, waspadai.")
    if remote:
        _add(findings, "sedang", "Remote access", f"Aplikasi remote access berjalan: {', '.join(sorted(remote))}", "",
             "Selama berjalan, orang lain bisa mengendalikan komputer ini bila tahu ID/kata sandinya. "
             "Ucapkan 'tutup aplikasi remote' kalau sedang tidak dipakai.")
    open_ports: dict[int, str] = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status != psutil.CONN_LISTEN or not c.laddr:
                continue
            if c.laddr.ip.startswith("127.") or c.laddr.ip in ("::1",):
                continue
            open_ports.setdefault(c.laddr.port, SENSITIVE_PORTS.get(c.laddr.port, ""))
    except psutil.Error:
        pass
    sensitive = {p: label for p, label in open_ports.items() if label}
    if sensitive:
        _add(findings, "sedang", "Jaringan", "Layanan sensitif menerima koneksi dari jaringan",
             ", ".join(f"{label} (port {p})" for p, label in sorted(sensitive.items())),
             "Matikan layanan yang tidak dipakai (sudo systemctl disable --now <layanan>) atau batasi dengan firewall.")
    elif open_ports:
        _add(findings, "info", "Jaringan", f"{len(open_ports)} port terbuka ke jaringan",
             ", ".join(str(p) for p in sorted(open_ports)[:12]), "Umumnya dari aplikasi seperti Spotify/KDE Connect/dev server.")
    else:
        _add(findings, "ok", "Jaringan", "Tidak ada port yang terbuka ke jaringan")


def check_files(findings):
    downloads = HOME / "Downloads"
    disguised, scripts, windows_programs = [], [], []
    if downloads.is_dir():
        for f in list(downloads.glob("*")) + list(downloads.glob("*/*")):
            if not f.is_file():
                continue
            suffixes = [s.lower() for s in f.suffixes[-2:]]
            ext = f.suffix.lower()
            if len(suffixes) == 2 and suffixes[0] in DOC_EXT and (ext in SCRIPT_EXT or ext in WINDOWS_EXT):
                disguised.append(f.name)
            elif ext in SCRIPT_EXT or (os.access(f, os.X_OK) and ext not in DOC_EXT):
                scripts.append(f.name)
            elif ext in WINDOWS_EXT:
                windows_programs.append(f.name)
    if disguised:
        _add(findings, "tinggi", "File", "File menyamar sebagai dokumen di Downloads", ", ".join(disguised[:5]),
             "Jangan dibuka. Hapus kalau tidak dikenal (ucapkan 'hapus file itu').")
    if scripts:
        _add(findings, "rendah", "File", f"{len(scripts)} skrip/program di Downloads", ", ".join(scripts[:6]),
             "Jalankan hanya yang berasal dari sumber resmi dan Anda pahami isinya.")
    if windows_programs:
        _add(findings, "info", "File", f"{len(windows_programs)} program Windows di Downloads",
             ", ".join(windows_programs[:6]), "Tidak bisa berjalan langsung di Linux, tapi bisa lewat Wine; scan dulu bila ragu.")
    if not (disguised or scripts):
        _add(findings, "ok", "File", "Tidak ada file berbahaya di Downloads")
    foreign = [l.split()[0] for l in _run(["pacman", "-Qm"]).splitlines() if l.strip()]
    if foreign:
        _add(findings, "info", "Software", f"{len(foreign)} paket dari AUR / luar repo resmi",
             ", ".join(foreign[:10]) + ("…" if len(foreign) > 10 else ""),
             "Paket AUR dibuat komunitas: selalu baca PKGBUILD sebelum memasang atau memperbarui.")


CHECKS = [("sistem & update", check_system), ("program startup", check_autostart),
          ("proses & jaringan", check_processes_and_network), ("file & software", check_files)]


# ───────────────────────────── actions ─────────────────────────────

def _announce(message: str):
    import security_scan
    events.emit("security", status="scan-done", step=message)
    if security_scan.ANNOUNCE:
        security_scan.ANNOUNCE(message, "info")
    else:
        print(message)


def clean_orphan_startup() -> str:
    orphans = [e for e in startup_entries() if is_orphan(e)]
    if not orphans:
        return f"Tidak ada entri startup yatim, {T}. Semuanya menunjuk ke program yang masih ada."
    names = ", ".join(e["name"] for e in orphans[:5]) + (f" dan {len(orphans) - 5} lainnya" if len(orphans) > 5 else "")
    return safety.guard(f"menghapus {len(orphans)} entri startup yatim ({names})", _do_clean_orphans, orphans=orphans)


def _do_clean_orphans(orphans: list[dict]) -> str:
    import security_scan
    backup = security_scan._reports_dir() / f"cadangan_startup_{datetime.datetime.now():%Y-%m-%d_%H%M}.json"
    for e in orphans:
        if e.get("file") and os.path.exists(e["file"]):
            e["content"] = Path(e["file"]).read_text(errors="replace")
    backup.write_text(json.dumps(orphans, ensure_ascii=False, indent=1), encoding="utf-8")
    done, failed = [], []
    for e in orphans:
        try:
            if e["kind"] == "autostart":
                Path(e["file"]).unlink()
            else:
                subprocess.run(["systemctl", "--user", "disable", "--now", e["unit"]], capture_output=True, timeout=20)
            done.append(e["name"])
        except (OSError, subprocess.SubprocessError):
            failed.append(e["name"])
    text = f"{len(done)} entri startup yatim sudah dihapus, {T}. Cadangannya ada di {backup.name}."
    if failed:
        text += f" {len(failed)} tidak bisa dihapus: {', '.join(failed[:3])}."
    return text


def close_remote_access_apps() -> str:
    procs = [p for p in psutil.process_iter(["name", "pid"])
             if any(label and k in (p.info["name"] or "").lower() for k, label in REMOTE_TOOLS.items())]
    services = [s for s in ("teamviewerd", "anydesk", "rustdesk", "xrdp", "x11vnc", "sunshine")
                if _run(["systemctl", "is-active", s]).strip() == "active"]
    if not procs and not services:
        return f"Tidak ada aplikasi remote access yang berjalan, {T}."
    labels = sorted({label for p in procs for k, label in REMOTE_TOOLS.items() if label and k in p.info["name"].lower()}
                    | set(services))
    extra = " dan layanan latarnya (butuh password administrator)" if services else ""
    return safety.guard(f"menutup {', '.join(labels)}{extra}", _do_close_remote,
                        pids=[p.pid for p in procs], services=services)


def _do_close_remote(pids: list[int], services: list[str]) -> str:
    denied = []
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied.append(pid)
    if services or denied:
        import system_control
        script = "; ".join([f"systemctl stop {s}" for s in services] + ([f"kill -9 {' '.join(map(str, denied))}"] if denied else []))
        ok, _ = system_control.run_elevated(script)
        if not ok:
            return (f"Aplikasinya sudah saya tutup, tetapi layanan latarnya masih jalan karena izin administrator "
                    f"tidak diberikan, {T}.")
        return (f"Aplikasi remote access dan layanannya sudah dihentikan, {T}. Layanan aktif lagi setelah restart "
                f"bila masih di-enable; matikan permanen dengan systemctl disable.")
    return f"Aplikasi remote access sudah ditutup, {T}."


def _clamscan(targets: list[str], label: str, timeout: int):
    events.emit("security", status="running", step=f"{label} berjalan…")
    exe = shutil.which("clamdscan") if _run(["systemctl", "is-active", "clamav-daemon"]).strip() == "active" else None
    exe = exe or shutil.which("clamscan")
    try:
        r = subprocess.run([exe, "-r", "--infected", "--no-summary", *targets], capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        found = re.findall(r"^(.+?): (.+) FOUND$", r.stdout, re.M)
        if r.returncode == 0:
            message = f"{label} selesai, {T}. Tidak ada ancaman ditemukan."
        elif found:
            message = (f"{label} selesai, {T}. ClamAV menemukan {len(found)} ancaman: "
                       + ", ".join(f"{Path(f).name} ({sig})" for f, sig in found[:3]) + ". Sebaiknya file itu dihapus.")
        else:
            message = f"{label} berhenti dengan kode {r.returncode}, {T}. {(r.stderr or '').strip()[:120]}"
    except subprocess.TimeoutExpired:
        message = f"{label} melebihi batas waktu, {T}."
    _announce(message)


def _clam_missing() -> str | None:
    if shutil.which("clamscan"):
        return None
    return (f"ClamAV belum terpasang, {T}. Pasang dulu dengan perintah: sudo pacman -S clamav, "
            f"lalu sudo freshclam untuk mengunduh database virusnya.")


def defender_scan(kind: str = "quick") -> str:
    missing = _clam_missing()
    if missing:
        return missing
    import threading
    full = kind.lower().startswith(("full", "penuh", "lengkap", "menyeluruh"))
    label = "Full scan ClamAV" if full else "Quick scan ClamAV"
    targets = [str(HOME)] if full else [str(p) for p in (HOME / "Downloads", HOME / "Desktop", Path("/tmp")) if p.exists()]
    threading.Thread(target=_clamscan, args=(targets, label, 6 * 3600), daemon=True, name="clamav-scan").start()
    return (f"{label} saya jalankan di latar belakang, {T}. "
            + ("Ini bisa lebih dari satu jam." if full else "Memindai Downloads, Desktop, dan /tmp.") + " Saya kabari hasilnya.")


def scan_file(path: str) -> str:
    missing = _clam_missing()
    if missing:
        return missing
    import threading
    threading.Thread(target=_clamscan, args=([path], f"Scan file {Path(path).name}", 900), daemon=True,
                     name="clamav-file").start()
    return f"Memindai {Path(path).name} dengan ClamAV, {T}."


# ───────────────────────────── downloads guard ─────────────────────────────

def download_origin(path: Path) -> str:
    """Browsers store the source URL in an extended attribute on Linux."""
    for attr in ("user.xdg.origin.url", "user.xdg.referrer.url"):
        try:
            url = os.getxattr(str(path), attr).decode(errors="replace")
        except (OSError, AttributeError):
            continue
        if url:
            return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return ""


def assess_download(path: Path) -> tuple[str, str] | None:
    ext = path.suffix.lower()
    suffixes = [x.lower() for x in path.suffixes[-2:]]
    origin = download_origin(path)
    src = f" dari {origin}" if origin else ""
    if len(suffixes) == 2 and suffixes[0] in DOC_EXT and (ext in SCRIPT_EXT or ext in WINDOWS_EXT):
        return "tinggi", (f"Awas, {T}: file baru {path.name}{src} menyamar sebagai dokumen padahal program. "
                          f"Jangan dibuka. Ucapkan 'hapus file itu' atau 'scan file itu'.")
    if ext in (".appimage", ".run", ".bin"):
        return "sedang", (f"{T}, ada program baru di Downloads: {path.name}{src}. Jalankan hanya kalau dari situs "
                          f"resmi; ucapkan 'scan file itu' untuk memeriksanya.")
    if ext in (".sh", ".desktop", ".py", ".pl", ".jar"):
        return "sedang", (f"Ada skrip baru di Downloads: {path.name}{src}. Skrip bisa menjalankan perintah apa pun; "
                          f"baca isinya dulu sebelum dijalankan, {T}.")
    if ext in (".deb", ".rpm"):
        return "info", f"Paket {ext[1:]} baru: {path.name}{src}. Di Arch lebih aman memakai paket dari repo atau AUR."
    if ext in (".iso", ".img"):
        return "info", f"File image disk baru: {path.name}{src}."
    if ext in WINDOWS_EXT:
        return "info", f"Program Windows baru di Downloads: {path.name}{src}."
    return None
