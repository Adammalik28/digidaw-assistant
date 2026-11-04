"""
Digidaw's fast routes: sentences it understands by itself, instantly, without asking an AI — grouped by what they
are about. The groups are tried in ORDER and, inside a group, from top to bottom: the first pattern that matches the
whole (normalized) sentence and whose handler returns an action wins, so the specific ones come before the general
ones (the WINDOWS group, "buka …" / "tutup …" / "cari …", is last on purpose).

Each route is (pattern, handler(match) -> (tool, kwargs) | ("_ask", {...}) | None); None = "not me after all, try
the next one". Changing anything here: tests/test_router.py and tests/test_golden.py (261 real commands) tell what
else changed. Imported by brain.py at its end (it uses brain's helpers).
"""
import re
import time

import smarthome
import system_control as sc
from brain import (  # noqa: E402  (brain imports this module last, when all of these exist)
    FORECAST_WHEN,
    WORKSPACE,
    WS_NUM,
    _ask_close,
    _close_known,
    _flagged_delete,
    _focus_known,
    _forecast_when,
    _num,
    _open_in_browser,
    _open_known,
    _play,
    _read_offered,
    _repeat_last,
    _resolved,
    _search_route,
    _ws,
    _ws_route,
)

# a lamp named in a command ("lampu", "lampu kamar"), not the words after it ("lampu ke merah", "lampu lebih terang")
_LAMP = (r"(lampu(?:nya)?(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|mode\b|suasana\b|nuansa\b|"
         r"untuk\b|buat\b|lebih\b|agak\b|sedikit\b|dikit\b|kurang\b|\d)\w+)?)")
_SCENE = (r"(baca|membaca|kerja|bekerja|fokus|belajar|konsentrasi|santai|rileks|relax|chill|malam|tidur|film|nonton|"
          r"menonton|bioskop|romantis|pesta|party|disko|warna warni|pelangi)")


# "cari …" with a site: YouTube search or just the site
SITE_SEARCH = [
    # ── "cari ..." with a site: search on YouTube, or just open the site ("cari youtube" cut off by the mic) ──
    (r"(?:cari|carikan|search)(?: di)? youtube (?!di$)(.+)|(?:cari|carikan|search) (.+) di youtube",
     lambda m: ("search_youtube", {"query": m[1] or m[2]})),
    (r"(?:cari|carikan|search)(?: di)? ([\w ]+?)(?: di| dong)?",
     lambda m: ("open_website", {"site": m[1]}) if m[1] in sc.WEBSITES else None),
    (r"(?:hubungkan|sambungkan|koneksikan|sinkronkan|sinkron|sync)(?: agenda| jadwal)?(?: (?:ke|dengan))? (?:google calendar|google kalender|kalender google|google)",
     lambda m: ("connect_google_calendar", {})),
    (r"(?:status|cek) (?:sinkron )?(?:google calendar|kalender google|google kalender)", lambda m: ("google_calendar_status", {})),
    (r"(?:putuskan|hentikan|matikan) (?:sinkron )?(?:google calendar|kalender google|google kalender)",
     lambda m: ("disconnect_google_calendar", {})),
    (r"(?:alamat|qr|kode qr|link|buka|tampilkan|cara buka) (?:hud|digidaw|panel)(?: digidaw)? (?:di |untuk |lewat |dari )?(?:hp|handphone|ponsel|hape)",
     lambda m: ("mobile_hud_info", {})),
    (r"(?:nonton|tonton|menonton|lihat video|tampilkan video)(?: video)? (?!apa\b)(.{3,}?)(?: di youtube)?",
     lambda m: _play(m[1])),
    (r"(?:skip|lewati|lewatin|lompati) (?:iklan|ads|ad)(?:nya)?(?: youtube)?", lambda m: ("media_control", {"action": "skip_ad"})),
]


# Digidaw itself: is the AI working, the HUD window
ASSISTANT = [
    # ── is the AI working? (must work without the AI) ──
    (r"(?:cek|tes|test|periksa|status)(?: koneksi)? (?:ai|a i|gemini|otak(?: ai)?)(?: (?:saya|digidaw|nya))?|"
     r"(?:apakah|kenapa) ai(?: ?nya)? (?:berfungsi|jalan|normal|tidak (?:jalan|berfungsi|bisa)|error)",
     lambda m: ("ai_status", {})),
    # ── the HUD window ──
    (r"(?:buka|tampilkan|munculkan|perlihatkan|lihat|open|show|keluarkan)(?: lagi)? (?:hud|h u d|hut|had|ho|hood|hot|"
     r"hut|hat|dashboard|"
     r"tampilan(?: digidaw)?|panel digidaw|aplikasi digidaw|jendela digidaw|layar digidaw|digidaw)(?: nya| digidaw)?",
     lambda m: ("show_hud", {})),
    (r"(?:tutup|sembunyikan|minimize|minimalkan|kecilkan) (?:hud|h u d|hut|dashboard|tampilan digidaw|jendela digidaw)(?:nya)?",
     lambda m: ("hide_hud", {})),
]


# built projects, generated images / files, project versions
PROJECTS = [
    # ── open a built project by name ──
    (r"(?:buka|jalankan|lihat|tampilkan) (?:project|proyek) (?!saya$|ku$)(.+)",
     lambda m: ("open_last_project", {"project": "" if re.fullmatch(r"(?:yang )?(?:terakhir|terbaru)(?: (?:di)?buat)?", m[1]) else m[1]})),
    # ── generate images / files with Antigravity ──
    (r"(?:buat|buatkan|bikin|bikinkan|generate|gambarkan|lukiskan|desain|desainkan)(?: saya| aku| kan)?(?: sebuah| satu| (\d|dua|tiga|empat))?"
     r" (?:gambar|foto|ilustrasi|logo|poster|wallpaper|ikon|icon|banner|lukisan|karikatur|stiker|sticker|thumbnail|avatar|"
     r"desain gambar|image)\b.*", lambda m: ("generate_image", {"description": m[0], "count": _num(m[1]) or 1})),
    (r"(?:buat|buatkan|bikin|bikinkan|generate|susun|susunkan|tulis|tuliskan)(?: saya| aku| kan)?(?: sebuah| satu)?"
     r" (?:file|dokumen|document|word|excel|spreadsheet|tabel excel|presentasi|powerpoint|ppt|slide|pdf|laporan|surat|makalah|"
     r"proposal|cv|resume|daftar riwayat hidup|invoice|faktur|template|rpp|esai|essay|artikel|modul|kuesioner)\b.*",
     lambda m: ("create_file", {"description": m[0]})),
    (r"(?:buka )?(?:hasil generate|gambar (?:tadi|terakhir|yang tadi)|file (?:yang )?(?:tadi|terakhir) (?:dibuat|di-?generate))",
     lambda m: ("open_generated", {"which": "terakhir"})),
    (r"buka folder (?:hasil )?(?:generate|gambar|hasil)", lambda m: ("open_generated", {"which": "folder"})),
    (r"(?:status|progres) (?:pembuatan )?(?:gambar|file|generate)", lambda m: ("generate_status", {})),
    (r"(?:batalkan|batal|hentikan) (?:pembuatan )?(?:gambar|file)(?: nya)?", lambda m: ("cancel_generate", {})),
    # ── projects: where, versions ──
    (r"(?:di ?mana|dimana) (?:lokasi |letak |folder )?(?:project|proyek)(?: saya| ku| yang (?:sudah )?dibuat)?"
     r"(?: (?:disimpan|berada|ada|tersimpan))?|(?:lokasi|letak) (?:folder )?(?:project|proyek)(?: saya| ku)?",
     lambda m: ("project_location", {})),
    (r"(?:kembalikan|pulihkan|rollback|undo) (?:project|proyek) (.+?)(?: ke versi (?:sebelumnya|lama|(\w+)))?",
     lambda m: ("restore_project_version", {"project": m[1], "version": _num(m[2])})),
    (r"(?:kembalikan|pulihkan|rollback) (?:project|proyek)?(?: ?nya)? ?ke versi (?:sebelumnya|lama|(\w+))",
     lambda m: ("restore_project_version", {"project": "", "version": _num(m[1])})),
    (r"(?:batalkan|undo) perubahan (?:terakhir|tadi)(?: (?:di|pada|ke) (?:project|proyek) (.+))?",
     lambda m: ("restore_project_version", {"project": m[1] or "", "version": 0})),
    (r"(?:riwayat|history|daftar) versi(?: (?:project|proyek))?(?: (.+))?|riwayat (?:project|proyek)(?: (.+))?",
     lambda m: ("project_history", {"project": m[1] or m[2] or ""})),
    (r"apa (?:saja )?yang (?:di)?ubah (?:di|pada|dari) (?:project|proyek)(?: (.+))?|"
     r"(?:perubahan|update) (?:terakhir )?(?:di |pada )?(?:project|proyek)(?: (.+))?",
     lambda m: ("project_changes", {"project": m[1] or m[2] or ""})),
]


# security follow-ups
SECURITY = [
    # ── security follow-ups ──
    (r"(?:bersihkan|hapus) (?:entri |program )?startup(?: (?:yang )?yatim)?|bersihkan entri yatim",
     lambda m: ("clean_orphan_startup", {})),
    (r"(?:tutup|matikan|hentikan) (?:semua )?(?:aplikasi |app )?(?:remote(?: access)?|teamviewer|anydesk|rustdesk|ultraviewer)",
     lambda m: ("close_remote_access_apps", {})),
    (r"(?:jalankan|lakukan|mulai) (?:quick scan|scan cepat)(?: defender)?|scan virus(?: sekarang)?|quick scan(?: defender)?",
     lambda m: ("defender_scan", {"kind": "quick"})),
    (r"(?:jalankan|lakukan|mulai) (?:full scan|scan penuh|scan menyeluruh)(?: defender)?|full scan(?: defender)?",
     lambda m: ("defender_scan", {"kind": "full"})),
    (r"(?:scan|pindai|cek|periksa) file (?:itu|tadi|terakhir|yang tadi|yang baru)", lambda m: ("scan_file", {})),
    (r"hapus file (?:itu|tadi|yang tadi)", lambda m: _flagged_delete()),
]


# sleep timer, browser navigation, the open document, what's on screen
SCREEN = [
    # ── sleep timer ──
    (r"(?:matikan|hentikan|stop|pause) (?:musik|lagu)(?:nya)? (?:dalam|setelah|sesudah) (\d+) (menit|jam)|sleep timer (\d+) (menit|jam)",
     lambda m: ("sleep_timer", {"minutes": int(m[1] or m[3]) * (60 if (m[2] or m[4]) == "jam" else 1), "target": "music"})),
    (r"(?:matikan|shutdown) (?:laptop|komputer|pc)(?:nya)? (?:dalam|setelah|sesudah) (\d+) (menit|jam)",
     lambda m: ("sleep_timer", {"minutes": int(m[1]) * (60 if m[2] == "jam" else 1), "target": "laptop"})),
    # ── browser / window navigation ──
    (r"(?:scroll|gulir|geser)(?: ke)? (?:bawah|turun)(?: (\d+) kali)?|(?:scroll|gulir)(?: aja)?(?: (?:instagram|halaman(?:nya)?|layar|terus))?",
     lambda m: ("navigate", {"action": "scroll_down", "times": int(m[1] or 1)})),
    (r"(?:scroll|gulir|geser)(?: ke)? (?:atas|naik)(?: (\d+) kali)?", lambda m: ("navigate", {"action": "scroll_up", "times": int(m[1] or 1)})),
    (r"(?:kembali|balik|back)(?: lagi)?(?: ke halaman sebelumnya)?|halaman sebelumnya", lambda m: ("navigate", {"action": "back"})),
    (r"(?:maju|forward)(?: ke halaman berikutnya)?|halaman berikutnya", lambda m: ("navigate", {"action": "forward"})),
    (r"(?:refresh|reload|muat ulang)(?: (?:halaman|page|tab))?(?: ini)?", lambda m: ("navigate", {"action": "refresh"})),
    (r"(?:buka )?tab baru", lambda m: ("navigate", {"action": "new_tab"})),
    (r"(?:pindah ke )?tab (?:berikutnya|selanjutnya|sebelah)", lambda m: ("navigate", {"action": "next_tab"})),
    (r"(?:pindah ke )?tab sebelumnya", lambda m: ("navigate", {"action": "previous_tab"})),
    (r"buka (?:lagi )?tab yang (?:tadi|baru) (?:di)?tutup", lambda m: ("navigate", {"action": "reopen_tab"})),
    (r"(?:ke )?paling (atas|bawah)(?: halaman)?", lambda m: ("navigate", {"action": "top" if m[1] == "atas" else "bottom"})),
    # ── the document that is open ──
    (r"(?:(?:analisis|analisa|cek|lihat) )?(?:(?:ini )?(?:file|dokumen|berkas) apa(?: ini)?|ini (?:file|dokumen|berkas) apa|info file(?: ini)?)",
     lambda m: ("analisis_dokumen", {"pertanyaan": "file apa ini"})),
    (r"(?:rangkum|ringkas|rangkumkan|ringkaskan|buat (?:ringkasan|rangkuman)|simpulkan|jelaskan|terjemahkan|analisis|analisa)"
     r"(?: isi)? (?:dokumen|file|berkas|pdf|word|excel|presentasi|slide|tabel|laporan|makalah|skripsi|jurnal)(?: ini| yang (?:sedang )?(?:saya )?buka)?"
     r"(?: (?:secara )?(?:lengkap|detail|singkat))?", lambda m: ("analisis_dokumen", {"pertanyaan": m[0]})),
    (r"apa (?:isi|inti|kesimpulan|poin penting) (?:dari )?(?:dokumen|file|pdf|laporan|presentasi) ini",
     lambda m: ("analisis_dokumen", {"pertanyaan": m[0]})),
    (r"(?:simpan|catat) (?:hasil )?(?:analisis|rangkuman|ringkasan)(?: dokumen)?(?: tadi| itu| ini)?",
     lambda m: ("simpan_hasil_analisis", {})),
    (r"(?:diagnosa|diagnosis|cek masalah|periksa masalah) (?:sistem|komputer|laptop|pc|windows)(?: (?:minggu ini|seminggu))?",
     lambda m: ("diagnosa_sistem", {"jam_terakhir": 168 if "minggu" in m[0] else 24})),
    # ── what's on screen ──
    (r"tutup tab(?: (?:ini|sekarang|yang ini|yang sedang dibuka))?", lambda m: ("close_current_tab", {})),
    (r"(?:tutup|close) tab (.+?)(?: (?:di|pada) (?:chrome|firefox|browser|brave|edge))?", lambda m: ("close_tab", {"name": m[1]})),
    (r"(?:pindah|beralih|switch|balik|kembali|buka|ke) ke tab (?!baru|berikutnya|selanjutnya|sebelah|sebelumnya)(.+)|"
     r"(?:kembali|balik) ke tab (?!baru|berikutnya|selanjutnya|sebelah|sebelumnya)(.+)",
     lambda m: ("switch_tab", {"name": m[1] or m[2]})),
    (r"(?:ringkas|ringkaskan|rangkum|rangkumkan|jelaskan|simpulkan) (?:isi )?(?:halaman|artikel|berita|web|website|situs|page|tab)"
     r"(?: web)? (?:ini|yang (?:sedang )?(?:saya )?(?:buka|baca))", lambda m: ("summarize_current_page", {})),
    (r"(?:halaman|artikel|berita) ini (?:tentang|membahas) apa", lambda m: ("summarize_current_page", {})),
    (r"(?:simpan|bookmark|tandai) (?:link|halaman|url|alamat|situs)(?: web)? ini", lambda m: ("save_current_link", {})),
    (r"(?:salin|copy|kopi) (?:link|url|alamat)(?: (?:halaman|web))?(?: ini)?", lambda m: ("copy_current_link", {})),
    (r"(?:saya )?(?:sedang )?(?:buka|lihat) (?:halaman |jendela |aplikasi )?apa(?: sekarang)?|ini (?:halaman|jendela|aplikasi) apa",
     lambda m: ("active_window_info", {})),
]


# the daily recap, Digidaw's own performance, watches
DAY = [
    # the day in one breath (daily.py) and what Digidaw keeps an eye on (watchers.py)
    (r"(?:(?:berikan|kasih|bacakan|buatkan|minta) )?(?:rangkuman|ringkasan|rekap|recap|rekapan|laporan|resume)"
     r"(?: (?:kegiatan|aktivitas|harian))?(?: (?:saya|ku|aku))? (?:hari ini|harian)|"
     r"(?:apa saja|apa aja|ngapain aja|ngapain saja) (?:yang )?(?:saya|aku) (?:lakukan |kerjakan )?hari ini|"
     r"hari ini (?:saya|aku) ngapain (?:aja|saja)",
     lambda m: ("daily_recap", {})),
    # how well Digidaw itself did ("laporan kinerja minggu ini")
    (r"(?:(?:berikan|kasih|bacakan|minta) )?(?:laporan )?(?:kinerja|performa)(?: (?:kamu|digidaw|anda|mu))?"
     r"(?: (hari ini|minggu ini|seminggu|tujuh hari|7 hari))?|seberapa (?:bagus|baik) (?:kamu|digidaw)"
     r"(?: (hari ini|minggu ini))?",
     lambda m: ("performance_report", {"days": 7 if (m[1] or m[2] or "").startswith(("minggu", "seminggu", "tujuh", "7"))
                                       else 1})),
    (r"(?:sudah )?berapa lama (?:saya|aku) (?:di depan|pakai|main|memakai|menggunakan|nge-?laptop di) (?:laptop|komputer|pc)"
     r"(?: hari ini)?|(?:screen time|waktu layar)(?: (?:saya|ku|aku))?(?: hari ini)?",
     lambda m: ("screen_time", {})),
    (r"(?:apa saja |apa aja |apa )?yang (?:sedang )?(?:kamu |digidaw |anda )?pantau(?: sekarang)?|daftar pantauan",
     lambda m: ("list_watches", {})),
    (r"(?:batalkan|hentikan|stop|matikan) (?:semua )?pantauan(?: (.+))?|(?:tidak|nggak|gak|ga) usah (?:dipantau|pantau|"
     r"dikabari|kabari)(?: lagi)?",
     lambda m: ("cancel_watch", {"what": m[1] or ""})),
]


# smart lamps (smarthome.py)
LAMPS = [
    # smart lamps (smarthome.py): moods, a bit brighter / dimmer, then on / off, brightness, colour, status
    # (not "matikan lampu kamar tidur": that's the bedroom lamp going off, not the "tidur" mood)
    (rf"(?!(?:matikan|padamkan|matiin|padamin|nyalakan|hidupkan|nyalain|hidupin|idupin|redupkan|terangkan|redupin|"
     rf"terangin|status|cek) )(?:\w+ )?{_LAMP} (?:ke |jadi |menjadi )?(?:(?:mode|suasana|nuansa|untuk|buat) )?{_SCENE}",
     lambda m: ("lamp_scene", {"scene": m[2], "name": m[1]})),
    (rf"(?:(?:aktifkan|nyalakan|pasang|ubah ke|ganti ke|set) )?(?:suasana|nuansa|mode lampu|lampu mode) {_SCENE}",
     lambda m: ("lamp_scene", {"scene": m[1]})),
    (rf"{_LAMP} (?:lebih|agak|sedikit|dikit|kurang) (terang|redup|gelap)(?:in|kan)?(?: lagi| sedikit| dikit)?|"
     rf"(?:terangkan|redupkan|terangin|redupin|gelapkan) {_LAMP} (?:lagi|sedikit|dikit|sedikit lagi)",
     lambda m: ("lamp_brightness", {"percent": 30 if "terang" in m[0] else -30,     # ("kurang terang" = brighter)
                                    "relative": True, "name": m[1] or m[3]})),
    (r"(?:lebih|agak|sedikit|dikit) (terang|redup|gelap)(?:an|in|kan)?(?: lagi| sedikit| dikit)?|"
     r"(?:terangkan|redupkan|terangin|redupin|gelapkan|gelapin)(?: lagi| sedikit| dikit)?",
     lambda m: ("lamp_brightness", {"percent": 30 if "terang" in m[0] else -30, "relative": True})
     if smarthome.has_lamps() and smarthome.changed_by_user_since(time.time() - 600) else None),
    (r"(?:sekali |dikit |sedikit |tambah |naik |turun |kurang )?lagi(?: dong| deh| ya)?|(?:dikit|sedikit)(?: (?:dong|aja|saja))?|"
     r"sekali lagi|ulangi(?: lagi)?", lambda m: _repeat_last(m[0])),
    (r"bukan (?!itu\b|ini\b|yang\b|begitu\b|gitu\b|dia\b|saya\b|aku\b|kamu\b|anda\b|apa\b)(.{2,40})",
     lambda m: _resolved("open", m[1], sure_only=True)),
    (r"(?:kapan|jam berapa|pukul berapa) matahari (?:terbenam|terbit|tenggelam)(?: hari ini)?|"
     r"matahari (?:terbenam|terbit|tenggelam) (?:jam|pukul) berapa(?: hari ini)?",
     lambda m: ("sun_today", {})),
    (r"(?:nyalakan|hidupkan|nyalain|idupin|hidupin|aktifkan) (?:semua )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)|(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:nyala|on|hidup)(?:kan)?",
     lambda m: ("lamp_power", {"on": True, "name": m[1] or m[2]})),
    (r"(?:matikan|padamkan|matiin|padamin) (?:semua )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)|(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:mati|off|padam)(?:kan)?",
     lambda m: ("lamp_power", {"on": False, "name": m[1] or m[2]})),
    (r"(?:(?:atur|set|setel|ubah) )?(?:kecerahan )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:ke |jadi |di )?(\d{1,3}) ?(?:persen|%)?|"
     r"(?:redupkan|terangkan|redupin|terangin) (lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)(?: (?:ke |jadi )?(\d{1,3}) ?(?:persen|%)?)?",
     lambda m: ("lamp_brightness", {"percent": int(m[2] or m[4] or (30 if "redup" in m[0] else 100)), "name": m[1] or m[3]})),
    (r"(?:\w+ )?(?:warna )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:jadi |ke |warna |menjadi )*"
     r"(merah muda|biru muda|putih hangat|putih dingin|kuning hangat|merah|hijau|biru|kuning|oranye|jingga|ungu|pink|toska|"
     r"cyan|putih|hangat|netral)",
     lambda m: ("lamp_color", {"color": m[2], "name": m[1]})),
    (r"(?:status|cek) (lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)|(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:nyala|menyala|mati) (?:nggak|gak|tidak|enggak)",
     lambda m: ("lamp_status", {"name": m[1] or m[2]})),
]


# messages and notifications
MESSAGES = [
    # "Mau saya bacakan?" after a message was announced (or the welcome back): "ya" / "bacakan" reads the messages
    (r"(?:ya|iya|iyah|yap|yes|boleh|oke|ok|silakan|silahkan|baik|sip)(?: (?:bacakan|bacain|baca|silakan|boleh|saja|aja))*|"
     r"(?:coba )?(?:bacakan|bacain|baca)(?: (?:saja|aja|pesannya|semuanya|semua))*|apa isinya|isinya apa",
     lambda m: _read_offered()),
    # notifications that came in (WhatsApp etc.): who sent it and what it says
    (r"(?:tolong )?(?:cek|lihat|baca|bacakan|bacain|periksa|tampilkan|kode|cok|cik|check|jek)(?: semua)? (?:pesan|chat)(?: yang)?(?: (?:masuk|baru|terbaru|tadi))?"
     r"(?: (?:di |dari )?(whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: (?:ada )?apa(?: saja)?)?|"
     r"(?:apakah |apa )?ada (?:pesan|chat)(?: (?:baru|masuk|baru masuk))?(?: (?:di |dari )?(whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: tidak| nggak| gak)?|"
     r"(?:apakah |apa )?ada (?:pesan|chat) (whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram)(?: (?:baru|masuk))?",
     lambda m: ("read_notifications", {"messages_only": True, "app": next((g for g in m.groups() if g), "")})),
    (r"(?:tolong )?(?:cek|lihat|baca|bacakan|bacain|periksa|tampilkan)(?: semua)? (?:notifikasi|notif)(?: (?:yang )?(?:masuk|baru|tadi))?"
     r"(?: (?:di |dari )?(whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: (?:ada )?apa(?: saja)?)?|"
     r"(?:apakah |apa )?ada (?:notifikasi|notif)(?: (?:baru|masuk))?(?: (?:di |dari )?(whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: tidak| nggak| gak)?|"
     r"notifikasi apa(?: saja)?(?: yang masuk)?",
     lambda m: ("read_notifications", {"app": next((g for g in m.groups() if g), "")})),
    (r"(?=.*(?:ulang|lagi|tadi|itu))(?:baca|bacakan|bacain|ulangi)(?: ulang| lagi)?(?: (?:pesan|notifikasi|notif))?(?: (?:tadi|itu))?(?: lagi)?"
     r"(?: (?:dari |di )?(whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?",
     lambda m: ("read_notifications", {"include_read": True, "app": next((g for g in m.groups() if g), "")})),
    (r"(?:sekarang )?(?:jam|pukul) berapa(?: sekarang)?|jam|waktu|jam sekarang|sekarang jam(?: berapa)?",
     lambda m: ("get_time", {})),
    (r"(?:(?:bagaimana|gimana|apa|cek|lihat)\s+)?cuaca(?: (?:hari ini|sekarang|saat ini|di luar|di sini|hari ini gimana|bagaimana))?",
     lambda m: ("get_weather", {})),
    (r"(?:(?:bagaimana|gimana|apa|cek|lihat)\s+)?cuaca (?:di )?(?!hari|sekarang|saat|luar|sini|bagaimana|besok|lusa|nanti|malam|pagi|sore|siang|minggu)([a-z]{3,}(?: [a-z]{3,})?)",
     lambda m: ("get_weather", {"city": m[1].strip()})),
]


# rain and the forecast (weather.py)
WEATHER = [
    # rain and tomorrow's weather: from a real forecast (weather.py), never a guess
    (rf"(?:(?:apakah|apa|kira kira) )?{FORECAST_WHEN} (?:akan |bakal |bakalan )?(?:hujan|turun hujan|cerah)"
     rf"(?: (?:nggak|gak|tidak|enggak|ga|engga|kah))?|"
     rf"(?:(?:apakah|apa|kira kira) )?(?:akan |bakal |bakalan )?(?:hujan|turun hujan)(?: {FORECAST_WHEN})?"
     rf"(?: (?:nggak|gak|tidak|enggak|ga|engga|kah))?(?: {FORECAST_WHEN})?|"
     rf"(?:(?:bagaimana|gimana|apa|cek|lihat) )?(?:(?:prakiraan|ramalan|perkiraan) )?cuaca(?: untuk)? {FORECAST_WHEN}"
     r"(?: (?:gimana|bagaimana|seperti apa|kayak apa))?(?: (?:hujan|cerah))?(?: (?:nggak|gak|tidak|enggak))?|"
     rf"(?:prakiraan|ramalan|perkiraan) cuaca(?: {FORECAST_WHEN})?",
     lambda m: ("get_forecast", {"when": _forecast_when(m[0])})),
    (r"(?:hari ini )?(?:tanggal|hari) (?:berapa|apa)(?: (?:hari ini|sekarang))?", lambda m: ("get_date", {})),
]


# volume and music
SOUND = [
    # "volume 50" / "naikkan volume ke 80" set a level; "turunkan volume 5 persen" (no "ke") lowers BY 5 (below)
    (r"(?:(?:atur|set|ubah|setel) )?(?:volume|suara)(?:nya)? (?:ke |jadi |menjadi |sampai |hingga |di |pada )?(\d{1,3})(?: ?(?:persen|%))?|"
     r"(?:naikkan|turunkan|kecilkan|besarkan|keraskan|pelankan|kurangi|tambah) (?:volume|suara)(?:nya)? "
     r"(?:ke|jadi|menjadi|sampai|hingga) (\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("set_volume", {"level": int(m[1] or m[2])})),
    (r"(?:naikkan|besarkan|keraskan|kencangkan|tambah(?:kan)?) (?:volume|suara)(?:nya)?(?: (?:sebanyak |sebesar )?(\d{1,3}) ?(?:persen|%)?)?(?: lagi)?"
     r"|(?:volume|suara) (?:naik|lebih (?:keras|besar|kencang))(?: (\d{1,3}) ?(?:persen|%)?)?(?: lagi)?",
     lambda m: ("volume_up", {"amount": int(m[1] or m[2] or 10)})),
    (r"(?:turunkan|kecilkan|pelankan|kurangi) (?:volume|suara)(?:nya)?(?: (?:sebanyak |sebesar )?(\d{1,3}) ?(?:persen|%)?)?(?: lagi)?"
     r"|(?:volume|suara) (?:turun|lebih (?:pelan|kecil))(?: (\d{1,3}) ?(?:persen|%)?)?(?: lagi)?",
     lambda m: ("volume_down", {"amount": int(m[1] or m[2] or 10)})),
    (r"mute|unmute|bisukan|matikan suara|nyalakan suara|hidupkan suara", lambda m: ("volume_mute", {})),
    (r"(?:putar|putarkan|lanjut ke|ke) (?:lagu |musik |video )?(?:selanjutnya|berikutnya)|(?:musik|lagu) (?:selanjutnya|berikutnya)",
     lambda m: ("media_control", {"action": "next"})),
    (r"(?:pause|paus|pos|jeda|stop|setop|hentikan|berhenti|matikan)(?: dulu)?(?: (?:musik|musiknya|music|lagu|lagunya|song|video|videonya))?"
     r"|(?:pause|paus|jeda|stop|matikan) (?:musik|lagu|video)(?:nya)?(?: dulu)?",
     lambda m: ("media_control", {"action": "pause"})),
    (r"musik|music|musiknya|(?:hidupkan|nyalain|nyalakan) (?:musik|lagu)(?:nya)?(?: lagi)?|play music|"
     r"(?:play|resume|lanjut|lanjutkan|putar lagi|putar kembali|putar|putarkan|nyalakan|setel)(?: (?:musik|musiknya|lagu|lagunya|video|videonya))?"
     r"|(?:play|resume|lanjut|lanjutkan|putar lagi|putar kembali)(?: (?:musik|musiknya|lagu|lagunya|video|videonya))?",
     lambda m: ("media_control", {"action": "play"})),
    (r"next|skip|lewati|ganti lagu|(?:lagu|video) (?:selanjutnya|berikutnya)", lambda m: ("media_control", {"action": "next"})),
    (r"previous|(?:lagu|video) sebelumnya", lambda m: ("media_control", {"action": "previous"})),
    (r"(?:ini )?lagu apa(?: ini)?|judul lagu(?: ini|nya)?(?: apa)?|lagu apa yang (?:sedang )?(?:diputar|main)",
     lambda m: ("now_playing", {})),
    (r"(?:putar|putarkan|play|setel|mainkan) (?:playlist|lagu-lagu|mix) (.+)", lambda m: _play(m[1], mix=True)),
    # "putar youtube / putar musik / lanjutkan musik": resume (or pick up the last music), not a search for "youtube"
    (r"(?:putar|putarkan|play|setel|mainkan|lanjutkan|lanjut)(?: lagi)? (?:pemutaran )?(?:youtube|musik(?:nya)?|lagu(?:nya)?|video(?:nya)?)"
     r"(?: (?:di )?youtube)?(?: lagi)?", lambda m: ("media_control", {"action": "play"})),
    # "putar musik tradisional": "musik" stays in the search (a genre); "putar lagu X": X is the title
    (r"(?:putar|putarkan|play|setel|mainkan) (lagu|musik|video) (.+?)(?: di youtube)?",
     lambda m: _play(re.sub(r"^(?:dari|oleh|by) ", "", m[2]) if re.match(r"(?:dari|oleh|by) ", m[2])
                     else f"musik {m[2]}" if m[1] == "musik" else m[2])),
    (r"(?:putar|putarkan|setel|mainkan) (?!(?:lagu|musik|video|playlist)$)(.{3,}?)(?: di youtube)?", lambda m: _play(m[1])),
    (r"(?:ambil |tangkap )?(?:screenshot|tangkapan layar|tangkap layar)(?: layar)?", lambda m: ("take_screenshot", {})),
    (r"kunci (?:komputer|laptop|pc|layar)", lambda m: ("lock_workstation", {})),
]


# the PC's settings and jobs on the screen (pc_control.py, screen_agent.py, file_tools.py)
CONTROL = [
    (r"(?:matikan|nonaktifkan|off(?:kan)?) (?:koneksi )?wi ?fi", lambda m: ("wifi_power", {"on": False})),
    (r"(?:nyalakan|hidupkan|aktifkan|on(?:kan)?) (?:koneksi )?wi ?fi", lambda m: ("wifi_power", {"on": True})),
    (r"(?:sambungkan|hubungkan|konek\w*|connect)(?: ke)? wi ?fi(?: ke)? (.{2,40})", lambda m: ("wifi_connect", {"name": m[1]})),
    (r"(?:matikan|nonaktifkan) bluetooth", lambda m: ("bluetooth", {"action": "off"})),
    (r"(?:nyalakan|hidupkan|aktifkan) bluetooth", lambda m: ("bluetooth", {"action": "on"})),
    (r"(?:sambungkan|hubungkan|konek\w*|connect) (?:bluetooth (?:ke )?|ke )?((?:headset|earphone|earbuds|tws|speaker bluetooth|.{2,30} bluetooth).*)|"
     r"(?:sambungkan|hubungkan|konek\w*|connect) bluetooth (?:ke )?(.{2,40})",
     lambda m: ("bluetooth", {"action": "connect", "device": re.sub(r"\s*bluetooth\s*", " ", m[1] or m[2]).strip()})),
    (r"putuskan bluetooth(?: (.{2,40}))?", lambda m: ("bluetooth", {"action": "disconnect", "device": m[1] or ""})),
    (r"(?:pakai |aktifkan |ubah ke |ganti ke |set )?mode (hemat(?: daya| baterai| batre)?|seimbang|performa(?: tinggi)?|gaming|game)",
     lambda m: ("power_mode", {"mode": m[1]})),
    (r"(?:matikan|nonaktifkan)(?: mode)? jangan ganggu", lambda m: ("do_not_disturb", {"on": False})),
    (r"(?:aktifkan |nyalakan |pakai )?(?:mode )?jangan ganggu", lambda m: ("do_not_disturb", {"on": True})),
    (r"(?:pindahkan |pindah |alihkan |ubah |ganti )?(?:suara|audio|keluaran suara)(?:nya)? ke (.{2,30})",
     lambda m: ("audio_output", {"name": m[1]})),
    (r"(?:matikan|padamkan|offkan) (?:layar|monitor)(?: laptop)?", lambda m: ("screen_power", {"on": False})),
    (r"(?:hentikan|batalkan|stop|setop) tugas(?:nya)?(?: (?:di )?layar)?", lambda m: ("stop_task", {})),
    (r"lanjutkan tugas(?:nya)?", lambda m: ("resume_task", {})),
    (r"(?:apa )?isi folder (.{2,40}?)(?: apa saja| apa aja)?", lambda m: ("list_folder", {"path": m[1]})),
]


# power, keyboard, closing everything, "katakan", analysis, security scan, temperature, …
SYSTEM = [
    # ── power: always the real skill (it asks "yakin?"), never an AI that might only SAY it is restarting ──
    (r"(?:restart|reboot|mulai ulang|nyalakan ulang|hidupkan ulang) (?:komputer|laptop|pc|sistem)(?: (?:saya|ku|sekarang))?|restart",
     lambda m: ("restart_pc", {})),
    (r"(?:matikan|shutdown|shut down|matiin) (?:komputer|laptop|pc|sistem)(?: (?:saya|ku|sekarang))?|shutdown|shut down",
     lambda m: ("shutdown_pc", {})),
    (r"(?:sleep|tidurkan|sleepkan) (?:komputer|laptop|pc)(?: (?:saya|ku))?", lambda m: ("sleep_pc", {})),
    (r"(?:batalkan|batal|cancel) (?:shutdown|restart|matikan komputer|mematikan komputer)", lambda m: ("cancel_shutdown", {})),
    # ── keyboard ──
    (r"(?:ketik|ketikkan|tuliskan di sini|type) (.+?)(?: (?:dan|lalu|terus|kemudian) (?:tekan |klik |pencet )?enter)?",
     lambda m: ("type_text", {"text": m[1], "press_enter": bool(re.search(r" (?:dan|lalu|terus|kemudian) (?:tekan |klik |pencet )?enter$", m[0]))})),
    (r"(?:klik (?!kanan\b|kiri\b|atas\b|bawah\b)|tekan |pencet |press )(?:tombol )?(enter|tab|escape|esc|spasi|space|backspace|delete|home|end|f\d{1,2}|"
     r"page ?up|page ?down|atas|bawah|kiri|kanan)(?: (\d) ?kali)?",
     lambda m: ("press_keys", {"keys": {"spasi": "space", "atas": "up", "bawah": "down", "kiri": "left", "kanan": "right",
                                       "page up": "pageup", "page down": "pagedown"}.get(m[1], m[1])})),
    # ── the mouse: where the pointer is, or something described in words (the AI looks for it) ──
    (r"klik(?: kiri)?(?: di ?sini)?", lambda m: ("click_here", {})),
    (r"klik kanan(?: di ?sini)?", lambda m: ("click_here", {"button": "right"})),
    (r"klik (?:dua kali|dobel|double)(?: di ?sini)?", lambda m: ("click_here", {"double": True})),
    (r"klik (.{2,60})", lambda m: ("click_on_screen", {"target": m[1]})),
    # ── close everything (asks first) ──
    (r"tutup semua(?: (?:aplikasi|jendela|program|app|window))?(?: yang (?:sedang )?(?:terbuka|ada|berjalan|jalan))?(?: sekarang)?",
     lambda m: ("close_all_apps", {})),
    # ── "katakan X": just say it ──
    (r"(?:katakan|ucapkan|coba katakan|coba ucapkan) (?!apa\b|kenapa\b|ke\b|bahwa\b)(.{1,80})",
     lambda m: ("say", {"text": m[1]})),
    (r"(?:atur |set |ubah |setel )?(?:kecerahan|brightness)(?: layar)?(?:nya)? (?:ke |jadi |menjadi |di )?(\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("set_brightness", {"level": int(m[1])})),
    # ── Antigravity system analysis ──
    (r"(?:lakukan |jalankan |tolong |minta |suruh )?(?:sambungkan ke )?(?:antigravity |antigrafity )?(?:untuk )?"
     r"(?:analisis|analisa|analisi|periksa|cek) (?:seluruh |kondisi |kesehatan )?(?:sistem|laptop|komputer|pc)"
     r"(?: (?:saya|ku))?(?: (?:secara )?(?:menyeluruh|lengkap|total))?(?: (?:dengan|menggunakan|lewat|pake|pakai) (?:antigravity|antigrafity))?|"
     r"(?:minta |suruh )?(?:antigravity|antigrafity) (?:untuk )?(?:analisis|analisa|periksa|cek)(?: (?:seluruh |kondisi |kesehatan )?(?:sistem|laptop|komputer|pc))?",
     lambda m: ("analisis_sistem", {})),
    (r"(?:buka )?laporan (?:analisis sistem|antigravity)(?: terakhir)?",
     lambda m: ("last_system_analysis", {})),
    # ── Security scan ──
    (r"(?:lakukan |jalankan |tolong )?(?:analisis|analisa|analisi|scan|pindai|periksa|cek) (?:keamanan|security|virus|malware|ancaman)"
     r"(?: (?:sistem|laptop|komputer|pc))?(?: saya| ku)?(?: (?:secara )?(?:menyeluruh|lengkap|total))?|cek virus|apakah (?:laptop|komputer|pc|sistem) (?:saya |ku )?aman",
     lambda m: ("security_scan", {})),
    (r"(?:buka )?laporan (?:keamanan|analisis keamanan)(?: terakhir)?", lambda m: ("last_security_report", {})),
    # how hot the laptop is (not the weather)
    (r"(?:cek |lihat |berapa |cek berapa )?(?:suhu|temperatur|temperature|panas)(?: (?:cpu|prosesor|processor|gpu|vga|"
     r"kartu grafis|laptop|komputer|pc|mesin|hardware|ssd|perangkat))+(?: (?:saya|ku|aku|sekarang|berapa|saat ini|sih))*|"
     r"(?:apakah |apa )?(?:laptop|komputer|pc|cpu|prosesor)(?: (?:saya|ku|aku|ini))? (?:panas|kepanasan|overheat|terlalu panas)"
     r"(?: (?:nggak|gak|tidak|enggak|ga|banget|sekali))*|kenapa kipas(?:nya)?(?: laptop)? (?:kencang|berisik|bunyi)(?: banget| sekali)?",
     lambda m: ("get_temperature", {})),
    (r"(?:status sistem|diagnostik(?: sistem)?|cek baterai)", lambda m: ("get_system_status", {})),
    (r"(?:cek |berapa )?(?:penggunaan|pemakaian|pakai|sisa|kapasitas)? ?(?:ram|memori|memory|cpu|prosesor|processor)"
     r"(?: (?:saya|laptop|komputer|ku))?(?: (?:berapa|sekarang|sudah berapa|terpakai))*(?: persen)?",
     lambda m: ("get_system_status", {})),
    (r"(?:baterai|batre|batrai)(?: (?:saya|laptop|ku))?(?: (?:berapa|tinggal berapa|sisa berapa))?(?: persen)?(?: sekarang)?",
     lambda m: ("get_system_status", {})),
    (r"(?:cek |lihat |tampilkan |sebutkan |coba cek )?(?:ada )?(?:jendela|aplikasi|window) apa saja(?: yang)? (?:terbuka|sedang terbuka|dibuka|berjalan|jalan)(?: sekarang)?|"
     r"(?:daftar|list) (?:jendela|window)(?: yang terbuka)?", lambda m: ("list_windows", {})),
    (r"(?:buka|open) (?:folder )?(?:project|proyek)(?: saya| ku)?", lambda m: ("open_project", {})),
    (r"(?:buat|bikin|buatkan|bikinkan)(?: saya| aku)?(?: sebuah| satu)? (?:project|proyek|aplikasi|app|website|web|situs|game|program)\b.*",
     lambda m: ("create_project", {"description": m[0]})),
    (r"(?:status|progres|progress) (?:project|proyek|pembuatan project)(?: (.+))?",
     lambda m: ("project_build_status", {"project": m[1] or ""})),
    (r"(?:batalkan|batal|hentikan|stop) (?:pembuatan|pengerjaan|antrean) (?:project|proyek)(?: (.+))?",
     lambda m: ("cancel_project_build", {"project": m[1] or ""})),
    (r"(?:hentikan|matikan|stop|tutup) (?:project|proyek)(?: (.+))?", lambda m: ("stop_project", {"project": m[1] or ""})),
    (r"(?:daftar|list) (?:project|proyek)(?: saya)?|project apa saja(?: yang (?:sudah )?(?:dibuat|ada))?",
     lambda m: ("list_projects", {})),
]


# workspaces, windows and apps (the most general routes: last)
WINDOWS = [
    # ── workspaces (Hyprland) ──
    (rf"(?:(?:pindah|beralih|switch|balik|kembali|pergi|buka|lompat)(?: ke)? |ke |\w{{3,8}}(?: ke)? )?{WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)",
     lambda m: _ws_route("switch_workspace", {"workspace": _ws(m[1])})),
    (rf"(?:pindahkan|pindahin|kirim|kirimkan|lempar|taruh|move) (.+?) ke {WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)"
     r"( (?:dan|lalu|terus) (?:ikut(?: pindah)?|ikuti|ke sana|pindah ke sana))?",
     lambda m: _ws_route("move_to_workspace", {"workspace": _ws(m[2]), "follow": bool(m[3]),
                                               "name": "" if re.fullmatch(r"(?:jendela|aplikasi)? ?(?:ini|aktif|yang aktif|sekarang)|jendela|aplikasi", m[1]) else m[1]})),
    (r"(?:pindah|beralih|switch|fokus|balik) ke (?:jendela |aplikasi )?(?!halaman|tab|versi|mode)(.+)",
     lambda m: _focus_known(m[1])),
    # "ke WhatsApp" / "kembali ke discord": the same, without the verb (speech often drops it)
    (r"(?:ke|kembali ke|balik ke) (?!halaman|tab|versi|mode|paling|atas|bawah|sana|sini|mana|situ)(.{2,40})",
     lambda m: _focus_known(m[1], raw_fallback=False)),
    (r"(?:pindah|pindah dong|pindah lagi|geser)", lambda m: _ws_route("switch_workspace", {"workspace": "next"})),
    (r"(?:lagu|song) (?!apa\b|ini\b|itu\b|berikutnya\b|selanjutnya\b|sebelumnya\b|yang\b|lagi\b|tadi\b|favorit)(.{2,60})",
     lambda m: _play(m[1])),
    (r"([a-z][a-z ]{1,30}) music", lambda m: _play(m[0])),
    (r"(?:buka|open) (.+?) (?:di|pakai|pake|lewat|dengan) (firefox|chrome|google chrome|brave|edge|chromium)",
     lambda m: _open_in_browser(m[1], m[2])),
    (r"(?:butuh|perlu|pengen buka|mau buka) (.{2,40})", lambda m: _open_known(m[1], single=True)),
    # "untuk Firefox": usually "tutup Firefox" misheard — when it is open, ask instead of answering into the void
    (r"(?:untuk|untung|utuk|tuk) (.{2,30})", lambda m: _ask_close(m[1])),
    (r"(?:buka|open|jalankan) (.+)", lambda m: _open_known(m[1])),
    (r"(?:tutup|close) (.+)", lambda m: _close_known(m[1])),
    # "cari kepiting": on YouTube when YouTube is in front, otherwise Google (not a chat about crabs)
    (r"(?:cari|carikan|search|googling)(?: di google)? (?!tahu\b|tau\b|file\b|folder\b|dokumen\b|berkas\b|"
     r"jendela\b|aplikasi\b|tab\b)(.{2,80})", lambda m: _search_route(m[1])),
]


ORDER = [SITE_SEARCH, ASSISTANT, PROJECTS, SECURITY, SCREEN, DAY, LAMPS, MESSAGES, WEATHER, SOUND, CONTROL, SYSTEM, WINDOWS]
FAST_ROUTES = [(re.compile(p), h) for group in ORDER for p, h in group]
