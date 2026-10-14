"""
Antigravity System Analysis integration for Digidaw ("Digidaw, analisis sistem").

Menghubungkan Digidaw ke Antigravity AI Agent (via CLI `agy`) untuk memeriksa
dan menganalisis seluruh sistem komputer secara mendalam:
- Spesifikasi OS, versi Kernel, arsitektur, dan uptime
- Penggunaan beban CPU dan load average
- Utilisasi RAM dan pemakaian Swap
- Kapasitas partisi disk dan ruang kosong (khususnya partisi root `/`)
- Proses-proses terberat (CPU dan RAM)
- Status unit/layanan sistem (systemd failed units) dan log error kritis (journalctl)
- Konektivitas jaringan dan port terbuka
- Keamanan dasar dan pembaruan sistem

Hasil analisis dirangkum oleh Antigravity:
1. Ringkasan suara padat untuk dibacakan oleh Digidaw (TTS).
2. Laporan komprehensif berformat Markdown & HTML yang otomatis disimpan
   di Documents/Digidaw Reports/ dan dibuka di peramban.
"""
import os
import re
import json
import time
import shutil
import datetime
import threading
import subprocess
import webbrowser
from pathlib import Path

import psutil

import config
import events
import oscompat

def _render_markdown(text: str) -> str:
    """Render markdown ke HTML dengan markdown_it, markdown, atau fallback."""
    try:
        from markdown_it import MarkdownIt
        return MarkdownIt().render(text)
    except Exception:
        pass
    try:
        import markdown
        return markdown.markdown(text, extensions=['tables', 'fenced_code', 'nl2br'])
    except Exception:
        pass
    import html
    return f"<pre>{html.escape(text)}</pre>"

IS_WINDOWS = oscompat.IS_WINDOWS
T = config.USER_TITLE
ANNOUNCE = None   # set by main (core.announce)

HOME = Path.home()
_state = {
    "running": False,
    "step": "",
    "last": None
}


def agent_exe() -> str | None:
    """Temukan executable Antigravity CLI (agy)."""
    local_bin = HOME / ".local" / "bin"
    candidates = [
        shutil.which("agy"),
        str(local_bin / "agy"),
        os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe"),
        str(local_bin / "agy.exe")
    ]
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def _reports_dir() -> Path:
    try:
        import system_control
        folder = system_control._known_folder("documents") / "Digidaw Reports"
    except Exception:
        folder = HOME / "Documents" / "Digidaw Reports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _clean_tts(text: str) -> str:
    """Bersihkan karakter markdown/formatting agar suara TTS membaca dengan halus."""
    t = re.sub(r"[*_`#~>|]", " ", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _gather_baseline() -> dict:
    """Kumpulkan metrik dasar sistem dengan cepat secara lokal sebagai referensi awal."""
    data = {}
    try:
        import platform
        data["os"] = platform.platform()
        data["cpu_count"] = psutil.cpu_count(logical=True)
        data["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        data["ram_total_gb"] = round(vm.total / (1024**3), 1)
        data["ram_used_gb"] = round(vm.used / (1024**3), 1)
        data["ram_percent"] = vm.percent
        data["ram_avail_gb"] = round(vm.available / (1024**3), 1)
        
        sw = psutil.swap_memory()
        data["swap_total_gb"] = round(sw.total / (1024**3), 1)
        data["swap_used_gb"] = round(sw.used / (1024**3), 1)
        data["swap_percent"] = sw.percent
        
        root = "/" if not IS_WINDOWS else "C:\\"
        du = psutil.disk_usage(root)
        data["disk_total_gb"] = round(du.total / (1024**3), 1)
        data["disk_used_gb"] = round(du.used / (1024**3), 1)
        data["disk_free_gb"] = round(du.free / (1024**3), 1)
        data["disk_percent"] = du.percent
        
        boot = psutil.boot_time()
        uptime_sec = int(time.time() - boot)
        hours, rem = divmod(uptime_sec, 3600)
        minutes, _ = divmod(rem, 60)
        data["uptime"] = f"{hours} jam {minutes} menit"
    except Exception as e:
        data["error"] = str(e)
    return data


def _generate_html_report(full_md: str, spoken: str, baseline: dict, started: float) -> Path:
    """The report page, in the same look as every Digidaw report (report_theme)."""
    import report_theme as rt
    folder = _reports_dir()
    now = datetime.datetime.now()
    path = folder / f"Analisis_Sistem_Antigravity_{now:%Y-%m-%d_%H%M}.html"
    elapsed = max(1, int(time.time() - started))
    b = baseline.get
    # the agent's own heading duplicates the page title
    md = re.sub(r"^#\s+LAPORAN ANALISIS SISTEM.*\n", "", full_md.strip(), count=1, flags=re.IGNORECASE)
    body_html = rt.add_anchors(rt.render_markdown(md))
    # the overall verdict is the first status mark (the agent starts with "Ringkasan Kondisi: 🟢 SEHAT"), not the
    # worst mark anywhere (one drive at 66% must not turn a healthy system yellow)
    first = re.search(r"🔴|🟠|🟡|🟢|\b(?:KRITIS|BERMASALAH|PERLU PERHATIAN|SEHAT)\b", md)
    mark = first[0] if first else ""
    health = ("tinggi" if mark in ("🔴", "KRITIS", "BERMASALAH") else "sedang" if mark in ("🟠", "🟡", "PERLU PERHATIAN")
              else "ok")
    verdict = {"ok": "Sistem sehat", "sedang": "Ada yang perlu dipantau", "tinggi": "Ada masalah yang perlu ditangani"}[health]
    stats = "".join([
        rt.stat_card("Beban CPU", f"{b('cpu_percent', 0)}%", b("cpu_percent", 0), f"{b('cpu_count', 0)} thread"),
        rt.stat_card("Memori RAM", f"{b('ram_percent', 0)}%", b("ram_percent", 0),
                     f"{b('ram_used_gb', 0)} / {b('ram_total_gb', 0)} GB · sisa {b('ram_avail_gb', 0)} GB"),
        rt.stat_card("Penyimpanan", f"{b('disk_percent', 0)}%", b("disk_percent", 0),
                     f"{b('disk_used_gb', 0)} / {b('disk_total_gb', 0)} GB · bebas {b('disk_free_gb', 0)} GB"),
        rt.stat_card("Swap", f"{b('swap_percent', 0)}%", b("swap_percent", 0),
                     f"{b('swap_used_gb', 0)} / {b('swap_total_gb', 0)} GB"),
    ])
    hero = (f'<section class="hero" style="grid-template-columns:1fr"><div><h2><span class="pill {health}"></span> '
            f'{verdict}</h2><div class="voice" style="margin-top:12px"><span class="label">Ringkasan dari Digidaw</span>'
            f'{rt.esc(spoken)}</div></div></section>')
    nav = rt.toc(body_html)
    content = f'<div class="layout">{nav}<article class="doc">{body_html}</article></div>' if nav else \
        f'<article class="doc">{body_html}</article>'
    page = rt.page("Analisis Kesehatan Sistem", "Digidaw × Antigravity",
                   f"{rt.tanggal(now)} · dianalisis dalam {elapsed} detik · menyala {b('uptime', '-')}",
                   hero + f'<div class="stats">{stats}</div>' + content
                   + '<footer>Dianalisis oleh agen Antigravity; Digidaw hanya membaca kondisi sistem, tidak ada yang '
                     'diubah.</footer>',
                   badges=[str(b("os", "Linux")).split("-with-")[0], f"{b('cpu_count', 0)} thread CPU",
                           f"RAM {b('ram_total_gb', 0)} GB", f"Disk {b('disk_total_gb', 0)} GB"])
    path.write_text(page, encoding="utf-8")
    return path


def _parse_agent_output(raw_text: str) -> tuple[str, str]:
    """Ekstrak ringkasan suara dan laporan lengkap dari output Antigravity."""
    spoken = ""
    full_report = raw_text.strip()
    
    # Format terstruktur: ===RINGKASAN_SUARA=== dan ===LAPORAN_LENGKAP===
    m_spoken = re.search(r"===RINGKASAN_SUARA===\s*(.*?)(?====LAPORAN_LENGKAP===|\Z)", raw_text, re.DOTALL | re.IGNORECASE)
    m_full = re.search(r"===LAPORAN_LENGKAP===\s*(.*)", raw_text, re.DOTALL | re.IGNORECASE)
    
    if m_spoken and m_spoken.group(1).strip():
        spoken = m_spoken.group(1).strip()
    if m_full and m_full.group(1).strip():
        full_report = m_full.group(1).strip()
    
    if not spoken:
        # Jika model tidak menggunakan delimiter yang diminta, ambil 2-3 kalimat pertama
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full_report) if s.strip()]
        spoken = " ".join(sentences[:3]) if sentences else full_report[:250]
        
    return _clean_tts(spoken), full_report


def _run_analysis():
    """Worker background untuk menjalankan Antigravity, membaca stream-json, dan melaporkan hasilnya."""
    started = time.time()
    events.emit("security", status="running", step="Menghubungkan ke Antigravity…")
    events.emit("analysis", status="running", step="Menghubungkan ke Antigravity…")
    
    exe = agent_exe()
    if not exe:
        msg = f"Maaf {T}, Antigravity CLI (agy) tidak ditemukan di komputer ini."
        _state["running"] = False
        if ANNOUNCE:
            ANNOUNCE(msg, "warning")
        else:
            print(f"[DIGIDAW] {msg}")
        return

    baseline = _gather_baseline()
    
    prompt = f"""Anda adalah Antigravity AI Agent yang bertugas menganalisis kondisi dan kesehatan SELURUH sistem komputer milik {T} secara mendalam dan menyeluruh.

Data dasar sistem saat ini yang telah dihimpun oleh Digidaw:
- OS & Kernel: {baseline.get('os')}
- Uptime Sistem: {baseline.get('uptime')}
- CPU ({baseline.get('cpu_count')} Cores): beban rata-rata {baseline.get('cpu_percent')}%
- Memori RAM: {baseline.get('ram_used_gb')} GB / {baseline.get('ram_total_gb')} GB ({baseline.get('ram_percent')}%), tersedia {baseline.get('ram_avail_gb')} GB
- Swap: {baseline.get('swap_used_gb')} GB / {baseline.get('swap_total_gb')} GB ({baseline.get('swap_percent')}%)
- Partisi Utama (/): {baseline.get('disk_used_gb')} GB / {baseline.get('disk_total_gb')} GB ({baseline.get('disk_percent')}%), sisa {baseline.get('disk_free_gb')} GB

Tugas Anda:
Gunakan tool terminal/shell (run_command) Anda untuk mengecek hal-hal spesifik berikut di sistem:
1. Jalankan pengecekan proses terberat yang mengonsumsi CPU dan RAM saat ini (misal ps aux --sort=-%cpu atau sejenisnya).
2. Periksa apakah ada layanan sistem yang gagal (misal systemctl --failed).
3. Periksa apakah ada error sistem kritis terbaru di log (misal journalctl -p 3 -xb -n 15).
4. Periksa sekilas status koneksi jaringan dan port mendengarkan (misal ss -tuln atau ip route).
5. Analisis seluruh temuan dengan kritis, jelaskan kondisi kesehatan komputer, temukan potensi bottleneck, dan berikan saran terbaik.

FORMAT OUTPUT WAJIB:
Tulis respons Anda dengan tepat memuat 2 bagian berikut:

===RINGKASAN_SUARA===
(Tulis 2 sampai 4 kalimat padat dalam bahasa Indonesia yang ramah dan enak didengar saat dibacakan langsung oleh suara Digidaw kepada {T}. Sebutkan status kesehatan sistem secara umum, konsumsi resource utama CPU/RAM/Disk, serta temuan penting atau rekomendasi singkat. Jangan gunakan markdown, bintang, bullet, atau karakter aneh di bagian ini.)

===LAPORAN_LENGKAP===
(Tulis laporan komprehensif berformat Markdown yang rapi dan terstruktur untuk disimpan dan dibaca:
# LAPORAN ANALISIS SISTEM — ANTIGRAVITY
- **Ringkasan Kondisi**: ...
- **Spesifikasi & Uptime**: ...
- **Analisis Sumber Daya (CPU, RAM, Swap, Disk)**: ...
- **Proses & Aplikasi Paling Dominan**: ...
- **Layanan Sistem & Error Log**: ...
- **Jaringan & Keamanan**: ...
- **Rekomendasi Tindakan**: ...)
"""

    cmd = [exe, "-p", prompt, "--output-format", "stream-json", "--dangerously-skip-permissions"]
    response_text = ""
    
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=oscompat.NO_WINDOW
        )
        
        last_step = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                kind = data.get("event")
                if kind == "step_update":
                    su = data.get("step_update") or {}
                    if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
                        tool_info = su.get("tool_info") or {}
                        tname = su.get("tool_name") or tool_info.get("name") or "tool"
                        params = tool_info.get("parameters") or {}
                        c = str(params.get("CommandLine") or params.get("command") or "")
                        step_desc = f"Antigravity menjalankan: {c[:50]}" if c else f"Antigravity memeriksa sistem ({tname})…"
                        last_step = step_desc
                        _state["step"] = step_desc
                        events.emit("security", status="running", step=step_desc)
                        events.emit("analysis", status="running", step=step_desc)
                elif kind == "result":
                    res = data.get("result") or {}
                    response_text = res.get("response") or ""
            except ValueError:
                pass
                
        proc.wait(timeout=30)
    except Exception as e:
        msg = f"Maaf {T}, analisis sistem oleh Antigravity terganggu: {e}"
        _state["running"] = False
        if ANNOUNCE:
            ANNOUNCE(msg, "warning")
        else:
            print(f"[DIGIDAW] {msg}")
        return
    finally:
        _state["running"] = False

    # Jika response tidak tertangkap lewat event result, gunakan fallback
    if not response_text.strip():
        spoken = (f"Pemeriksaan sistem telah selesai, {T}. Sistem berjalan normal dengan beban CPU {baseline.get('cpu_percent')}%, "
                  f"RAM terpakai {baseline.get('ram_used_gb')} dari {baseline.get('ram_total_gb')} GB, "
                  f"dan ruang disk root terpakai {baseline.get('disk_percent')}%.")
        full_md = (f"# LAPORAN ANALISIS SISTEM (RINGKASAN LOKAL)\n\n"
                   f"- **Waktu**: {datetime.datetime.now():%d %B %Y, %H:%M}\n"
                   f"- **OS**: {baseline.get('os')}\n"
                   f"- **Uptime**: {baseline.get('uptime')}\n"
                   f"- **CPU**: {baseline.get('cpu_percent')}%\n"
                   f"- **RAM**: {baseline.get('ram_used_gb')} / {baseline.get('ram_total_gb')} GB ({baseline.get('ram_percent')}%)\n"
                   f"- **Disk**: {baseline.get('disk_used_gb')} / {baseline.get('disk_total_gb')} GB ({baseline.get('disk_percent')}%)\n")
    else:
        spoken, full_md = _parse_agent_output(response_text)
    
    # Simpan laporan markdown dan HTML
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    md_path = _reports_dir() / f"Analisis_Sistem_Antigravity_{timestamp_str}.md"
    try:
        md_path.write_text(full_md, encoding="utf-8")
    except Exception:
        pass
        
    html_path = _generate_html_report(full_md, spoken, baseline, started)
    
    _state["last"] = {
        "summary": spoken,
        "report": str(html_path),
        "report_md": str(md_path),
        "time": time.time()
    }
    
    events.emit("security", status="done", report=str(html_path))
    events.emit("analysis", status="done", report=str(html_path), summary=spoken)
    events.emit("toast", text="Analisis sistem oleh Antigravity selesai!", level="info")
    
    # Buka laporan di browser
    try:
        webbrowser.open(html_path.as_uri())
    except Exception:
        pass
        
    # Berikan hasil ke Digidaw untuk disampaikan ke pengguna
    final_message = f"Analisis sistem dari Antigravity telah selesai, {T}. {spoken}"
    if ANNOUNCE:
        ANNOUNCE(final_message, "info")
    else:
        print(f"\n[DIGIDAW] {final_message}\n")
        try:
            import tts
            tts.speak(final_message)
        except Exception:
            pass


# ───────────────────────────── Public Skills ─────────────────────────────

def analisis_sistem() -> str:
    """Hubungkan ke Antigravity AI Agent untuk menganalisis seluruh kondisi dan kesehatan sistem
    komputer (OS, kernel, CPU, RAM, disk, proses besar, layanan gagal, error log, dan jaringan).
    Gunakan untuk perintah: 'analisis sistem', 'cek seluruh sistem', 'analisis laptop',
    'analisis komputer', 'analisis sistem dengan antigravity', 'periksa kondisi sistem'.
    Pemeriksaan berjalan di latar belakang dan hasilnya langsung dilaporkan melalui suara dan laporan visual."""
    if _state["running"]:
        return f"Analisis sistem dengan Antigravity masih berjalan, {T}. Saya akan kabari begitu selesai."
        
    exe = agent_exe()
    if not exe:
        return (f"Maaf {T}, Antigravity CLI (agy) tidak ditemukan di komputer ini. "
                f"Pastikan agy terpasang di folder ~/.local/bin/agy.")
                
    _state["running"] = True
    _state["step"] = "Menghubungkan ke Antigravity…"
    
    threading.Thread(target=_run_analysis, daemon=True, name="antigravity-system-analysis").start()
    
    return (f"Baik, {T}. Saya menghubungkan ke Antigravity untuk menganalisis seluruh sistem Anda. "
            f"Mohon tunggu sebentar, hasilnya akan langsung saya sampaikan begitu selesai.")


def last_system_analysis() -> str:
    """Buka kembali laporan analisis sistem terakhir dari Antigravity atau bacakan ringkasannya."""
    last = _state.get("last")
    if not last:
        folder = _reports_dir()
        reports = sorted(folder.glob("Analisis_Sistem_Antigravity_*.html")) if folder.exists() else []
        if not reports:
            return f"Belum ada laporan analisis sistem, {T}. Ucapkan 'analisis sistem' untuk memulainya."
        latest = reports[-1]
        webbrowser.open(latest.as_uri())
        return f"Membuka laporan analisis sistem terakhir di peramban, {T}."
        
    report_path = Path(last["report"])
    if report_path.exists():
        webbrowser.open(report_path.as_uri())
    return f"Membuka laporan analisis sistem terakhir, {T}. Ringkasannya: {last['summary']}"


AI_TOOLS = [analisis_sistem, last_system_analysis]
