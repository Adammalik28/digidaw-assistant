"""
Commands that need root, and updating Arch.

The password is typed in the system's own window (polkit, pkexec): Digidaw never sees it, stores nothing and has no standing right to be root. Everything here
first says exactly what will run and waits for "ya", whatever config.CONFIRM_DANGEROUS_ACTIONS says, also when an AI chose the command.

The update runs as a system unit (systemd-run) writing to its own log, not as a child of Digidaw: restarting Digidaw or losing the desktop session must not
cut pacman off halfway, which can leave the system unable to boot. AUR packages stay in a terminal: building them runs other people's scripts, which the user should get to see.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil

import config
import events
import linux_desktop as ld
import oscompat
import safety

T = config.USER_TITLE
ANNOUNCE = None                  # set by main (core.announce)

MARK = "__DIGIDAW_KELUAR="
ROOT_SECONDS = 600               # a command, the time for the password included
AUTH_SECONDS = 180               # the update: nobody typed the password in this long
SAID_MOST = 160                  # of a command's output, out loud
UNIT = "digidaw-update"
UPDATE_LOG = Path("/var/log/digidaw-update.log")      # (written as root, so not somewhere the user's programs can point elsewhere; only package names in it)
PACMAN_LOG = Path("/var/log/pacman.log")
START_MARK = "===== "
POLL_SECONDS = 2
GONE_CHECKS = 3
PUBLISH_EVERY = 10
LOW_BATTERY = 30
UPDATE_SCRIPT = "pacman -Sy --needed --noconfirm archlinux-keyring && pacman -Su --noconfirm"     # (after weeks without updating, an old keyring refuses the new signatures)
KERNELS = re.compile(r"^(?:linux|linux-zen|linux-lts|linux-hardened|nvidia.*|systemd|glibc)$")
SUDO_SAID_RE = re.compile(r"(?:tolong |coba )?(?:jalankan |run |eksekusi )?(?:perintah )?(?:sudo|su do|pseudo|suto)\s+(.+)", re.IGNORECASE)
# whole disks and the root folder: not by voice, whatever the answer to "yakin?" (a misheard word is enough)
NEVER_RE = re.compile(r"\brm\s+(?:-\S+\s+)*(?:/|/\*|/(?:etc|usr|boot|home|var|bin|lib)/?)(?:\s|$)|\bmkfs\b|\bwipefs\b|\bdd\b.*\bof=/dev/|>\s*/dev/(?:sd|nvme|vd)|"
                      r"\bchmod\s+-R\s+\S+\s+/(?:\s|$)|\bchown\s+-R\s+\S+\s+/(?:\s|$)|:\(\)\s*\{|\b(?:fdisk|parted|sgdisk|cfdisk)\b", re.IGNORECASE)
NOT_ASKED_RE = re.compile(r"^Error executing command as another user|^polkit|^pkexec|^Request dismissed|^Not authorized", re.IGNORECASE)
PACKAGES_RE = re.compile(r"Packages? \((\d+)\)")
DOWNLOAD_RE = re.compile(r"Total Download Size:\s+([\d.]+) MiB")
PROGRESS_RE = re.compile(r"(?:upgrading|installing|reinstalling|downgrading) \S+\.\.\.\s*$")     # (without a terminal pacman prints no counter: seen in the real log)


def _say(text: str, level: str = "reminder"):
    print(f"[admin] {text}")
    if ANNOUNCE:
        ANNOUNCE(text, level)


# ───────────────────────────── one command as root ─────────────────────────────

def _without_sudo(command: str) -> str:
    said = SUDO_SAID_RE.fullmatch(command.strip())
    return (said[1] if said else command).strip().rstrip(".").strip()


def _unattended(command: str) -> str:
    """Nobody can answer pacman's own "proceed?" here: the "ya" was already given."""
    if re.match(r"pacman\s+-(?:S|R|U)", command) and "--noconfirm" not in command:
        return f"{command} --noconfirm"
    return command


def run_as_root(command: str) -> str:
    """Run a command that needs sudo / root ('sudo systemctl restart bluetooth', 'pasang paket htop' = pacman -S htop). command: without the word sudo.
    Digidaw says the command and waits for "ya", then the user types the password in the system's window on the laptop. The result is said when it is done."""
    command = _without_sudo(command)
    if not command or command.lower() in ("sudo", "su do", "pseudo", "suto"):
        return f"Perintahnya apa, {T}?"
    if NEVER_RE.search(command):
        return f"Perintah itu bisa menghapus isi disk, {T}. Tidak saya jalankan lewat suara; kalau memang perlu, pakai terminal."
    if oscompat.IS_WINDOWS:
        import system_control
        return safety.guard(f"menjalankan sebagai administrator: {command}", system_control._run_command_elevated, always=True, command=command)
    if not shutil.which("pkexec"):
        return f"pkexec tidak terpasang, {T}, jadi jendela kata sandinya tidak bisa muncul."
    return safety.guard(f"menjalankan sebagai root: {command}", _start_root, always=True, asking=f"Jalankan sebagai root: {command}. Yakin, {T}?", command=command)


def _start_root(command: str) -> str:
    threading.Thread(target=_root_job, args=(_unattended(command),), daemon=True, name="root-command").start()
    return f"Silakan isi kata sandi di jendela yang muncul di laptop, {T}. Hasilnya saya kabari."


def _root_job(command: str):
    print(f"[admin] root: {command}")
    proc = subprocess.Popen(["pkexec", "bash", "-c", f"{command}\necho {MARK}$?"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, errors="replace")
    try:
        out, _ = proc.communicate(timeout=ROOT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass                       # (root by now: it isn't ours to stop)
        _say(f"Perintah root belum selesai setelah {ROOT_SECONDS // 60} menit, {T}.", "warning")
        return
    found = re.search(rf"{MARK}(\d+)\s*$", out or "")
    if not found and proc.returncode in (126, 127):       # (only without the mark: 127 from inside is "command not found")
        _say(f"Perintah tidak dijalankan karena kata sandinya tidak diisi, {T}.", "warning")
        return
    code = int(found[1]) if found else proc.returncode
    text = (out[:found.start()] if found else out or "").strip()
    if text:
        print(f"[admin] keluaran: {text[:1500]}")
    if code == 0:
        short = f" Hasilnya: {text}" if text and len(text) <= SAID_MOST and "\n" not in text else ""
        _say(f"Perintah root selesai, {T}.{short}")
    else:
        last = text.splitlines()[-1][:SAID_MOST] if text else ""
        _say(f"Perintah root gagal, kode {code}, {T}.{f' {last}' if last else ''}", "warning")


def offer_install(package: str, what: str, then: str = "") -> str:
    """A skill needs a program that isn't here: say so and offer to install it, the usual "ya" and password. then: a command to run after it."""
    if not shutil.which("pkexec") or not shutil.which("pacman"):
        return f"{what} belum terpasang, {T}. Paketnya: {package}."
    command = f"pacman -S --needed --noconfirm {package}" + (f" && {then}" if then else "")
    return safety.guard(f"memasang {what}", _start_root, always=True, asking=f"{what} belum terpasang. Pasang sekarang, {T}?", command=command)


# ───────────────────────────── the update ─────────────────────────────

_update = {"running": False, "done": 0, "total": 0, "phase": "", "started": 0.0, "last": "", "size": 0.0}


def _publish():
    """For the HUDs: a badge on the PC, a pill on the phone."""
    now = {"running": _update["running"], "done": _update["done"], "total": _update["total"], "phase": _update["phase"]}
    if events.state.get("update") != now:
        events.state["update"] = now
        events.emit("update", **now)


def _unit_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, text=True, timeout=3).stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        return False


def updating() -> bool:
    """Also after a restart of Digidaw: the unit goes on without it."""
    return _update["running"] or _unit_active()


def _waiting() -> tuple[list[str] | None, int]:
    """(repository packages waiting, or None when it can't be told; AUR packages waiting)."""
    if not shutil.which("checkupdates"):
        return None, 0
    done = ld.run(["checkupdates"], timeout=60)
    if not done or done.returncode not in (0, 2):         # (2 = nothing to update)
        return None, 0
    names = [line.split()[0] for line in (done.stdout or "").splitlines() if line.strip()]
    aur = ld.run(["yay", "-Qua"], timeout=30) if shutil.which("yay") else None
    return names, len([x for x in (aur.stdout or "").splitlines() if x.strip()]) if aur else 0


def system_update() -> str:
    """Update Arch ('update sistem', 'perbarui sistem'): says how many packages wait and asks; after "ya" and the password it runs in the background and reports when done."""
    if updating():
        return update_status()
    names, aur = _waiting()
    if names is None:
        return f"Daftar pembaruan tidak bisa diambil, {T}. Periksa internetnya."
    if not names:
        return f"Sistem sudah terbaru, {T}." + (f" Ada {aur} paket AUR; ucapkan update AUR kalau mau dipasang." if aur else "")
    battery = psutil.sensors_battery()
    if battery and not battery.power_plugged and battery.percent < LOW_BATTERY:
        return f"Baterai tinggal {battery.percent:.0f} persen, {T}. Update yang terputus bisa merusak sistem; sambungkan charger dulu."
    heavy = ", termasuk bagian inti sistem, jadi nanti perlu restart" if any(KERNELS.fullmatch(n) for n in names) else ""
    return safety.guard(f"memasang {len(names)} pembaruan paket", _start_update, always=True, asking=f"Ada {len(names)} pembaruan paket{heavy}. Pasang sekarang, {T}?", total=len(names), aur=aur)


def _start_update(total: int, aur: int = 0) -> str:
    _update.update(running=True, done=0, total=total, phase="menunggu kata sandi", started=time.time(), size=0.0)
    _publish()
    threading.Thread(target=_update_job, args=(aur,), daemon=True, name="system-update").start()
    return f"Silakan isi kata sandi di jendela yang muncul di laptop, {T}. Setelah itu update berjalan di latar belakang, dan saya kabari kalau selesai."


def _note_line(line: str):
    m = PROGRESS_RE.match(line)
    size = DOWNLOAD_RE.search(line)
    many = PACKAGES_RE.match(line)
    if m:
        _update.update(done=_update["done"] + 1, phase="memasang")
    elif many:
        _update["total"] = int(many[1])
    elif size:
        _update["size"] = float(size[1])
    elif "Retrieving packages" in line or "downloading" in line:
        _update["phase"] = "mengunduh"
    elif line.startswith(":: Running post-transaction hooks"):
        _update["phase"] = "menyelesaikan"
    if not m or _update["done"] % PUBLISH_EVERY == 0:
        _publish()


def _give_up(proc):
    try:
        proc.kill()
    except OSError:
        pass


def _log_size() -> int:
    try:
        return UPDATE_LOG.stat().st_size
    except OSError:
        return 0


def _update_job(aur: int):
    """The unit writes to its own log and needs nobody reading it: Digidaw can restart or crash meanwhile, and picks the reading up again (start)."""
    script = f'exec >>{UPDATE_LOG} 2>&1; echo "{START_MARK}$(date "+%F %T") ====="; {UPDATE_SCRIPT}; echo {MARK}$?'
    argv = ["pkexec", "systemd-run", "--quiet", "--collect", f"--unit={UNIT}", "--setenv=LC_ALL=C",
            "systemd-inhibit", "--what=sleep:shutdown:idle", "--who=Digidaw", "--why=Update sistem", "bash", "-c", script]
    begin = _log_size()
    code, out = 126, ""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace")
        try:
            out, _ = proc.communicate(timeout=AUTH_SECONDS)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _give_up(proc)
    except OSError as e:
        code, out = 1, str(e)
    if code in (126, 127):
        _update.update(running=False, phase="", last="")
        _publish()
        _say(f"Update tidak dijalankan karena kata sandinya tidak diisi, {T}.", "warning")
        return
    if code != 0:
        _update.update(running=False, phase="")
        _publish()
        _say(f"Update tidak bisa dimulai, {T}: {(out or '').strip()[:SAID_MOST]}", "warning")
        return
    _update["phase"] = "memeriksa"
    _publish()
    print("[admin] update sistem berjalan")
    _follow(begin, aur)


def _follow(position: int, aur: int):
    """Read what the unit writes until it says how it ended."""
    code, errors, pacnew, gone, rest = None, [], 0, 0, b""
    while code is None:
        try:
            with open(UPDATE_LOG, "rb") as log:
                log.seek(position)
                chunk = log.read()
                position = log.tell()
        except OSError:
            chunk = b""
        lines = (rest + chunk).split(b"\n")
        rest = lines.pop()
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            _note_line(line)
            if line.lower().startswith("error:"):
                errors.append(line[6:].strip())
            pacnew += ".pacnew" in line
            if line.startswith(MARK):
                code = int(line[len(MARK):].strip() or 1)
        if code is None:
            gone = gone + 1 if not _unit_active() else 0
            if gone >= GONE_CHECKS:
                break                      # (stopped from outside, or the machine went through something: no end was written)
            time.sleep(POLL_SECONDS)
    if code is None and _pacman_finished():
        code = 0                           # (no end in this log, but pacman's own says it finished: an update begun before Digidaw kept its own log)
    _update["running"] = False
    _publish()
    minutes = max(1, round((time.time() - _update["started"]) / 60))
    if code == 0:
        text = f"Update sistem selesai, {_update['done']} paket dalam {minutes} menit, {T}." if _update["done"] else f"Update sistem selesai dalam {minutes} menit, {T}."
        if needs_restart():
            text += " Kernel baru sudah terpasang; restart laptop kalau sudah sempat."
        if pacnew:
            text += f" Ada {pacnew} berkas pengaturan baru berakhiran pacnew yang perlu ditinjau."
        if aur:
            text += f" Masih ada {aur} paket AUR; ucapkan update AUR untuk memasangnya di terminal."
        _say(text)
    else:
        why = errors[-1][:SAID_MOST] if errors else (f"kode {code}" if code is not None else "berhenti tanpa laporan")
        text = f"Update sistem gagal, {T}: {why}. Ucapkan update lewat terminal untuk menyelesaikannya. Rinciannya di {UPDATE_LOG}."
        _say(text, "warning")
    _update["last"] = text


def _pacman_finished() -> bool:
    """Did the last full upgrade in pacman's own log reach its end?"""
    try:
        text = PACMAN_LOG.read_text(encoding="utf-8", errors="replace")[-400_000:]
    except OSError:
        return False
    begun = text.rfind("starting full system upgrade")
    return begun >= 0 and "transaction completed" in text[begun:]


def start():
    """Digidaw came back while an update is running: keep reading where this update began."""
    if not _unit_active():
        return
    try:
        whole = UPDATE_LOG.read_bytes()
        position = max(0, whole.rfind(START_MARK.encode()))
    except OSError:
        position = 0
    _update.update(running=True, done=0, total=0, phase="memeriksa", started=time.time())
    _publish()
    print("[admin] update sistem masih berjalan, dipantau lagi")
    threading.Thread(target=_follow, args=(position, 0), daemon=True, name="system-update").start()


def needs_restart() -> bool:
    """The running kernel's modules are gone once a new kernel is installed."""
    return not os.path.isdir(f"/usr/lib/modules/{os.uname().release}")


def update_status() -> str:
    """How far the system update is ('status update', 'update sudah sampai mana')."""
    if not updating():
        return _update["last"] or f"Tidak ada update yang sedang berjalan, {T}."
    if _update["phase"] == "memasang" and _update["total"]:
        return f"Update sedang memasang, {_update['done']} dari {_update['total']} paket, {T}."
    if _update["phase"] == "mengunduh" and _update.get("size"):
        return f"Update sedang mengunduh sekitar {_update['size'] / 1024:.1f} GB, {T}.".replace(".", ",", 1)
    return f"Update sedang berjalan, tahap {_update['phase'] or 'memasang'}, {T}."


def _terminal(command: list[str], title: str) -> str:
    if updating():
        return update_status()
    if not shutil.which("kitty") or not ld.spawn(["kitty", "--title", title, "--hold", *command]):
        return f"Terminal untuk update tidak bisa dibuka, {T}."
    return f"Terminal {title.lower()} saya buka, {T}. Kata sandi dan persetujuannya di sana."


def update_in_terminal() -> str:
    """Update everything in a terminal the user watches and answers in, AUR included ('update lewat terminal')."""
    tool = next((t for t in ("yay", "paru") if shutil.which(t)), "")
    return _terminal([tool, "-Syu"] if tool else ["sudo", "pacman", "-Syu"], "Update sistem")


def update_aur() -> str:
    """Update the AUR packages in a terminal, where the user can read what gets built ('update AUR')."""
    tool = next((t for t in ("yay", "paru") if shutil.which(t)), "")
    if not tool:
        return f"yay atau paru belum terpasang, {T}."
    return _terminal([tool, "-Sua"], "Update AUR")


AI_TOOLS = [run_as_root, update_status, update_in_terminal, update_aur]
