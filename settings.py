"""
Control Panel settings: the schema the HUD renders, validation, saving to settings.json and
applying changes live. The Gemini API key is kept in .env instead (never in settings.json).
"""
import os
import re
import sys
import json
import shutil
import subprocess
from pathlib import Path

import config
import storage

YES_NO = None
# Indonesian voices. The "multibahasa" ones are Microsoft's newer, more natural voices (made for Copilot); they speak
# Indonesian correctly too (checked 31 Oct 2026 by transcribing samples), and English in the same voice. Andrew is left
# out: its server was so busy that it often took 5 s or timed out (measured median first audio: Ardi 1.2 s, Florian
# 0.9 s, William 1.5 s, Emma 1.6 s, Brian 1.7-2.5 s, Ava 2.8 s).
VOICES_ID = [("id-ID-ArdiNeural", "Ardi (pria, Indonesia)"), ("id-ID-GadisNeural", "Gadis (wanita, Indonesia)"),
             ("de-DE-FlorianMultilingualNeural", "Florian (pria, multibahasa, natural, cepat)"),
             ("en-AU-WilliamMultilingualNeural", "William (pria, multibahasa, natural)"),
             ("en-US-BrianMultilingualNeural", "Brian (pria, multibahasa, santai · sedikit lebih lambat)"),
             ("en-US-EmmaMultilingualNeural", "Emma (wanita, multibahasa, ceria)"),
             ("en-US-AvaMultilingualNeural", "Ava (wanita, multibahasa, ekspresif · lebih lambat)")]
VOICES_EN = [("en-GB-RyanNeural", "Ryan (Inggris, pria)"), ("en-GB-ThomasNeural", "Thomas (Inggris, pria)"),
             ("en-GB-SoniaNeural", "Sonia (Inggris, wanita)"), ("en-US-AndrewNeural", "Andrew (Amerika, pria)"),
             ("en-US-GuyNeural", "Guy (Amerika, pria)"), ("en-US-AriaNeural", "Aria (Amerika, wanita)")]

# (section, key, label, type, extras) — "restart": only takes effect after restarting Digidaw
FIELDS = [
    ("Umum", "USER_TITLE", "Sapaan untuk Anda", "text", {"help": "Contoh: Sir, Tuan, Bos, Kak.", "restart": True}),
    ("Umum", "DEFAULT_CITY", "Kota untuk cuaca", "text", {"help": "Kosongkan untuk mendeteksi otomatis dari internet."}),
    ("Umum", "START_WITH_WINDOWS", "Jalankan otomatis saat " + ("Windows menyala" if os.name == "nt" else "login"), "bool",
     {"help": "Digidaw mulai diam-diam di latar belakang; panggil namanya atau tekan hotkey HUD."}),
    ("Umum", "STARTUP_BRIEFING", "Ringkasan harian saat pertama memanggil \"Digidaw\"", "bool",
     {"help": "Saat Digidaw pertama kali menyala hari itu (juga saat ikut menyala bersama Windows): tanggal, cuaca, "
              "pengingat, dan catatan keamanan."}),
    ("Alarm", "ALARM_SOUND", "Suara alarm", "alarm_sound",
     {"help": "Tambahkan audio Anda sendiri (lagu, nada dering, rekaman). Yang dipilih langsung dipakai."}),
    ("Alarm", "ALARM_SOUND_FOR_ALL", "Pakai juga untuk timer dan pengingat", "bool",
     {"help": "Mati = hanya alarm yang memakai suara pilihan; timer dan pengingat memakai bunyi pendek bawaan."}),
    ("Alarm", "ALARM_RISE_SECONDS", "Volume naik perlahan (detik, 0 = langsung penuh)", "int",
     {"min": 0, "max": 120, "help": "Alarm mulai pelan lalu naik sampai volume penuh, supaya tidak mengagetkan."}),
    ("Alarm", "ALARM_MIN_VOLUME", "Volume alarm paling kecil (%)", "int",
     {"min": 20, "max": 100, "help": "Alarm tetap terdengar walau volume laptop sedang kecil atau dibisukan."}),
    ("Alarm", "ALARM_RING_SECONDS", "Lama alarm berbunyi (detik)", "int",
     {"min": 10, "max": 600, "help": "Alarm berhenti sendiri setelah ini kalau tidak dimatikan / ditunda."}),
    ("Alarm", "ALARM_SNOOZE_MINUTES", "Tunda alarm (menit)", "int", {"min": 1, "max": 60}),
    ("Alarm", "ALARM_SHOW_HUD", "Tampilkan layar alarm saat berbunyi", "bool",
     {"help": "HUD terbuka sendiri dengan tombol berhenti dan tunda."}),
    ("Umum", "HUD_AUTO_OPEN", "Buka HUD saat Digidaw dijalankan", "bool", {}),

    ("Folder & Project", "PROJECT_DIR", "Folder project fokus", "folder",
     {"help": "Folder yang dipakai saat Anda bilang \"buka project\", \"cari file …\", atau \"buat folder …\"."}),
    ("Folder & Project", "PROJECT_EDITOR", "Buka project dengan", "choice",
     {"options": [("vscode", "Visual Studio Code"), ("explorer", "File Explorer"), ("terminal", "Terminal")]}),
    ("Folder & Project", "GENERATE_DIR", "Folder hasil generate (gambar & file)", "folder",
     {"help": "Kosong = Documents\\Digidaw Generated. Gambar masuk subfolder Gambar, dokumen ke subfolder File."}),
    ("Folder & Project", "GENERATE_OPEN_RESULT", "Buka hasil generate otomatis", "bool", {}),
    ("Folder & Project", "NOTES_DIR", "Folder catatan", "folder", {"help": "Kosong = Documents\\Digidaw Notes."}),
    ("Folder & Project", "SCREENSHOT_DIR", "Folder screenshot", "folder", {"help": "Kosong = Pictures\\Screenshots."}),

    ("Project Builder", "CODING_AGENT", "Agen pembuat project", "choice",
     {"options": [("antigravity", "Antigravity CLI (default)"), ("claude", "Claude Code")],
      "help": "Bisa juga dipilih per project dengan menyebutnya, mis. \"buat project … pakai Claude Code\"."}),
    ("Project Builder", "PROJECTS_ROOT", "Folder induk project baru", "folder",
     {"help": "Kosong = folder Projects di folder pengguna Anda. Setiap project mendapat subfolder sendiri."}),
    ("Project Builder", "AGENT_SKIP_PERMISSIONS", "Izinkan agen bekerja tanpa bertanya", "bool",
     {"help": "Agen boleh menginstall paket dan menjalankan perintah sendiri tanpa konfirmasi. Diperlukan agar project "
              "bisa dibuat otomatis; matikan jika Anda ingin agen berhenti setiap butuh izin (project bisa tidak selesai)."}),
    ("Project Builder", "BUILD_TIMEOUT_MINUTES", "Batas waktu pembuatan (menit)", "int", {"min": 5, "max": 120}),
    ("Keamanan & Sistem", "SECURITY_AUTO_SCAN_DAYS", "Scan keamanan otomatis tiap (hari, 0 = mati)", "int",
     {"min": 0, "max": 60, "help": "Digidaw hanya bersuara kalau ada temuan baru dibanding scan sebelumnya."}),
    ("Keamanan & Sistem", "DOWNLOAD_GUARD", "Peringatkan file berisiko di Downloads", "bool",
     {"help": "Program tanpa tanda tangan, skrip, file image disk, dan file berekstensi ganda (mis. foto.jpg.exe)."}),
    ("Project Builder", "BRIEF_ASK_QUESTIONS", "Tanya dulu kalau deskripsi kurang jelas", "bool", {}),
    ("Project Builder", "MAX_PARALLEL_BUILDS", "Project dikerjakan bersamaan (sisanya antre)", "int", {"min": 1, "max": 4}),
    ("Project Builder", "PROJECT_PREVIEW", "Tampilkan screenshot project di HUD", "bool", {}),
    ("Project Builder", "BRIEF_SILENCE_SECONDS", "Diam berapa detik = selesai menjelaskan", "int",
     {"min": 8, "max": 90, "help": "Saat menjelaskan project, Digidaw terus mendengarkan sampai Anda bilang \"selesai\" atau diam selama ini."}),
    ("Pendengaran", "ESC_TO_TALK", "Tombol Esc untuk bicara / batal", "choice",
     {"options": [("single", "Tekan Esc sekali"), ("double", "Tekan Esc dua kali cepat"), ("off", "Mati")],
      "help": "Hanya saat jendela HUD Digidaw aktif (di aplikasi lain Esc tetap normal). Esc: Digidaw langsung "
              "mendengarkan tanpa perlu dipanggil. Esc lagi: batal mendengarkan / hentikan bicaranya."}),
    ("Pendengaran", "STT_PRIVACY_MODE", "Mode privat (pengenalan di laptop dulu)", "bool",
     {"help": "Obrolan yang tidak memanggil \"Digidaw\" dikenali di laptop dan tidak pernah dikirim ke Google. "
              "Menambah ±0,4 detik saat Anda memanggil Digidaw.", "restart": True}),
    ("Pendengaran", "OFFLINE_STT", "Perintah suara tetap jalan tanpa internet", "bool",
     {"help": "Tanpa internet, perintah dikenali di laptop (kurang akurat dibanding Google, cukup untuk perintah dasar)."}),
    ("Pendengaran", "MEDIA_WORDS_WITHOUT_WAKE", "Kontrol musik tanpa menyebut \"Digidaw\"", "bool",
     {"help": "Saat musik diputar, cukup ucapkan \"pause\", \"stop\", atau \"next\"."}),
    ("Pendengaran", "LOG_OVERHEARD", "Catat obrolan yang diabaikan ke file log", "bool",
     {"help": "Mati = kalimat yang bukan untuk Digidaw (obrolan orang lain) tidak disimpan ke log."}),
    ("Pendengaran", "CONVERSATION_MODE", "Mode percakapan", "bool",
     {"help": "Setelah menjawab, Digidaw tetap mendengarkan beberapa detik sehingga Anda bisa langsung "
              "membalas tanpa menyebut \"Digidaw\"."}),
    ("Pendengaran", "FOLLOWUP_SECONDS", "Lama tetap mendengar setelah menjawab (detik)", "int", {"min": 3, "max": 60}),
    ("Pendengaran", "WAKE_ALIASES", "Kata panggilan alternatif", "list",
     {"help": "Cara Google sering salah menulis \"Digidaw\" (pisahkan dengan koma). Hanya dihitung di awal atau "
              "akhir kalimat. Tip: klik ucapan yang diabaikan di log HUD untuk menambahkannya."}),
    ("Pendengaran", "REQUIRE_WAKE_WORD", "Wajib memanggil \"Digidaw\" untuk memulai", "bool",
     {"help": "Matikan agar semua ucapan dianggap perintah (tidak disarankan saat musik diputar)."}),
    ("Pendengaran", "WAKE_LISTEN_SECONDS", "Waktu menunggu perintah setelah dipanggil (detik)", "int", {"min": 3, "max": 30}),
    ("Pendengaran", "STT_VAD_AGGRESSIVENESS", "Filter kebisingan", "choice",
     {"options": [(0, "Sangat peka"), (1, "Peka"), (2, "Seimbang (disarankan)"), (3, "Ketat (ruangan bising)")],
      "restart": True}),
    ("Pendengaran", "STT_END_SILENCE_MS", "Jeda akhir kalimat (milidetik)", "int",
     {"min": 300, "max": 1500, "help": "Lebih kecil = lebih cepat merespons; lebih besar = tidak memotong kalimat.",
      "restart": True}),
    ("Pendengaran", "PUSH_TO_TALK_HOTKEY", "Hotkey bicara / menyela", "text", {"restart": True}),
    ("Pendengaran", "SHOW_HUD_HOTKEY", "Hotkey menampilkan HUD", "text", {"restart": True}),
    ("Pendengaran", "ACK_SOUND", "Bunyi \"tik\" saat perintah dipahami", "bool", {}),
    ("Pendengaran", "BARGE_IN", "Bisa disela dengan suara saat Digidaw bicara", "bool",
     {"help": "Ucapkan \"cukup\", \"stop\", atau \"Digidaw, <perintah lain>\" dengan suara jelas saat Digidaw sedang bicara. Suara pendek yang "
              "jelas juga dianggap \"stop\". Suara Digidaw, aplikasi lain, dan mikrofon sehari-hari tidak diubah.",
      "restart": True}),

    ("Suara", "TTS_ENGINE", "Mesin suara", "choice",
     {"options": [("edge", "Edge Neural (paling natural)"), ("gtts", "Google TTS"), ("sapi", "Windows (offline)")]}),
    ("Suara", "EDGE_VOICE_ID", "Suara bahasa Indonesia", "choice", {"options": VOICES_ID}),
    ("Suara", "EDGE_VOICE_EN", "Suara bahasa Inggris", "choice", {"options": VOICES_EN}),
    ("Suara", "EDGE_RATE", "Kecepatan bicara", "choice",
     {"options": [("-10%", "Lambat"), ("+0%", "Normal"), ("+5%", "Sedikit cepat"), ("+12%", "Cepat"), ("+20%", "Sangat cepat")]}),
    ("Suara", "STREAM_REPLIES", "Ucapkan jawaban panjang sambil AI masih menulisnya", "bool",
     {"help": "Kalimat pertama sudah terdengar sebelum jawaban AI online selesai ditulis."}),
    ("Suara", "THINKING_FILLER_SECONDS", "Bilang \"Sebentar\" bila jawaban lebih lama dari (detik)", "int",
     {"min": 0, "max": 15, "help": "0 = tidak pernah."}),

    ("Media", "MEDIA_BROWSER", "Browser pemutar YouTube", "choice",
     {"options": [("auto", "Otomatis (Chrome/Edge/Brave)"), ("chrome", "Chrome"), ("edge", "Edge"), ("brave", "Brave"),
                 ("firefox", "Firefox (tidak disarankan: lagu bisa mengulang)")], "restart": True}),
    ("Media", "MEDIA_BRING_TO_FRONT", "Tampilkan jendela video saat memutar", "bool", {}),
    ("Media", "PAUSE_OTHER_MEDIA_ON_PLAY", "Pause musik lain saat memutar lagu baru", "bool", {}),
    ("Media", "YOUTUBE_AUTO_SKIP_ADS", "Tekan \"Skip Ad\" otomatis", "bool", {}),
    ("Media", "DUCK_MEDIA", "Kecilkan musik saat Digidaw bicara / mendengarkan", "bool", {}),
    ("Media", "DUCK_LEVEL", "Volume musik saat dikecilkan", "choice",
     {"options": [(0.15, "15%"), (0.3, "30%"), (0.5, "50%")]}),

    ("AI", "GEMINI_API_KEY", "Gemini API key", "secret",
     {"help": "Disimpan di file .env, tidak ditampilkan lagi. Boleh lebih dari satu, pisahkan dengan koma: kalau batas "
              "key pertama penuh, Digidaw otomatis memakai key berikutnya, lalu kembali ke yang pertama."}),
    ("AI", "ADDRESS_STYLE", "Gaya bicara Digidaw", "choice",
     {"options": [("anda", "Sopan santai (saya / Anda)"), ("kamu", "Akrab (aku / kamu)")]}),
    ("AI", "BRAIN_MODE", "Otak utama", "choice",
     {"options": [("hybrid", "Gabungan: lokal (Ollama) + Gemini untuk hal online"), ("gemini", "Selalu Gemini"),
                  ("local", "Utamakan lokal (Gemini hanya untuk internet)")]}),
    ("AI", "LOCAL_MODEL", "Model lokal (Ollama)", "text",
     {"help": "Nama model di `ollama list`, mis. qwen3:4b-instruct. Hindari model thinking (lambat)."}),
    ("AI", "MODEL_PREFERENCE", "Prioritas model", "choice",
     {"options": [("fast", "Cepat (urutan model tercepat)"), ("smart", "Lebih pintar (Flash penuh dulu)")]}),

    ("HUD di HP", "MOBILE_HUD", "Aktifkan HUD untuk HP (Wi-Fi rumah)", "bool",
     {"restart": True, "help": "Buka dari HP di Wi-Fi yang sama. Ucapkan \"alamat HUD HP\" untuk QR-nya."}),
    ("HUD di HP", "MOBILE_PIN", "PIN masuk HP", "text",
     {"help": "4-12 angka. Mengganti PIN mengeluarkan semua HP yang sudah masuk."}),
    ("HUD di HP", "MOBILE_SCREEN", "Izinkan HP melihat layar & mengendalikan mouse/keyboard", "bool",
     {"help": "Siapa pun yang tahu PIN di Wi-Fi rumah bisa melihat layar dan mengetik di PC. PC memberi tanda setiap "
              "kali dipakai. Matikan bila tidak perlu."}),
    ("Keamanan & Sistem", "CONFIRM_DANGEROUS_ACTIONS", "Minta konfirmasi untuk aksi berbahaya", "bool",
     {"help": "Shutdown, kosongkan Recycle Bin, jalankan perintah PowerShell, dll."}),
    ("Keamanan & Sistem", "PROACTIVE_ALERTS", "Peringatan otomatis (baterai, CPU, RAM, disk)", "bool", {}),
    ("Keamanan & Sistem", "FAN_SAFE_CELSIUS", "Kipas kembali otomatis kalau CPU sepanas ini (°C, 0 = mati)", "int",
     {"min": 0, "max": 95, "help": "Berlaku saat kipas diatur manual, karena mode manual tidak mengikuti suhu. Sesudah itu kipas tetap otomatis sampai diatur lagi."}),
    ("Keamanan & Sistem", "HEAT_ALERT_CELSIUS", "Peringatan CPU terlalu panas (°C, 0 = mati)", "int",
     {"min": 0, "max": 110, "help": "Diucapkan kalau suhu CPU bertahan setinggi ini lebih dari semenit."}),

    ("Asisten Proaktif", "ANNOUNCE_MESSAGES", "Beri tahu saat ada pesan masuk", "choice",
     {"options": [("personal", "Chat pribadi"), ("all", "Semua, termasuk grup"), ("off", "Mati")],
      "help": "Hanya nama pengirim yang diucapkan (\"ada pesan WhatsApp dari Rina, mau saya bacakan?\"); isinya hanya "
              "kalau Anda jawab \"ya\". Diam saat Do Not Disturb, saat layar penuh (game/film), atau saat aplikasi "
              "chat-nya sedang Anda buka."}),
    ("Asisten Proaktif", "AWAY_MINUTES", "Dianggap pergi setelah tidak ada aktivitas (menit, 0 = mati)", "int",
     {"min": 0, "max": 180, "help": "Tanpa keyboard, mouse, atau perintah selama ini: kabar kecil (pesan, gambar "
                                    "selesai, baterai penuh) disimpan dan diceritakan saat Anda kembali. Peringatan "
                                    "dan pengingat tetap diucapkan langsung."}),
    ("Asisten Proaktif", "WELCOME_BACK", "Sapa & ceritakan yang terlewat saat Anda kembali", "bool",
     {"help": "Pagi hari setelah semalaman pergi: langsung ringkasan hari ini (agenda, cuaca, pesan)."}),
    ("Asisten Proaktif", "WEEKLY_REMIND_MINUTES", "Ingatkan sebelum kuliah (menit, 0 = mati)", "int",
     {"min": 0, "max": 180, "help": "Dari jadwal di skrip pengingat_jadwal. Diucapkan dan dikirim ke HP, jam 06.00 sampai 22.00."}),
    ("Asisten Proaktif", "RAIN_ALERTS", "Peringatan sebelum hujan", "bool",
     {"help": "Prakiraan Open-Meteo untuk kota di Umum → Kota untuk cuaca. Paling banyak sekali per 3 jam, "
              "jam 06.00–21.00."}),
    ("Asisten Proaktif", "DAILY_RECAP_HOUR", "Rangkuman hari otomatis mulai jam (0 = hanya saat diminta)", "int",
     {"min": 0, "max": 23, "help": "Sekali sehari saat Anda di depan laptop: lama aktif & aplikasi yang dipakai, pesan "
                                  "masuk, dan agenda/alarm/cuaca besok. Bisa juga diminta: \"rangkuman hari ini\"."}),

    ("Rumah (Lampu)", "LAMP_AUTO", "Lampu otomatis mengikuti matahari & keberadaan Anda", "bool",
     {"help": "Menyala hangat menjelang matahari terbenam (bila Anda di depan laptop), menyala saat Anda kembali "
              "setelah gelap (sebelum tengah malam), dan meredup saat Anda pergi lama."}),
    ("Rumah (Lampu)", "LAMP_NIGHT_HOUR", "Lampu diredupkan mulai jam (0 = tidak)", "int", {"min": 0, "max": 23}),
    ("Rumah (Lampu)", "LAMP_NIGHT_BRIGHTNESS", "Kecerahan lampu malam (%)", "int", {"min": 1, "max": 100}),
    ("Rumah (Lampu)", "LAMP_AWAY_MINUTES", "Redupkan lampu bila pergi lebih dari (menit, 0 = tidak)", "int",
     {"min": 0, "max": 240}),
    ("Rumah (Lampu)", "LAMP_ALARM_LIGHT", "Lampu ikut alarm & pengingat", "bool",
     {"help": "Alarm: lampu menyala perlahan seperti matahari terbit. Timer/pengingat: lampu berkedip (kalau menyala)."}),
]
FIELD_MAP = {f[1]: f for f in FIELDS}


def schema() -> list[dict]:
    items = []
    for section, key, label, kind, extra in FIELDS:
        value = getattr(config, key, None)
        item = {"section": section, "key": key, "label": label, "type": kind,
                "help": extra.get("help", ""), "restart": extra.get("restart", False)}
        if kind == "secret":
            item["value"], item["has_value"] = "", bool(value)
        elif kind == "list":
            item["value"] = ", ".join(value or [])
        else:
            item["value"] = str(value) if kind == "folder" else value
        if "options" in extra:
            item["options"] = [{"value": v, "label": l} for v, l in extra["options"]]
        for bound in ("min", "max"):
            if bound in extra:
                item[bound] = extra[bound]
        items.append(item)
    return items


def _validate(key: str, value):
    _, _, label, kind, extra = FIELD_MAP[key]
    if key == "MOBILE_PIN":
        pin = str(value or "").strip()
        if pin and not re.fullmatch(r"\d{4,12}", pin):
            raise ValueError("PIN harus 4-12 angka")
        return pin
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return max(extra.get("min", -10**9), min(extra.get("max", 10**9), int(float(value))))
    if kind == "choice":
        for option, _ in extra["options"]:
            if str(option) == str(value):
                return option
        raise ValueError(f"Pilihan tidak valid untuk {label}")
    if kind == "list":
        items = value if isinstance(value, list) else str(value or "").split(",")
        clean = []
        for item in items:
            item = " ".join(str(item).lower().split())[:30]
            if item and item not in clean and item not in ("digidaw",):
                clean.append(item)
        return clean[:25]
    if kind == "alarm_sound":
        import alarm_sounds
        value = str(value or "")
        if value and not alarm_sounds.exists(value):
            raise ValueError(f"Suara alarm tidak ditemukan: {value}")
        return value
    if kind == "folder":
        value = str(value or "").strip()
        if value and not os.path.isdir(value):
            raise ValueError(f"Folder tidak ditemukan: {value}")
        return value
    value = str(value or "").strip()[:200]
    if key.endswith("HOTKEY") and value:
        import hotkeys
        try:
            hotkeys.parse(value)
        except (KeyError, TypeError, IndexError):
            raise ValueError(f"Hotkey tidak valid: {value} (contoh yang benar: ctrl+alt+d)")
    return value


def _saved() -> dict:
    data = storage.load_json(config.SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_api_key(key: str):
    env = config.ENV_FILE
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [l for l in lines if not l.strip().startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY="))]
    lines.append(f"GEMINI_API_KEY={key}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.GEMINI_API_KEY = key
    import llm
    llm.reset_keys()     # reconnect with the new key(s)


def save(values: dict) -> dict:
    """Validate, apply live and persist. Returns {"changed": [...], "restart": [labels]}."""
    stored = _saved()
    changed, restart = [], []
    for key, raw in values.items():
        if key not in FIELD_MAP:
            continue
        kind, label, extra = FIELD_MAP[key][3], FIELD_MAP[key][2], FIELD_MAP[key][4]
        if kind == "secret":
            if raw:
                _save_api_key(str(raw).strip())
                changed.append(key)
            continue
        value = _validate(key, raw)
        if value == getattr(config, key, None):
            continue
        setattr(config, key, value)
        stored[key] = value
        changed.append(key)
        if extra.get("restart"):
            restart.append(label)
    if "MOBILE_PIN" in changed:
        import mobile_server
        mobile_server.logout_all()          # a new PIN: every phone has to log in again
    if "MODEL_PREFERENCE" in changed:
        config.apply_model_preference()
    if "START_WITH_WINDOWS" in changed:
        set_autostart(config.START_WITH_WINDOWS)
    storage.save_json(config.SETTINGS_FILE, stored, backup=True, mode=storage.PRIVATE, indent=2)
    return {"changed": changed, "restart": restart}


# ───────────────────────────── helpers ─────────────────────────────

def _startup_shortcut() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Digidaw.lnk"


def make_shortcut(path: Path, arguments: str = ""):
    """Windows shortcut that starts Digidaw without a console window."""
    launcher = config.BASE_DIR / "start_digidaw.vbs"
    icon = config.BASE_DIR / "digidaw.ico"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); "
        "$s.TargetPath = \"$env:SystemRoot\\System32\\wscript.exe\"; "
        "$s.Arguments = '\"' + $env:VBS + '\" ' + $env:ARGS; "
        "$s.WorkingDirectory = $env:DIR; $s.IconLocation = $env:ICO; "
        "$s.Description = 'Digidaw - Personal AI Assistant'; $s.Save()"
    )
    env = dict(os.environ, LNK=str(path), VBS=str(launcher), ARGS=arguments,
               DIR=str(config.BASE_DIR), ICO=str(icon))
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, check=True,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


LINUX_AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "digidaw.desktop"


def linux_desktop_entry(arguments: str = "") -> str:
    """A .desktop entry that starts Digidaw without a terminal (app menu + XDG autostart, run by uwsm)."""
    launcher = config.BASE_DIR / "start_digidaw.sh"
    return ("[Desktop Entry]\nType=Application\nName=Digidaw\nComment=Digidaw - Personal AI Assistant\n"
            f"Exec=\"{launcher}\"{' ' + arguments if arguments else ''}\nPath={config.BASE_DIR}\n"
            f"Icon={config.BASE_DIR / 'digidaw.ico'}\nTerminal=false\nCategories=Utility;\n"
            "X-GNOME-Autostart-enabled=true\n")


LINUX_KEEP_APPS = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
                   / "app-digidaw@autostart.service.d" / "keep-apps.conf")
KEEP_APPS_CONF = """# Written by Digidaw. Apps Digidaw opens (Firefox, WhatsApp, terminals...) start inside Digidaw's own
# service; without this, stopping or restarting Digidaw would close all of them too. Only Digidaw itself is stopped.
[Service]
KillMode=process
# a crash (e.g. the audio driver aborting the process) brings Digidaw back; "Matikan" exits cleanly and stays off
Restart=on-failure
RestartSec=3
"""


def ensure_keep_apps():
    """Make sure stopping / restarting Digidaw (autostart unit from uwsm) never kills the apps it opened."""
    if os.name == "nt" or not LINUX_AUTOSTART.exists():
        return
    try:
        if LINUX_KEEP_APPS.exists() and LINUX_KEEP_APPS.read_text(encoding="utf-8") == KEEP_APPS_CONF:
            return
        LINUX_KEEP_APPS.parent.mkdir(parents=True, exist_ok=True)
        LINUX_KEEP_APPS.write_text(KEEP_APPS_CONF, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# Digidaw's own user service (3 Nov 2026): whoever starts it (the user's script, the app menu, autostart), systemd
# watches it — a crash or a hang (no heartbeat for WatchdogSec) brings it back. Only the service is restarted: the apps
# it opened stay open (KillMode=process).
LINUX_SERVICE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "digidaw.service"


def service_unit() -> str:
    return f"""# Written by Digidaw (settings.install_service). Start / stop: systemctl --user start|stop digidaw
[Unit]
Description=Digidaw - asisten suara
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={config.BASE_DIR / "start_digidaw.sh"} --background
# apps Digidaw opens (Firefox, WhatsApp, terminals…) live in this service too: stopping Digidaw must not close them
KillMode=process
# a crash, or a hang (main.py stops sending its heartbeat), brings Digidaw back; "Matikan" exits cleanly and stays off
Restart=on-failure
RestartSec=3
NotifyAccess=main
WatchdogSec=120
TimeoutStopSec=20

[Install]
WantedBy=graphical-session.target
"""


def _temporary_copy() -> bool:
    """Is this Digidaw a copy in a temporary folder? Only the real one may be what starts at login."""
    import tempfile
    here = Path(config.BASE_DIR).resolve()
    return any(here.is_relative_to(Path(t).resolve()) for t in {tempfile.gettempdir(), "/tmp", "/var/tmp"})


def install_service() -> bool:
    """Write (or update) the user service. True when it is in place."""
    if os.name == "nt" or not shutil.which("systemctl"):
        return False
    if _temporary_copy():
        return False                     # (a working copy under /tmp pointed the real service at itself; gone after a reboot, Digidaw couldn't start: 6 Nov)
    try:
        text = service_unit()
        if not (LINUX_SERVICE.exists() and LINUX_SERVICE.read_text(encoding="utf-8") == text):
            LINUX_SERVICE.parent.mkdir(parents=True, exist_ok=True)
            LINUX_SERVICE.write_text(text, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def set_autostart(enabled: bool):
    if os.name != "nt":
        # at login through the service (supervised), never also through the desktop autostart (a second copy)
        LINUX_AUTOSTART.unlink(missing_ok=True)
        if install_service():
            subprocess.run(["systemctl", "--user", "enable" if enabled else "disable", "digidaw.service"],
                           capture_output=True, timeout=10)
        elif enabled:
            LINUX_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            LINUX_AUTOSTART.write_text(linux_desktop_entry("--background"), encoding="utf-8")
            ensure_keep_apps()
        return
    link = _startup_shortcut()
    if enabled:
        make_shortcut(link, "--background")
    else:
        link.unlink(missing_ok=True)


def pick_folder(initial: str = "") -> str:
    """Native folder picker (runs in its own process so it can't disturb Digidaw's threads)."""
    import shutil
    if os.name != "nt" and shutil.which("zenity"):
        result = subprocess.run(["zenity", "--file-selection", "--directory", "--title=Pilih folder",
                                 f"--filename={(initial or str(Path.home())).rstrip('/')}/"],
                                capture_output=True, text=True, timeout=600)
        return os.path.normpath(result.stdout.strip()) if result.stdout.strip() else ""
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory(initialdir=sys.argv[1] or None, title='Pilih folder') or '')\n"
    )
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    result = subprocess.run([str(pythonw if pythonw.exists() else exe), "-c", code, initial or ""],
                            capture_output=True, text=True, timeout=600)
    return os.path.normpath(result.stdout.strip()) if result.stdout.strip() else ""


def alias_sounds_like_name(alias: str) -> bool:
    """Does the word sound anything like "Digidaw" ("digital" 0.71, "di bidang" 0.67)? Ordinary words ("gak" 0.2,
    "saya", "volume") would make Digidaw wake up all day."""
    import difflib
    key = re.sub(r"[^a-z]", "", alias.lower())
    return len(key) >= 3 and difflib.SequenceMatcher(None, key, "digidaw").ratio() >= 0.5


def add_wake_alias(alias: str) -> list[str]:
    """Add one mis-heard form of the name (from the HUD log) to the wake aliases."""
    if not alias_sounds_like_name(alias):
        raise ValueError(f'"{alias}" tidak mirip bunyi "Digidaw"; kalau dijadikan panggilan, Digidaw akan sering salah bangun')
    aliases = list(config.WAKE_ALIASES) + [alias]
    save({"WAKE_ALIASES": aliases})
    return config.WAKE_ALIASES
