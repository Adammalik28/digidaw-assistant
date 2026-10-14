"""
Security analysis ("Digidaw, analisis sistem / analisis keamanan").

A read-only health check of the PC's security, run in the background (it takes ~20-60 s):
Windows Defender state & threat history, firewall, updates, UAC, proxy/hosts hijacking, programs that
start automatically, scheduled tasks, processes running from risky folders, open network ports,
remote-access tools, dangerous files in Downloads and software from unofficial sources.
The analysis itself only reads; every finding comes with advice, and the full report opens as a page.

Follow-up actions (each asks for confirmation first): clean orphaned startup entries (a backup is
written), close remote-access apps, run a Defender quick/full scan or scan one file.
In the background: an automatic scan every SECURITY_AUTO_SCAN_DAYS that only speaks up about findings
that are new since the previous scan, and a Downloads guard that warns about risky new downloads.
"""
import os
import re
import json
import html
import time
import datetime
import threading
import subprocess
import webbrowser
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import psutil

import config
import events
import oscompat
import safety

IS_WINDOWS = oscompat.IS_WINDOWS
if IS_WINDOWS:
    import winreg
else:
    import security_linux      # the Linux checks / actions (firewall, pacman, autostart, ClamAV, ...)

T = config.USER_TITLE
ANNOUNCE = None   # set by main (core.announce)

HOME = Path.home()
RISKY_DIRS = [str(HOME / "AppData" / "Local" / "Temp").lower(), str(HOME / "Downloads").lower(),
              str(HOME / "AppData" / "Roaming").lower(), r"c:\users\public", r"c:\programdata",
              os.environ.get("TEMP", "").lower()]
DANGEROUS_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".scr",
                 ".hta", ".jar", ".com", ".pif", ".lnk", ".reg"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".txt", ".mp4", ".mp3", ".zip", ".rar"}
REMOTE_TOOLS = {"teamviewer": "TeamViewer", "anydesk": "AnyDesk", "rustdesk": "RustDesk", "ultraviewer": "UltraViewer",
                "screenconnect": "ScreenConnect", "ngrok": "ngrok", "radmin": "Radmin", "vncserver": "VNC",
                "winvnc": "VNC", "tvnserver": "TightVNC", "remoteutilities": "Remote Utilities", "rutserv": "Remote Utilities"}
UNOFFICIAL = re.compile(r"elamigos|fitgirl|dodi[- ]?repack|kaoskrew|\bcodex\b|skidrow|empress|\bcrack|keygen|"
                        r"kms(?:pico|auto)|\bactivator\b", re.IGNORECASE)
HACK_TOOLS = re.compile(r"cheat ?engine|wemod|artmoney|process hacker|mimikatz", re.IGNORECASE)
THREAT_STATUS = {1: "terdeteksi", 2: "dibersihkan", 3: "dikarantina", 4: "dihapus", 5: "diizinkan", 6: "diblokir",
                 102: "gagal dikarantina", 103: "gagal dihapus", 104: "gagal diizinkan", 105: "ditinggalkan",
                 106: "ditangani", 107: "gagal diblokir"}
SEVERITY_ORDER = {"tinggi": 0, "sedang": 1, "rendah": 2, "info": 3}

_state = {"running": False, "last": None, "flagged_file": ""}
STATE_FILE = Path(config.LOG_DIR) / "security_state.json"
RISKY_DOWNLOAD_EXT = (DANGEROUS_EXT - {".lnk"}) | {".iso", ".img", ".vhd", ".vhdx", ".msix", ".appx", ".apk"}
PARTIAL_EXT = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut", ".downloading"}


# ───────────────────────────── helpers ─────────────────────────────

def _ps_json(script: str, timeout: int = 60):
    """Run PowerShell and parse its JSON output (None on failure)."""
    full = ("$ErrorActionPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            + script)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=oscompat.NO_WINDOW)
        out = r.stdout.strip()
        return json.loads(out) if out else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, dict) and "value" in value and "Count" in value:   # PowerShell 5.1 array wrapper
        value = value["value"]
    return value if isinstance(value, list) else [value]


def _risky_path(path: str) -> bool:
    p = (path or "").lower()
    return any(d and p.startswith(d) for d in RISKY_DIRS)


def _exe_from_command(command: str) -> str:
    command = os.path.expandvars((command or "").strip())
    m = re.match(r'^"([^"]+)"', command) or \
        re.match(r"^(.+?\.(?:exe|bat|cmd|vbs|js|ps1|com|scr|lnk))(?=\s|$)", command, re.I)
    return m.group(1) if m else command.split(" ")[0]


def _signatures(paths: list[str]) -> dict[str, str]:
    """Authenticode status per file: Valid / NotSigned / HashMismatch / ... (one PowerShell call)."""
    paths = sorted({p for p in paths if p and os.path.isfile(p)})[:80]
    if not paths:
        return {}
    listing = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    data = _ps_json(f"@({listing}) | ForEach-Object {{ $s = Get-AuthenticodeSignature -FilePath $_; "
                    f"[pscustomobject]@{{Path=$_; Status=[string]$s.Status; "
                    f"Signer=[string]$s.SignerCertificate.Subject}} }} | ConvertTo-Json -Compress", timeout=90)
    result = {}
    for item in _as_list(data):
        signer = re.search(r"CN=([^,]+)", item.get("Signer") or "")
        result[item["Path"].lower()] = item.get("Status", "Unknown") + (f" ({signer.group(1)})" if signer else "")
    return result


def _add(findings, severity, category, title, detail="", advice=""):
    findings.append({"severity": severity, "category": category, "title": title, "detail": detail, "advice": advice})


# ───────────────────────────── checks ─────────────────────────────

def check_windows_security(findings):
    data = _ps_json(
        "$m = Get-MpComputerStatus; $fw = Get-NetFirewallProfile | Select-Object Name, Enabled; "
        "$hf = Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1; "
        "$uac = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System').EnableLUA; "
        "$ie = Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'; "
        "[pscustomobject]@{ av=$m.AntivirusEnabled; rtp=$m.RealTimeProtectionEnabled; tamper=$m.IsTamperProtected; "
        "sigAge=$m.AntivirusSignatureAge; quickAge=$m.QuickScanAge; fullAge=$m.FullScanAge; "
        "firewall=@($fw); hotfix=$hf.HotFixID; hotfixDate=if($hf.InstalledOn){$hf.InstalledOn.ToString('yyyy-MM-dd')}else{''}; "
        "uac=$uac; proxy=$ie.ProxyEnable; proxyServer=$ie.ProxyServer; autoConfig=$ie.AutoConfigURL } | ConvertTo-Json -Depth 4 -Compress")
    if not data:
        _add(findings, "sedang", "Windows Security", "Status Windows Security tidak bisa dibaca",
             "PowerShell tidak mengembalikan data.", "Buka Windows Security secara manual untuk memastikan antivirus aktif.")
        return
    if data.get("av") is False:
        _add(findings, "tinggi", "Antivirus", "Antivirus Windows Defender MATI", "",
             "Aktifkan kembali di Windows Security > Virus & threat protection, atau pastikan antivirus lain aktif.")
    elif data.get("rtp") is False:
        _add(findings, "tinggi", "Antivirus", "Perlindungan real-time Defender MATI", "",
             "Nyalakan Real-time protection di Windows Security.")
    else:
        _add(findings, "ok", "Antivirus", "Windows Defender aktif dengan perlindungan real-time")
    if (data.get("sigAge") or 0) > 3:
        _add(findings, "sedang", "Antivirus", f"Database virus sudah {data['sigAge']} hari tidak diperbarui", "",
             "Jalankan Windows Update atau klik 'Check for updates' di Windows Security.")
    if data.get("tamper") is False:
        _add(findings, "sedang", "Antivirus", "Tamper Protection mati",
             "Tanpa ini, malware bisa mematikan Defender.", "Aktifkan Tamper Protection di Windows Security.")
    if (data.get("fullAge") or 0) > 90:
        _add(findings, "rendah", "Antivirus", f"Full scan terakhir {data['fullAge']} hari lalu", "",
             "Ucapkan 'jalankan full scan' (berjalan di latar belakang, bisa lebih dari satu jam).")
    off = [f["Name"] for f in _as_list(data.get("firewall")) if not f.get("Enabled")]
    if off:
        _add(findings, "tinggi", "Firewall", f"Firewall mati untuk jaringan: {', '.join(off)}", "",
             "Nyalakan kembali di Windows Security > Firewall & network protection.")
    else:
        _add(findings, "ok", "Firewall", "Firewall aktif di semua jaringan")
    if data.get("hotfixDate"):
        age = (datetime.date.today() - datetime.date.fromisoformat(data["hotfixDate"])).days
        if age > 45:
            _add(findings, "sedang", "Windows Update", f"Update Windows terakhir {age} hari lalu ({data.get('hotfix')})", "",
                 "Buka Settings > Windows Update dan pasang update yang tertunda.")
        else:
            _add(findings, "ok", "Windows Update", f"Update terakhir {age} hari lalu ({data.get('hotfix')})")
    if data.get("uac") == 0:
        _add(findings, "tinggi", "UAC", "User Account Control (UAC) dimatikan",
             "Program bisa berjalan sebagai administrator tanpa sepengetahuan Anda.", "Nyalakan kembali UAC di Control Panel.")
    if data.get("proxy") == 1 or data.get("autoConfig"):
        _add(findings, "sedang", "Jaringan", "Proxy / skrip konfigurasi proxy aktif",
             f"Server: {data.get('proxyServer') or data.get('autoConfig')}",
             "Jika Anda tidak sengaja memasangnya, matikan di Settings > Network > Proxy (bisa tanda pembajakan browser).")


def check_threat_history(findings):
    data = _ps_json(
        "$t = @(Get-MpThreat | Select-Object ThreatID, ThreatName, SeverityID, IsActive, DidThreatExecute); "
        "$d = @(Get-MpThreatDetection | Sort-Object InitialDetectionTime -Descending | Select-Object ThreatID, ThreatStatusID, "
        "@{n='When';e={$_.InitialDetectionTime.ToString('yyyy-MM-dd')}}, @{n='Res';e={[string[]]@($_.Resources)}}); "
        "[pscustomobject]@{threats=$t; detections=$d} | ConvertTo-Json -Depth 4 -Compress")
    if not data:
        return
    detections = _as_list(data.get("detections"))
    for threat in _as_list(data.get("threats")):
        mine = [d for d in detections if d.get("ThreatID") == threat.get("ThreatID")]
        files = sorted({r[len("file:_"):] for d in mine for r in _as_list(d.get("Res"))
                        if isinstance(r, str) and r.startswith("file:_")})
        still_there = [f for f in files if os.path.exists(f)]
        name = threat.get("ThreatName", "?")
        pua = name.upper().startswith("PUA")
        last = mine[0]["When"] if mine else "?"
        status = THREAT_STATUS.get(mine[0].get("ThreatStatusID"), "?") if mine else "?"
        detail = f"Terakhir terdeteksi {last} ({len(mine)}x), status: {status}." + \
                 (f" File: {', '.join(files[:3])}" if files else "")
        if threat.get("IsActive"):
            _add(findings, "tinggi", "Ancaman", f"Ancaman MASIH AKTIF: {name}", detail,
                 "Buka Windows Security > Protection history dan pilih Remove / Quarantine, lalu jalankan Full scan.")
        elif still_there:
            _add(findings, "sedang" if pua else "tinggi", "Ancaman",
                 f"File yang ditandai {'aplikasi tidak diinginkan' if pua else 'berbahaya'} masih ada: {name}",
                 detail + f" Masih ada di: {', '.join(still_there[:3])}",
                 "Hapus file tersebut jika tidak Anda perlukan (ucapkan: 'Digidaw, hapus file ...'), "
                 "atau pastikan berasal dari sumber resmi.")
        else:
            _add(findings, "info", "Ancaman", f"Riwayat: {name} sudah ditangani Defender", detail)
    if not _as_list(data.get("threats")):
        _add(findings, "ok", "Ancaman", "Tidak ada ancaman dalam riwayat Defender")


def _startup_entries() -> list[dict]:
    entries = []
    keys = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM Run (32-bit)"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU RunOnce"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM RunOnce")]
    for hive, path, label in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, i)
                    entries.append({"name": name, "command": str(value), "where": label,
                                    "hive": "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM", "key": path})
        except OSError:
            continue
    for folder in (HOME / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup",
                   Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs/StartUp"):
        for f in folder.glob("*") if folder.exists() else []:
            if f.name.lower() != "desktop.ini":
                entries.append({"name": f.stem, "command": str(f), "where": "folder Startup", "file": str(f)})
    return entries


def _all_startup() -> list[dict]:
    """Run keys, Startup folders and non-Microsoft scheduled tasks, each with the program it starts ("exe")."""
    entries = _startup_entries()
    tasks = _as_list(_ps_json(
        "Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' -and $_.State -ne 'Disabled' } | "
        "ForEach-Object { $a = $_.Actions | Select-Object -First 1; [pscustomobject]@{name=$_.TaskName; path=$_.TaskPath; "
        "command=(([string]$a.Execute) + ' ' + ([string]$a.Arguments)).Trim()} } | ConvertTo-Json -Compress"))
    for t in tasks:
        entries.append({"name": t.get("name"), "command": t.get("command") or "", "where": "Scheduled Task",
                        "task_path": t.get("path") or "\\"})
    links = [e["command"] for e in entries if e["command"].lower().endswith(".lnk")]
    targets = {}
    if links:
        listing = ",".join("'" + l.replace("'", "''") + "'" for l in links)
        for item in _as_list(_ps_json(f"$sh = New-Object -ComObject WScript.Shell; @({listing}) | ForEach-Object "
                                      f"{{ [pscustomobject]@{{l=$_; t=$sh.CreateShortcut($_).TargetPath}} }} | ConvertTo-Json -Compress")):
            targets[item["l"]] = item.get("t") or ""
    for e in entries:
        e["exe"] = targets.get(e["command"]) or _exe_from_command(e["command"])
    return entries


def _is_orphan(exe: str) -> bool:
    return bool(exe) and "\\windowsapps\\" not in exe.lower() and (":" in exe or "\\" in exe) and not os.path.exists(exe)


def check_autostart(findings):
    entries = _all_startup()
    obfuscated = re.compile(r"-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{20,}|frombase64string|iex\s*\(|downloadstring|"
                            r"mshta\s+http|regsvr32\s+/i:http|bitsadmin\s+/transfer", re.I)
    sigs = _signatures([e["exe"] for e in entries if _risky_path(e["exe"])])
    flagged, orphans = 0, []
    for e in entries:
        exe = e["exe"]
        if obfuscated.search(e["command"]):
            flagged += 1
            _add(findings, "tinggi", "Startup", f"Perintah tersamar berjalan otomatis: {e['name']} ({e['where']})",
                 e["command"][:220], "Pola ini sering dipakai malware. Nonaktifkan di Task Scheduler / Task Manager > Startup, lalu jalankan Full scan.")
        elif _is_orphan(exe):
            orphans.append(e["name"])
        elif Path(exe).name.lower() in ("pythonw.exe", "python.exe") and "digidaw" in e["command"].lower() \
                or "digidaw" in e["name"].lower() and "digidaw" in exe.lower():
            continue   # Digidaw itself
        elif _risky_path(exe):
            sig = sigs.get(exe.lower(), "tidak dicek")
            if sig.startswith("Valid"):
                if re.search(r"torrent|utweb|bittorrent", exe, re.I):
                    _add(findings, "rendah", "Startup", f"Aplikasi torrent berjalan otomatis: {e['name']}", exe,
                         "Aplikasi torrent sering membawa iklan/bundel dan membagikan file Anda. Matikan startup-nya jika jarang dipakai.")
                continue
            flagged += 1
            _add(findings, "tinggi" if "NotSigned" in sig or "HashMismatch" in sig else "sedang", "Startup",
                 f"Program tanpa tanda tangan digital berjalan otomatis: {e['name']} ({e['where']})",
                 f"{exe} — tanda tangan: {sig}",
                 "Periksa apakah Anda mengenal program ini. Jika tidak, nonaktifkan di Task Manager > Startup apps dan scan filenya.")
    if orphans:
        _add(findings, "rendah", "Startup", f"{len(orphans)} entri startup yatim (programnya sudah dihapus)",
             ", ".join(orphans[:8]),
             "Sisa program yang sudah di-uninstall. Tidak berbahaya. Ucapkan 'bersihkan startup yatim' untuk "
             "menghapusnya (cadangannya disimpan).")
    if not flagged:
        _add(findings, "ok", "Startup", f"{len(entries)} program startup / scheduled task diperiksa, tidak ada yang mencurigakan")


def check_processes_and_network(findings):
    risky_procs = {}
    for p in psutil.process_iter(["pid", "name", "exe"]):
        exe = p.info.get("exe") or ""
        if exe and _risky_path(exe) and "\\appdata\\local\\programs\\" not in exe.lower():
            risky_procs[exe] = p.info["name"]
    listening = {}
    for c in psutil.net_connections("inet"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.ip in ("0.0.0.0", "::") and c.pid:
            try:
                proc = psutil.Process(c.pid)
                listening.setdefault(c.laddr.port, (proc.name(), proc.exe()))
            except psutil.Error:
                continue
    sigs = _signatures(list(risky_procs) + [exe for _, exe in listening.values() if _risky_path(exe)])
    unsigned = [(exe, name) for exe, name in risky_procs.items() if not sigs.get(exe.lower(), "").startswith("Valid")]
    for exe, name in unsigned[:6]:
        _add(findings, "sedang", "Proses", f"Program tanpa tanda tangan berjalan dari folder rawan: {name}", exe,
             "Program dari Temp/Downloads/AppData yang tidak ditandatangani perlu dicek. Jika tidak dikenal, tutup dan scan filenya.")
    if not unsigned:
        _add(findings, "ok", "Proses", f"{len(psutil.pids())} proses berjalan, tidak ada yang mencurigakan dari folder rawan")

    if 3389 in listening:
        _add(findings, "sedang", "Jaringan", "Remote Desktop (port 3389) terbuka",
             "Orang lain di jaringan bisa mencoba login ke PC ini.", "Matikan Remote Desktop di Settings > System jika tidak dipakai.")
    for port, (name, exe) in sorted(listening.items()):
        if _risky_path(exe) and not sigs.get(exe.lower(), "").startswith("Valid"):
            _add(findings, "tinggi", "Jaringan", f"Program tidak dikenal membuka port {port} ke jaringan: {name}", exe,
                 "Ini bisa berarti backdoor. Tutup programnya, scan filenya, dan periksa kapan dipasang.")
    public = [f"{port} ({name})" for port, (name, _) in sorted(listening.items())
              if port not in (135, 139, 445, 5040, 7680) and port < 49152]
    if public:
        _add(findings, "info", "Jaringan", f"Port yang menerima koneksi dari jaringan: {', '.join(public[:10])}", "",
             "Normal untuk aplikasi seperti Spotify, server development, atau game. Tutup yang tidak Anda kenal.")

    running = {p.info["name"].lower(): p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]}
    for key, label in REMOTE_TOOLS.items():
        if any(key in name for name in running):
            _add(findings, "sedang", "Remote Access", f"Aplikasi remote access sedang berjalan: {label}",
                 "Aplikasi ini memungkinkan orang lain mengendalikan PC Anda dari jauh.",
                 f"Pastikan Anda sendiri yang memasang {label} dan tidak ada sesi yang tidak dikenal. "
                 f"Ucapkan 'tutup aplikasi remote' untuk menutupnya.")


def check_files(findings):
    downloads = HOME / "Downloads"
    cutoff = time.time() - 120 * 86400
    candidates, double_ext = [], []
    if downloads.exists():
        for f in downloads.rglob("*"):
            try:
                if not f.is_file() or f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            suffixes = [s.lower() for s in f.suffixes[-2:]]
            if f.suffix.lower() in DANGEROUS_EXT:
                candidates.append(str(f))
                if len(suffixes) == 2 and suffixes[0] in DOC_EXT:
                    double_ext.append(str(f))
            if len(candidates) > 200:
                break
    for f in double_ext[:5]:
        _add(findings, "tinggi", "File", f"File menyamar (ekstensi ganda): {Path(f).name}", f,
             "Ini trik klasik malware (mis. 'invoice.pdf.exe'). Jangan dibuka; hapus file ini.")
    execs = [f for f in candidates if Path(f).suffix.lower() in (".exe", ".msi", ".scr", ".com")]
    sigs = _signatures(execs)
    unsigned = [f for f in execs if f not in double_ext and not sigs.get(f.lower(), "").startswith("Valid")]
    scripts = [f for f in candidates if Path(f).suffix.lower() in (".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".hta", ".wsf")]
    if unsigned:
        _add(findings, "sedang", "File", f"{len(unsigned)} installer/program tanpa tanda tangan di Downloads",
             ", ".join(Path(f).name for f in unsigned[:8]),
             "Program tanpa tanda tangan digital tidak bisa dipastikan asalnya. Hapus yang tidak dikenal atau scan dengan Defender (klik kanan > Scan with Microsoft Defender).")
    if scripts:
        _add(findings, "rendah", "File", f"{len(scripts)} file skrip di Downloads", ", ".join(Path(f).name for f in scripts[:8]),
             "Skrip (.bat, .ps1, .vbs, .js) bisa menjalankan perintah apa pun. Jangan jalankan jika tidak tahu isinya.")
    if not (double_ext or unsigned or scripts):
        _add(findings, "ok", "File", f"Tidak ada file berbahaya di Downloads (120 hari terakhir, {len(candidates)} program diperiksa)")

    hosts = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/drivers/etc/hosts"
    try:
        entries = [l.split() for l in hosts.read_text(encoding="utf-8", errors="replace").splitlines()
                   if l.strip() and not l.strip().startswith("#")]
        odd = [" ".join(e[:2]) for e in entries if len(e) >= 2 and e[1].lower() not in ("localhost",)
               and not e[0].startswith(("127.", "0.0.0.0", "::1"))]
        if odd:
            _add(findings, "sedang", "Jaringan", "File hosts mengalihkan alamat website", "; ".join(odd[:5]),
                 "Malware kadang mengubah file hosts untuk mengarahkan situs bank/login ke server palsu. Periksa entri ini.")
    except OSError:
        pass

    try:
        import system_control
        apps = system_control._start_apps_cache or system_control._start_apps()
    except Exception:
        apps = []
    unofficial = {a["Name"] for a in apps if UNOFFICIAL.search(a.get("Name", "") + " " + a.get("AppID", ""))}
    unofficial |= {Path(f).name for f in candidates if UNOFFICIAL.search(Path(f).name)}
    menus = [HOME / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs"]
    for menu in menus:
        for link in menu.rglob("*") if menu.exists() else []:
            if UNOFFICIAL.search(link.name):
                unofficial.add(f"{link.parent.name} (repack)" if link.parent != menu else link.stem)
    hack = sorted({a["Name"] for a in apps if HACK_TOOLS.search(a.get("Name", "") + " " + a.get("AppID", ""))})
    if hack:
        _add(findings, "sedang", "Software", f"Tool cheat/peretasan terpasang: {', '.join(hack[:4])}",
             "Tool seperti Cheat Engine bisa memuat driver kernel dan installernya pernah membawa adware.",
             "Uninstall jika tidak dipakai lagi; jangan jalankan sebagai administrator tanpa perlu.")
    unofficial = sorted(unofficial)
    if unofficial:
        _add(findings, "sedang", "Software", "Software dari sumber tidak resmi (repack/crack/activator) terdeteksi",
             ", ".join(sorted(set(unofficial))[:6]),
             "Installer repack, crack, dan activator adalah jalur paling umum masuknya malware. Pastikan sudah di-scan, "
             "dan hindari menjalankannya sebagai administrator.")


# ───────────────────────────── report ─────────────────────────────

def _score(findings) -> int:
    penalty = {"tinggi": 25, "sedang": 8, "rendah": 3}
    return max(0, 100 - sum(penalty.get(f["severity"], 0) for f in findings))


COMMAND_RE = re.compile(r"((?:sudo|systemctl|pacman|yay|paru|ufw|flatpak|freshclam|clamscan)\s[^,;]+?)(?=,\s|;|\.$|\)|$)")
SEVERITY_LABEL = {"tinggi": "Tinggi", "sedang": "Sedang", "rendah": "Rendah", "info": "Info", "ok": "Aman"}


def _split_command(advice: str) -> tuple[str, str]:
    """'Jalankan: sudo pacman -Syu, lalu restart ...' -> ('Jalankan perintah di bawah, lalu restart ....',
    'sudo pacman -Syu'); the command gets its own copyable block."""
    m = COMMAND_RE.search(advice or "")
    if not m:
        return advice, ""
    command = m[1].strip().rstrip(".")
    prefix = advice[:m.start()].strip().rstrip(":( ").strip()
    suffix = advice[m.end():].strip(" .,;)")
    if not prefix or prefix.lower() in ("jalankan", "ketik"):
        lead = "Jalankan perintah di bawah"
    else:
        lead = f"{prefix}, jalankan perintah di bawah"
    return lead + (f", {suffix}" if suffix else "") + ".", command


def _finding_html(f: dict, group: str) -> str:
    import report_theme as rt
    advice, command = _split_command(f.get("advice", ""))
    detail = f.get("detail") or ""
    return (f'<article class="finding" data-sev="{f["severity"]}" data-group="{group}" '
            f'style="--c:{rt.COLORS[f["severity"]]}"><div class="meta"><span class="sev">'
            f'{SEVERITY_LABEL[f["severity"]]}</span>{rt.esc(f["category"])}</div>'
            f'<h3>{rt.esc(f["title"])}</h3>'
            + (f'<div class="detail">{rt.esc(detail)}</div>' if detail else "")
            + (f'<div class="advice">{rt.esc(advice)}</div>' if advice else "")
            + (rt.command_block(command) if command else "") + "</article>")


def _write_report(findings, score, started) -> Path:
    import report_theme as rt
    try:
        import system_control
        folder = system_control._known_folder("documents") / "Digidaw Reports"
    except Exception:
        folder = HOME / "Documents" / "Digidaw Reports"
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    path = folder / f"Analisis_Keamanan_{now:%Y-%m-%d_%H%M}.html"
    ordered = sorted(findings, key=lambda x: SEVERITY_ORDER.get(x["severity"], 4))
    counts = {k: sum(1 for f in findings if f["severity"] == k) for k in SEVERITY_LABEL}
    act, tips = counts["tinggi"] + counts["sedang"], counts["rendah"] + counts["info"]
    verdict = ("Sangat baik" if score >= 90 else "Baik" if score >= 80 else "Cukup, ada yang perlu dibereskan"
               if score >= 55 else "Perlu perhatian segera")
    summary = (f"{act} hal sebaiknya segera dibereskan" if act else "Tidak ada yang perlu segera dibereskan") + \
              (f", {tips} saran untuk diperhatikan" if tips else "") + f", dan {counts['ok']} pemeriksaan sudah aman."
    groups = [("act", "Perlu tindakan", [f for f in ordered if f["severity"] in ("tinggi", "sedang")]),
              ("tips", "Saran & informasi", [f for f in ordered if f["severity"] in ("rendah", "info")])]
    sections = ""
    for key, title, items in groups:
        if items:
            sections += f'<div class="group" data-group="{key}">{title} · {len(items)}</div>'
            sections += "".join(_finding_html(f, key) for f in items)
    safe = [f for f in ordered if f["severity"] == "ok"]
    if safe:
        sections += (f'<div class="group" data-group="ok">Sudah aman · {len(safe)}</div>'
                     f'<details class="ok-list"><summary>Tampilkan {len(safe)} pemeriksaan yang aman</summary>'
                     + "".join(_finding_html(f, "ok") for f in safe) + "</details>")
    filters = [("all", f"Semua · {len(findings)}", "#5ccfff")] + [
        (k, f"{SEVERITY_LABEL[k]} · {counts[k]}", rt.COLORS[k]) for k in ("tinggi", "sedang", "rendah", "info", "ok")
        if counts[k]]
    note = ("Analisis ini hanya membaca kondisi sistem; tidak ada file yang dihapus atau diubah. Untuk pemeriksaan "
            "malware yang tuntas, " + ("jalankan juga Full scan di Windows Security." if IS_WINDOWS else
                                       'ucapkan "jalankan full scan" (memakai ClamAV).'))
    body = (f'<section class="hero">{rt.score_ring(score, "dari 100")}<div><h2>{verdict}</h2><p>{rt.esc(summary)}</p></div>'
            f'</section>{rt.chips(filters)}{sections}<footer>{rt.esc(note)}</footer>')
    import platform
    page = rt.page("Analisis Keamanan Sistem", "Digidaw · Keamanan",
                   f"{rt.tanggal(now)} · selesai dalam {int(time.time() - started)} detik",
                   body, badges=[platform.node() or "komputer ini", platform.system() + " " + platform.release()])
    path.write_text(page, encoding="utf-8")
    return path


def _spoken_summary(findings, score) -> str:
    high = [f for f in findings if f["severity"] == "tinggi"]
    medium = [f for f in findings if f["severity"] == "sedang"]
    verdict = "sistem Anda dalam kondisi aman" if score >= 80 else "ada beberapa hal yang perlu perhatian" \
        if score >= 55 else "ada risiko serius yang perlu segera ditangani"
    text = f"Analisis keamanan selesai, {T}. Skornya {score} dari 100, {verdict}."
    top = (high or medium)[:2]
    if top:
        text += " Yang terpenting: " + "; ".join(f["title"] for f in top) + "."
    if high or medium:
        text += f" Totalnya {len(high)} temuan tinggi dan {len(medium)} sedang. Laporan lengkap dan sarannya saya buka."
    else:
        text += " Laporan lengkapnya saya buka."
    return text


def _key(f: dict) -> str:
    return f["category"] + "|" + re.sub(r"\d+", "#", f["title"])


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _run_scan(auto: bool = False):
    started = time.time()
    findings: list[dict] = []
    checks = [("Windows Security", check_windows_security), ("riwayat ancaman", check_threat_history),
              ("program startup", check_autostart), ("proses & jaringan", check_processes_and_network),
              ("file & software", check_files)] if IS_WINDOWS else security_linux.CHECKS
    events.emit("security", status="running", step="Memulai analisis…")
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fn, findings): label for label, fn in checks}
            for future in futures:
                label = futures[future]
                try:
                    future.result(timeout=180)
                except Exception as e:
                    _add(findings, "info", "Analisis", f"Pemeriksaan {label} tidak selesai", str(e)[:150])
                events.emit("security", status="running", step=f"Selesai: {label}")
        score = _score(findings)
        report = _write_report(findings, score, started)
        _state["last"] = {"score": score, "report": str(report), "findings": findings, "when": time.time()}
        previous = _load_state()
        known = set(previous.get("keys", []))
        new = [f for f in findings if f["severity"] in ("tinggi", "sedang", "rendah") and _key(f) not in known] \
            if previous.get("last_scan") else []
        _save_state({"last_scan": time.time(), "score": score, "report": str(report),
                     "keys": [_key(f) for f in findings if f["severity"] not in ("ok", "info")]})
        events.emit("security", status="done", score=score, report=str(report),
                    high=sum(f["severity"] == "tinggi" for f in findings),
                    medium=sum(f["severity"] == "sedang" for f in findings))
        if auto:
            if not new:
                events.emit("toast", text=f"Scan keamanan otomatis: skor {score}/100, tidak ada temuan baru.", level="info")
                return
            message = (f"Scan keamanan otomatis selesai, {T}. Ada {len(new)} temuan baru sejak scan sebelumnya: "
                       + "; ".join(f["title"] for f in sorted(new, key=lambda x: SEVERITY_ORDER[x["severity"]])[:3])
                       + ". Ucapkan 'buka laporan keamanan' untuk detail dan sarannya.")
        else:
            webbrowser.open(report.as_uri())
            message = _spoken_summary(findings, score)
            if new:
                message += f" {len(new)} di antaranya baru sejak scan sebelumnya."
    except Exception as e:
        message = f"Maaf {T}, analisis keamanan gagal: {e}"
    finally:
        _state["running"] = False
    if ANNOUNCE:
        ANNOUNCE(message, "info")
    else:
        print(message)


# ───────────────────────────── skills ─────────────────────────────

def security_scan() -> str:
    """Full security analysis of the PC ('analisis sistem', 'analisis keamanan', 'cek virus', 'apakah laptop saya
    aman'): antivirus & firewall state, threat history, suspicious startup programs, processes, open ports,
    remote-access tools, dangerous files and unofficial software. Runs in the background (about a minute), then
    reports a score and opens a detailed report. Read-only: nothing is deleted."""
    if _state["running"]:
        return f"Analisis keamanan masih berjalan, {T}. Saya kabari begitu selesai."
    _state["running"] = True
    threading.Thread(target=_run_scan, daemon=True, name="security-scan").start()
    if not IS_WINDOWS:
        return (f"Baik, {T}. Saya mulai analisis keamanan: firewall, update sistem, program startup, proses, "
                f"jaringan, dan file berbahaya. Sekitar satu menit; saya kabari hasilnya.")
    return (f"Baik, {T}. Saya mulai analisis keamanan menyeluruh: antivirus, riwayat ancaman, program startup, "
            f"proses, jaringan, dan file berbahaya. Sekitar satu menit; saya kabari hasilnya.")


def last_security_report() -> str:
    """Reopen / summarise the most recent security analysis report."""
    last = _state.get("last")
    if not last:
        folder = HOME / "Documents" / "Digidaw Reports"
        reports = sorted(folder.glob("Analisis_Keamanan_*.html")) if folder.exists() else []
        if not reports:
            return f"Belum ada laporan keamanan, {T}. Ucapkan 'analisis keamanan' untuk membuatnya."
        webbrowser.open(reports[-1].as_uri())
        return f"Membuka laporan keamanan terakhir, {T}."
    webbrowser.open(Path(last["report"]).as_uri())
    return _spoken_summary(last["findings"], last["score"])


# ───────────────────────────── actions ─────────────────────────────

def _reports_dir() -> Path:
    try:
        import system_control
        folder = system_control._known_folder("documents") / "Digidaw Reports"
    except Exception:
        folder = HOME / "Documents" / "Digidaw Reports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def clean_orphan_startup() -> str:
    """Remove startup entries / scheduled tasks that point to programs which no longer exist
    ('bersihkan startup yatim'). A backup of what is removed is saved first. Asks confirmation."""
    if not IS_WINDOWS:
        return security_linux.clean_orphan_startup()
    orphans = [e for e in _all_startup() if _is_orphan(e["exe"])]
    if not orphans:
        return f"Tidak ada entri startup yatim, {T}. Semuanya menunjuk ke program yang masih ada."
    names = ", ".join(e["name"] for e in orphans[:5]) + (f" dan {len(orphans) - 5} lainnya" if len(orphans) > 5 else "")
    needs_admin = any(e.get("hive") == "HKLM" or e.get("task_path") for e in orphans)
    return safety.guard(f"menghapus {len(orphans)} entri startup yatim ({names})"
                        + (" — sebagian butuh izin administrator" if needs_admin else ""),
                        _do_clean_orphans, orphans=orphans)


def _do_clean_orphans(orphans: list[dict]) -> str:
    backup = _reports_dir() / f"cadangan_startup_{datetime.datetime.now():%Y-%m-%d_%H%M}.json"
    backup.write_text(json.dumps(orphans, ensure_ascii=False, indent=1), encoding="utf-8")
    done, admin = [], []
    for e in orphans:
        try:
            if e.get("hive") == "HKCU":
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, e["key"], 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, e["name"])
                done.append(e["name"])
            elif e.get("file"):
                Path(e["file"]).unlink()
                done.append(e["name"])
            else:
                admin.append(e)
        except OSError:
            admin.append(e)
    # user-created tasks can usually be removed without admin; try that first
    for e in [a for a in admin if a.get("task_path")]:
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", e["task_path"] + e["name"]], capture_output=True,
                           creationflags=oscompat.NO_WINDOW)
        if r.returncode == 0:
            done.append(e["name"])
            admin.remove(e)
    failed = []
    if admin:
        import system_control
        lines = []
        for e in admin:
            if e.get("task_path"):
                lines.append(f"Unregister-ScheduledTask -TaskName {_ps_quote(e['name'])} -TaskPath {_ps_quote(e['task_path'])} -Confirm:$false")
            elif e.get("hive"):
                lines.append(f"Remove-ItemProperty -Path {_ps_quote(e['hive'] + ':' + chr(92) + e['key'])} -Name {_ps_quote(e['name'])}")
        ok, _ = system_control.run_elevated(chr(10).join(lines))
        still = {e["name"] for e in orphans if _is_orphan(e["exe"])} & {e["name"] for e in _all_startup()} if ok else set()
        for e in admin:
            (failed if not ok or e["name"] in still else done).append(e["name"])
    text = f"{len(done)} entri startup yatim sudah dihapus, {T}. Cadangannya ada di {backup.name}."
    if failed:
        text += f" {len(failed)} tidak bisa dihapus ({', '.join(failed[:3])}), biasanya karena izin administrator ditolak."
    return text


def close_remote_access_apps() -> str:
    """Close remote-access apps (TeamViewer, AnyDesk, RustDesk, ...) and stop their background services
    so nobody can connect to this PC ('tutup aplikasi remote', 'matikan teamviewer'). Asks confirmation."""
    if not IS_WINDOWS:
        return security_linux.close_remote_access_apps()
    procs = [p for p in psutil.process_iter(["name", "pid"])
             if any(k in (p.info["name"] or "").lower() for k in REMOTE_TOOLS)]
    services = _as_list(_ps_json("Get-Service | Where-Object { $_.Status -eq 'Running' -and ($_.Name -match "
                                 "'teamviewer|anydesk|rustdesk|ultraviewer|screenconnect|radmin|vnc|rutserv') } | "
                                 "Select-Object Name, DisplayName | ConvertTo-Json -Compress"))
    if not procs and not services:
        return f"Tidak ada aplikasi remote access yang berjalan, {T}."
    labels = sorted({label for p in procs for k, label in REMOTE_TOOLS.items() if k in p.info["name"].lower()}
                    | {s.get("DisplayName") or s.get("Name") for s in services})
    extra = " dan layanan latarnya (butuh izin administrator)" if services else ""
    return safety.guard(f"menutup {', '.join(labels)}{extra}", _do_close_remote,
                        pids=[p.pid for p in procs], services=[s["Name"] for s in services])


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
        script = chr(10).join([f"Stop-Service -Force -Name {_ps_quote(n)}" for n in services]
                           + ([f"Stop-Process -Force -Id {','.join(map(str, denied))}"] if denied else []))
        ok, _ = system_control.run_elevated(script)
        if not ok:
            return (f"Aplikasinya sudah saya tutup, tetapi layanan latarnya masih jalan karena izin administrator "
                    f"tidak diberikan, {T}. Layanan itu masih bisa menerima koneksi.")
        return (f"Aplikasi remote access dan layanannya sudah dihentikan, {T}. Layanan akan aktif lagi setelah restart "
                f"kecuali Anda uninstall aplikasinya.")
    return f"Aplikasi remote access sudah ditutup, {T}."


def _mpcmdrun() -> str | None:
    platform = sorted(Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Microsoft", "Windows Defender",
                           "Platform").glob("*/MpCmdRun.exe"))
    fallback = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe")
    return str(platform[-1]) if platform else str(fallback) if fallback.exists() else None


def _defender(args: list[str], label: str, timeout: int):
    exe = _mpcmdrun()
    events.emit("security", status="running", step=f"{label} berjalan…")
    if not exe:
        message = f"Program pemindai Windows Defender tidak ditemukan, {T}."
    else:
        try:
            r = subprocess.run([exe, *args], capture_output=True, text=True, errors="replace", timeout=timeout,
                               creationflags=oscompat.NO_WINDOW)
            out = r.stdout + r.stderr
            threats = re.findall(r"Threat\s*:\s*(.+)", out)
            if r.returncode == 0:
                message = f"{label} selesai, {T}. Tidak ada ancaman ditemukan."
            elif r.returncode == 2 or threats:
                message = (f"{label} selesai, {T}. Defender menemukan ancaman: {', '.join(sorted(set(threats))[:3]) or 'lihat Windows Security'}. "
                           f"Buka Windows Security > Protection history untuk menanganinya.")
            else:
                message = f"{label} berhenti dengan kode {r.returncode}, {T}. Detailnya ada di Windows Security."
        except subprocess.TimeoutExpired:
            message = f"{label} masih berjalan lebih lama dari biasanya, {T}; hasilnya nanti ada di Windows Security."
    events.emit("security", status="scan-done", step=message)
    if ANNOUNCE:
        ANNOUNCE(message, "info")


def defender_scan(kind: str = "quick") -> str:
    """Run a Windows Defender scan in the background: kind='quick' (a few minutes) or 'full' (can take over
    an hour). 'jalankan quick scan', 'scan virus sekarang', 'jalankan full scan'."""
    if not IS_WINDOWS:
        return security_linux.defender_scan(kind)
    full = kind.lower().startswith(("full", "penuh", "lengkap", "menyeluruh"))
    label = "Full scan Defender" if full else "Quick scan Defender"
    threading.Thread(target=_defender, args=(["-Scan", "-ScanType", "2" if full else "1"], label, 6 * 3600),
                     daemon=True, name="defender-scan").start()
    return (f"{label} saya jalankan di latar belakang, {T}. "
            + ("Ini bisa lebih dari satu jam; komputer tetap bisa dipakai." if full else "Biasanya beberapa menit.")
            + " Saya kabari hasilnya.")


def scan_file(path: str = "") -> str:
    """Scan one file with Windows Defender ('scan file itu', 'cek file installer tadi'). Empty path = the last
    download Digidaw warned about."""
    path = path.strip().strip('"') or _state.get("flagged_file", "")
    if not path:
        return f"File mana yang ingin dipindai, {T}? Sebutkan nama atau lokasinya."
    if not os.path.exists(path):
        return f"File {Path(path).name} sudah tidak ada, {T}; mungkin sudah dihapus atau dikarantina Defender."
    if not IS_WINDOWS:
        return security_linux.scan_file(path)
    threading.Thread(target=_defender, args=(["-Scan", "-ScanType", "3", "-File", path], f"Scan file {Path(path).name}", 900),
                     daemon=True, name="defender-file").start()
    return f"Memindai {Path(path).name} dengan Defender, {T}."


# ───────────────────────────── background: auto scan + Downloads guard ─────────────────────────────

def _auto_scan_loop():
    time.sleep(300)                        # let Digidaw settle after start-up
    if not _load_state().get("last_scan"):
        _save_state({"last_scan": time.time(), "keys": []})   # baseline: first auto scan in N days
    while True:
        days = config.SECURITY_AUTO_SCAN_DAYS
        last = _load_state().get("last_scan") or 0
        if days and time.time() - last > days * 86400 and not _state["running"] and _idle_enough():
            _state["running"] = True
            _run_scan(auto=True)
        time.sleep(600)


def _idle_enough() -> bool:
    return psutil.cpu_percent(interval=1) < 60


def _download_origin(path: Path) -> str:
    try:
        with open(str(path) + ":Zone.Identifier", encoding="utf-8", errors="replace") as f:
            m = re.search(r"HostUrl=(\S+)", f.read())
        return re.sub(r"^https?://(www\.)?", "", m.group(1)).split("/")[0] if m else ""
    except OSError:
        return ""


def _assess_download(path: Path) -> tuple[str, str] | None:
    """(severity, message) for a new file in Downloads, or None when it's harmless."""
    if not IS_WINDOWS:
        return security_linux.assess_download(path)
    ext = path.suffix.lower()
    suffixes = [x.lower() for x in path.suffixes[-2:]]
    origin = _download_origin(path)
    src = f" dari {origin}" if origin else ""
    if len(suffixes) == 2 and suffixes[0] in DOC_EXT and ext in DANGEROUS_EXT:
        return "tinggi", (f"Awas, {T}: file baru {path.name}{src} menyamar sebagai dokumen padahal program. "
                          f"Jangan dibuka. Ucapkan 'hapus file itu' atau 'scan file itu'.")
    if ext not in RISKY_DOWNLOAD_EXT:
        return None
    if ext in (".exe", ".msi", ".scr", ".com", ".msix", ".appx"):
        sig = _signatures([str(path)]).get(str(path).lower(), "")
        if sig.startswith("Valid"):
            return "info", f"Installer {path.name}{src} ditandatangani {sig[6:].strip(' ()') or 'penerbit resmi'}."
        return "sedang", (f"{T}, ada program baru di Downloads: {path.name}{src}, tanpa tanda tangan digital. "
                          f"Ucapkan 'scan file itu' untuk memeriksanya dengan Defender sebelum dibuka.")
    if ext in (".iso", ".img", ".vhd", ".vhdx"):
        return "sedang", (f"File image disk baru di Downloads: {path.name}{src}. Format ini sering dipakai menyelundupkan "
                          f"malware; ucapkan 'scan file itu' sebelum membukanya, {T}.")
    if ext == ".apk":
        return "info", f"File APK Android baru: {path.name}{src}."
    return "sedang", (f"Ada skrip baru di Downloads: {path.name}{src}. Skrip bisa menjalankan perintah apa pun; "
                      f"jangan dijalankan kalau tidak tahu isinya, {T}.")


def _downloads_loop():
    folder = HOME / "Downloads"
    if not folder.exists():
        return
    def listing():
        found = {}
        for f in list(folder.glob("*")) + list(folder.glob("*/*")):
            try:
                if f.is_file():
                    found[str(f)] = f.stat().st_size
            except OSError:
                continue
        return found
    seen, pending = set(listing()), {}
    while True:
        time.sleep(4)
        if not config.DOWNLOAD_GUARD:
            continue
        try:
            now = listing()
        except OSError:
            continue
        for path, size in now.items():
            if path in seen or Path(path).suffix.lower() in PARTIAL_EXT:
                continue
            if pending.get(path) != size:          # still being written
                pending[path] = size
                continue
            pending.pop(path, None)
            seen.add(path)
            try:
                verdict = _assess_download(Path(path))
            except Exception:
                verdict = None
            if not verdict:
                continue
            severity, message = verdict
            events.emit("security", status="download", severity=severity, file=path, step=message)
            if severity == "info":
                events.emit("toast", text=message, level="info")
                continue
            _state["flagged_file"] = path
            if ANNOUNCE:
                ANNOUNCE(message, "warning")
        seen &= set(now) | set(pending)


def start_background():
    threading.Thread(target=_auto_scan_loop, daemon=True, name="security-auto").start()
    threading.Thread(target=_downloads_loop, daemon=True, name="downloads-guard").start()


if not IS_WINDOWS:   # what Gemini reads about the tools
    security_scan.__doc__ = security_scan.__doc__.replace(
        "antivirus & firewall state, threat history,", "firewall and pacman update state, kernel reboot, hosts file,")
    defender_scan.__doc__ = """Run a ClamAV virus scan in the background: kind='quick' (Downloads, Desktop, /tmp) or 'full'
    (the whole home folder, can take over an hour). 'jalankan quick scan', 'scan virus sekarang', 'jalankan full scan'."""
    scan_file.__doc__ = """Scan one file with ClamAV ('scan file itu', 'cek file installer tadi'). Empty path = the last
    download Digidaw warned about."""
    clean_orphan_startup.__doc__ = """Remove autostart entries / systemd user services that point to programs which no
    longer exist ('bersihkan startup yatim'). A backup of what is removed is saved first. Asks confirmation."""

AI_TOOLS = [security_scan, last_security_report, clean_orphan_startup, close_remote_access_apps, defender_scan, scan_file]
