"""
Windows health diagnosis from the Event Viewer ("diagnosa sistem", "kenapa PC saya lambat / sering crash").

Reads up to 200 error/critical events of the last N hours, drops well-known harmless noise, groups the rest
(same source + id = one problem, with a count) and recognises the important kinds: app crashes, unexpected
shutdowns / blue screens, disk & file-system errors, failing drivers / services, hardware (WHEA) errors and
Windows Update failures. The AI then explains the top problems in plain Indonesian; without the AI, a plain
summary is still given.

On Linux the same is read from the systemd journal (priority emerg..err): crashes come from systemd-coredump,
kernel messages are sorted into disk / hardware / network / driver problems, failed units come from systemd.
"""
import json
import re
import os
import hashlib
import datetime
import subprocess
from collections import Counter, defaultdict

import config
import llm
import oscompat

T = config.USER_TITLE
IS_WINDOWS = oscompat.IS_WINDOWS
SOURCE = "Event Viewer" if IS_WINDOWS else "journal sistem (journalctl -p 3 -b)"
SYSTEM_NAME = "Windows" if IS_WINDOWS else "Linux"

# frequent, harmless errors that only confuse a diagnosis
NOISE = {("Microsoft-Windows-DistributedCOM", 10016), ("Microsoft-Windows-DistributedCOM", 10010),
         ("Microsoft-Windows-DistributedCOM", 10005), ("ESENT", 455), ("Microsoft-Windows-Perflib", 1008),
         ("Microsoft-Windows-WMI", 10), ("Microsoft-Windows-Security-SPP", 8198), ("SecurityCenter", 17),
         ("Microsoft-Windows-Time-Service", 129), ("VSS", 8193), ("Microsoft-Windows-AppModel-Runtime", 69)}

CATEGORIES = [
    ("Aplikasi crash", lambda p, i: (p, i) in {("Application Error", 1000), ("Application Hang", 1002),
                                               (".NET Runtime", 1026), ("Windows Error Reporting", 1001)}),
    ("Mati mendadak / blue screen", lambda p, i: (p, i) in {("Microsoft-Windows-Kernel-Power", 41), ("BugCheck", 1001),
                                                            ("EventLog", 6008), ("Microsoft-Windows-WER-SystemErrorReporting", 1001)}),
    ("Disk / sistem file", lambda p, i: p in ("disk", "Ntfs", "Microsoft-Windows-Ntfs", "volmgr", "stornvme", "storahci")),
    ("Perangkat keras (WHEA)", lambda p, i: "WHEA" in p),
    ("Driver / layanan gagal", lambda p, i: p == "Service Control Manager" or "Kernel-PnP" in p or p.endswith("Driver")),
    ("Windows Update", lambda p, i: "WindowsUpdate" in p or p == "Microsoft-Windows-WindowsUpdateClient"),
    ("Jaringan", lambda p, i: any(k in p for k in ("DNS", "Dhcp", "NetBT", "Tcpip", "WLAN", "NCSI"))),
]

if not IS_WINDOWS:
    CATEGORIES = [
        ("Aplikasi crash", lambda p, i: p == "systemd-coredump"),
        ("Disk / sistem file", lambda p, i: p in ("kernel (disk)", "smartd", "udisksd", "ntfs-3g", "fsck")),
        ("Perangkat keras", lambda p, i: p == "kernel (perangkat keras)"),
        ("Driver / layanan gagal", lambda p, i: p in ("systemd", "kernel (driver)") or p.endswith(".service")),
        ("Jaringan", lambda p, i: p in ("kernel (jaringan)", "NetworkManager", "wpa_supplicant", "iwd",
                                        "systemd-resolved", "systemd-networkd", "dhcpcd", "bluetoothd")),
    ]

# kernel lines are split by what they are about; harmless chatter is dropped
_KERNEL_KINDS = [
    ("kernel (disk)", re.compile(r"I/O error|EXT4-fs (?:error|warning)|BTRFS (?:error|critical)|XFS .*error|nvme.*"
                                 r"(?:timeout|error|reset)|ata\d.*(?:error|failed|exception)|blk_update_request|"
                                 r"Buffer I/O|ntfs", re.I)),
    ("kernel (perangkat keras)", re.compile(r"Hardware Error|mce:|Machine check|PCIe Bus Error|AER:|thermal|"
                                            r"temperature above threshold|watchdog", re.I)),
    ("kernel (jaringan)", re.compile(r"iwlwifi|wlan\d|wlp|ath1\dk|rtw|r8169|e1000|bluetooth", re.I)),
]
_LINUX_NOISE = re.compile(r"ACPI BIOS Error|ACPI Error: (?:Aborting|AE_)|Unhandled alg|sudo: .*password is required|"
                          r"a password is required", re.I)


def _events_linux(hours: int) -> list[dict]:
    try:
        r = subprocess.run(["journalctl", f"--since=-{int(hours)}h", "-p", "0..3", "-o", "json", "-n", "300",
                            "--no-pager"], capture_output=True, text=True, errors="replace", timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return []
    events = []
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        message = e.get("MESSAGE") if isinstance(e.get("MESSAGE"), str) else ""
        if not message or _LINUX_NOISE.search(message):
            continue
        provider = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or e.get("_SYSTEMD_UNIT") or "?"
        if provider == "kernel":
            provider = next((kind for kind, rx in _KERNEL_KINDS if rx.search(message)), "kernel (driver)")
        when = datetime.datetime.fromtimestamp(int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6)
        shape = re.sub(r"\d+", "#", message)[:80]          # same kind of message = same problem
        events.append({"t": f"{when:%Y-%m-%d %H:%M}", "p": provider,
                       "i": int(hashlib.md5(shape.encode()).hexdigest()[:4], 16),
                       "l": "Critical" if str(e.get("PRIORITY", "3")) in ("0", "1", "2") else "Error",
                       "m": " ".join(message.split())[:400]})
    return events


def _events(hours: int) -> list[dict]:
    if not IS_WINDOWS:
        return _events_linux(hours)
    script = (f"$e = Get-WinEvent -FilterHashtable @{{LogName='System','Application'; Level=1,2; "
              f"StartTime=(Get-Date).AddHours(-{int(hours)})}} -MaxEvents 200 -ErrorAction SilentlyContinue; "
              "if ($e) { $e | Select-Object @{n='t';e={$_.TimeCreated.ToString('yyyy-MM-dd HH:mm')}}, "
              "@{n='p';e={$_.ProviderName}}, @{n='i';e={$_.Id}}, @{n='l';e={$_.LevelDisplayName}}, "
              "@{n='m';e={($_.Message -split \"`n\")[0..2] -join ' '}} | ConvertTo-Json -Compress } else { '[]' }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, creationflags=oscompat.NO_WINDOW)
        data = json.loads(r.stdout.strip() or "[]")
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return []
    return data if isinstance(data, list) else [data]


def _category(provider: str, event_id: int) -> str:
    return next((name for name, test in CATEGORIES if test(provider, event_id)), "Lainnya")


def _crashed_app(message: str) -> str:
    m = re.search(r"(?:Faulting application name|Nama aplikasi yang salah):\s*([^,\s]+)", message or "") or \
        re.search(r"Process \d+ \(([^)]+)\) of user \d+ (?:dumped core|terminated abnormally)", message or "")
    return m.group(1) if m else ""


def collect(hours: int = 24) -> dict:
    events = [e for e in _events(hours) if (e.get("p"), e.get("i")) not in NOISE]
    groups = defaultdict(list)
    for e in events:
        groups[(e.get("p") or "?", int(e.get("i") or 0))].append(e)
    problems = []
    for (provider, event_id), items in groups.items():
        apps = Counter(_crashed_app(e.get("m", "")) for e in items)
        apps.pop("", None)
        problems.append({"category": _category(provider, event_id), "source": provider, "id": event_id,
                         "count": len(items), "last": max(e.get("t", "") for e in items),
                         "critical": any(e.get("l") in ("Critical", "Kritis") for e in items),
                         "apps": [a for a, _ in apps.most_common(3)],
                         "message": re.sub(r"\s+", " ", items[0].get("m", ""))[:220]})
    order = [c[0] for c in CATEGORIES] + ["Lainnya"]
    problems.sort(key=lambda p: (not p["critical"], order.index(p["category"]), -p["count"]))
    return {"hours": hours, "total": len(events), "problems": problems}


def _plain_summary(data: dict) -> str:
    by_cat = Counter()
    for p in data["problems"]:
        by_cat[p["category"]] += p["count"]
    parts = [f"{n} {cat.lower()}" for cat, n in by_cat.most_common(4)]
    crashes = [a for p in data["problems"] if p["category"] == "Aplikasi crash" for a in p["apps"]]
    text = f"Dalam {data['hours']} jam terakhir ada {data['total']} error penting: {', '.join(parts)}."
    if crashes:
        text += f" Aplikasi yang crash: {', '.join(dict.fromkeys(crashes))}."
    return text


def diagnosa_sistem(jam_terakhir: int = 24) -> str:
    """Diagnose Windows problems (crashes, blue screens, sudden shutdowns, disk/driver/hardware errors, slowness)
    from the Event Viewer. Use for 'diagnosa sistem', 'kenapa PC saya lambat', 'kenapa aplikasi sering crash',
    'cek masalah komputer'. jam_terakhir = how many hours back (default 24, e.g. 168 for a week)."""
    hours = max(1, min(int(jam_terakhir or 24), 24 * 30))
    data = collect(hours)
    if not data["problems"]:
        return (f"Tidak ada error penting di {SYSTEM_NAME} dalam {hours} jam terakhir, {T}. Sistem terlihat sehat. "
                f"Kalau terasa lambat, coba 'status sistem' untuk melihat CPU dan memori.")
    lines = [f"- [{p['category']}] {p['source']}" + (f" (ID {p['id']})" if IS_WINDOWS else "")
             + f" ×{p['count']}, terakhir {p['last']}"
             + (f", aplikasi: {', '.join(p['apps'])}" if p["apps"] else "") + f": {p['message']}"
             for p in data["problems"][:12]]
    try:
        answer = llm.generate(
            f"Ringkasan error {SYSTEM_NAME if IS_WINDOWS else 'Arch Linux (systemd journal)'} {hours} jam terakhir "
            f"(sudah dikelompokkan, noise dibuang):\n" + "\n".join(lines) +
            "\n\nJelaskan ke pengguna awam dalam bahasa Indonesia untuk DIBACAKAN (tanpa markdown/bullet, maks 90 kata): "
            "1-3 masalah terpenting, kemungkinan penyebabnya, dan saran perbaikan singkat. Jangan menakut-nakuti untuk "
            "error yang umum dan tidak berbahaya.", max_tokens=500)
        return f"{answer.strip()}"
    except Exception:
        return _plain_summary(data) + f" Detailnya ada di {SOURCE}, {T}."


if not IS_WINDOWS:   # what Gemini reads about the tool
    diagnosa_sistem.__doc__ = diagnosa_sistem.__doc__.replace(
        "Diagnose Windows problems (crashes, blue screens,", "Diagnose Linux problems (app crashes / core dumps,").replace(
        "from the Event Viewer.", "from the systemd journal.")

AI_TOOLS = [diagnosa_sistem]
