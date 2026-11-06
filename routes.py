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

import quick_facts

import agenda
import alarms
import calculator
import media
import pc_control
import smarthome
import system_control as sc
from brain import (  # noqa: E402  (brain imports this module last, when all of these exist)
    FORECAST_WHEN,
    NUMBER_WORDS,
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
    _read_unread,
    _repeat_last,
    _resolved,
    _search_route,
    _that_app,
    _ws,
    _ws_route,
)

_COIN = "|".join(sorted(quick_facts.COINS, key=len, reverse=True))
_CURRENCY = "|".join(sorted(quick_facts.CURRENCIES, key=len, reverse=True))
# a lamp named in a command ("lampu", "lampu kamar"), not the words after it ("lampu ke merah", "lampu lebih terang")
_LAMP = (r"(lampu(?:nya)?(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|mode\b|suasana\b|nuansa\b|"
         r"untuk\b|buat\b|lebih\b|agak\b|sedikit\b|dikit\b|kurang\b|\d)\w+)?)")
_SCENE = (r"(baca|membaca|kerja|bekerja|fokus|belajar|konsentrasi|santai|rileks|relax|chill|malam|tidur|film|nonton|"
          r"menonton|bioskop|romantis|pesta|party|disko|warna warni|pelangi)")


# after "volume" / "suara" these aren't an app: "naikkan volume lagi", "turunkan suara dikit", "matikan suara laptop"
_NOT_AN_APP = frozenset("""lagi dikit sedikit banyak aja saja dong deh ya yuk nih sih dulu banget tolong tuan digidaw kamu
    kau anda aku saya laptop komputer pc sistem system speaker headset headphone earphone earbud bluetooth hdmi semua utama
    master keseluruhan notifikasi notif alarm sekarang berapa penuh full maksimal maksimum max minimal minimum min kecil
    besar pelan keras kencang rendah tinggi setengah nol""".split())


def _app_sound(app: str, **what):
    """The sound of one app, or None (the general volume routes below) when the word is no app. ("turunkan volume 20"
    lowers the volume by 20; it was an app called "20" for an hour on 6 Nov.)"""
    return None if app.isdigit() or app in _NOT_AN_APP else ("app_volume", {"app": app, **what})


# what "musik …" can be followed by when it is a kind of music rather than a title
_GENRE_RE = re.compile(r"\S+$|(?:santai|tradisional|pop|rock|rok|jazz|dangdut|klasik|indie|lo ?fi|akustik|religi|sedih|galau|semangat|tidur|belajar|fokus|"
                       r"indonesia|jawa|sunda|barat|korea|jepang|anime|instrumental|piano|gitar|edm|remix|reggae|kerja|olahraga)\b")


# "cari …" with a site: YouTube search or just the site
SITE_SEARCH = [
    # ── "cari di pencarian youtube doraemon" (without a word to look for: core asks "cari apa") ──
    (r"(?:cari|carikan|cariin|telusuri)(?: di)? (?:kolom )?pencarian(?: (youtube|google))? (.{2,80})",
     lambda m: ("search_youtube" if m[1] == "youtube" else "google_search", {"query": m[2]}) if m[1] else _search_route(m[2])),
    # ── "cari ..." with a site: search on YouTube, or just open the site ("cari youtube" cut off by the mic) ──
    (r"(?:cari|carikan|cariin|search)(?: di)? youtube (?!di$)(.+)|(?:cari|carikan|cariin|search) (.+) di youtube",
     lambda m: ("search_youtube", {"query": m[1] or m[2]})),
    (r"(?:cari|carikan|cariin|search)(?: di)? ([\w ]+?)(?: di| dong)?",
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
    (r"(?:latih|ajari|belajar|rekam)(?: ulang)?(?: suara)? (?:panggilan|nama digidaw|suara panggilan|panggil digidaw)(?: saya|ku)?", lambda m: ("train_my_call", {})),
    (r"(?:hapus|lupakan) (?:latihan|rekaman|suara) panggilan(?: saya|ku)?", lambda m: ("forget_my_call", {})),
    (r"(?:tes|test|uji|coba|coba bunyikan|bunyikan)(?: suara)? alarm(?: nya)?", lambda m: ("test_alarm", {})),
    (r"(?:jadikan|jadiin|pakai)(?: (?:lagu|musik|video))?(?: (?:ini|yang ini))? (?:sebagai |jadi |untuk |menjadi )?(?:nada |suara |bunyi )?alarm(?:nya| saya|ku)?",
     lambda m: ("song_as_alarm", {})),
    (r"(?:jadikan|jadiin|pakai) (?:lagu |musik )?(.{2,60}?) (?:sebagai |jadi |untuk |menjadi )?(?:nada |suara |bunyi )?alarm(?:nya| saya|ku)?",
     lambda m: ("song_as_alarm", {"title": m[1]})),
    (r"(?:ganti|ubah|atur|pilih|setel|tambah|tambahkan)(?: suara| nada| bunyi| lagu| audio| musik)+ alarm(?:nya)?|"
     r"(?:pengaturan|setelan) alarm", lambda m: ("sound_settings", {})),
    # ── prices and the time elsewhere: straight from the source, not through a web search and an AI (13 s at the median) ──
    (rf"(?:cek |berapa )?harga (?:koin |crypto |kripto )?({_COIN})(?: (?:hari ini|sekarang|saat ini|berapa))*|({_COIN}) (?:sekarang |hari ini )?(?:harganya )?berapa",
     lambda m: ("coin_price", {"coin": m[1] or m[2]})),
    (r"(?:cek |berapa )?harga emas(?: (?:antam|hari ini|sekarang|saat ini|per gram|segram|berapa))*|emas (?:hari ini |sekarang )?(?:harganya )?berapa", lambda m: ("gold_price", {})),
    (rf"(?:cek |berapa )?(?:kurs|nilai tukar|harga)(?: rupiah)?(?: ke| terhadap)? ({_CURRENCY})(?: (?:hari ini|sekarang|saat ini|berapa|ke rupiah))*",
     lambda m: ("exchange_rate", {"currency": m[1]})),
    (rf"(?:konversi |ubah |tukar |berapa )?(\d+|[a-z]+) ({_CURRENCY})(?: (?:ke|jadi|dalam|berapa|sama dengan))*(?: rupiah| idr)?(?: berapa)?",
     lambda m: ("exchange_rate", {"currency": m[2], "amount": _num(m[1])}) if _num(m[1]) else None),
    (r"(?:sekarang |saat ini )?(?:jam|pukul) berapa(?: sekarang)? di (.{2,20})|di (.{2,20}?) (?:sekarang )?(?:jam|pukul) berapa(?: sekarang)?",
     lambda m: ("world_time", {"place": m[1] or m[2]}) if quick_facts.knows_place(m[1] or m[2]) else None),
    # ── counting out loud is not a sum ("berhitung 1-50" is not 1 minus 50) ──
    (r"(?:coba )?(?:berhitung|menghitung)(?: dari)? (\w+) ?(?:-|sampai|hingga|ke) ?(\w+)|(?:coba )?hitung(?: dari)? (\w+) (?:sampai|hingga) (\w+)",
     lambda m: ("count_aloud", {"start": _num(m[1] or m[3]), "end": _num(m[2] or m[4])}) if str(m[1] or m[3]) == "0" or _num(m[1] or m[3]) else None),
    (r"(?:coba )?(?:berhitung|menghitung|hitung) mundur(?: dari)? (\w+)(?: (?:sampai|hingga|ke) (\w+))?",
     lambda m: ("count_aloud", {"start": _num(m[1]), "end": _num(m[2]) if m[2] else 1}) if _num(m[1]) else None),
    # ── sums: exact and instant ("berapa 20 * 30", "10 + 10 berapa", "hitung 25 persen dari 80") ──
    (r"(?:berapa(?: hasil(?:nya)?)?(?: dari)?|hitung(?:kan)?|hasil(?: dari)?)? ?(.*\d.*?)"
     r"(?: (?:berapa|hasilnya(?: berapa)?|sama dengan(?: berapa)?))?(?: ?=)?",
     lambda m: ("calculate", {"sum_said": m[1]}) if calculator.parse(m[1]) else None),
    # ── is the AI working? (must work without the AI) ──
    (r"(?:cek|tes|test|periksa|status)(?: koneksi)? (?:ai|a i|gemini|otak(?: ai)?)(?: (?:saya|digidaw|nya))?|"
     r"(?:apakah|kenapa) ai(?: ?nya)? (?:berfungsi|jalan|normal|tidak (?:jalan|berfungsi|bisa)|error)",
     lambda m: ("ai_status", {})),
    # ── the HUD window ──
    (r"(?:buka|tampilkan|munculkan|perlihatkan|lihat|open|show|keluarkan)(?: lagi)? (?:hud|h u d|hut|had|ho|hood|hot|"
     r"hut|hat|dashboard|sistem|system|sistim|"
     r"tampilan(?: digidaw)?|panel digidaw|aplikasi digidaw|jendela digidaw|layar digidaw|digidaw)(?: nya|nya| digidaw)?",
     lambda m: ("show_hud", {})),
    (r"(?:tutup|sembunyikan|minimize|minimalkan|kecilkan) (?:hud|h u d|hut|dashboard|sistem|system|sistim|tampilan digidaw|jendela digidaw)(?:nya)?",
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
    (r"(?:tingkatkan|naikkan|tambah|besarkan|terangkan) kecerahan (lampu(?:nya)?)", lambda m: ("lamp_brightness", {"percent": 30, "relative": True, "name": m[1]})),
    (r"(?:turunkan|kurangi|kecilkan|redupkan) kecerahan (lampu(?:nya)?)", lambda m: ("lamp_brightness", {"percent": -30, "relative": True, "name": m[1]})),
    (r"(?:\w{3,8} |(?:atur|set|setel|ubah|turunkan|naikkan|tingkatkan) )?(?:kecerahan )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:ke |jadi |di )?(\d{1,3}) ?(?:persen|%)?|"
     r"(?:redupkan|terangkan|redupin|terangin) (lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)(?: (?:ke |jadi )?(\d{1,3}) ?(?:persen|%)?)?",
     lambda m: ("lamp_brightness", {"percent": int(m[2] or m[4] or (30 if "redup" in m[0] else 100)), "name": m[1] or m[3]})),
    (r"(?:\w+ )?(?:warna )?(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:jadi |ke |warna |menjadi )*"
     r"(merah muda|biru muda|putih hangat|putih dingin|kuning hangat|merah|hijau|biru|kuning|oranye|jingga|ungu|pink|toska|"
     r"cyan|putih|hangat|netral)",
     lambda m: ("lamp_color", {"color": m[2], "name": m[1]})),
    (r"(?:\w{3,8} )?warna (merah muda|biru muda|putih hangat|putih dingin|kuning hangat|merah|hijau|biru|kuning|oranye|jingga|ungu|pink|toska|cyan|putih)",
     lambda m: ("lamp_color", {"color": m[1], "name": "lampu"}) if smarthome.has_lamps() else None),
    (r"(?:status|cek) (lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?)|(lampu(?: (?!jadi\b|ke\b|warna\b|menjadi\b|di\b|nyala|mati|hidup|padam|on\b|off\b|\d)\w+)?) (?:nyala|menyala|mati) (?:nggak|gak|tidak|enggak)",
     lambda m: ("lamp_status", {"name": m[1] or m[2]})),
]


def _seek(action: str, amount: str, unit: str):
    return "media_control", {"action": action, "seconds": _num(amount) * (60 if unit == "menit" else 1)}


def _fullscreen():
    """The video when Digidaw's player is open, otherwise the window in front."""
    return ("media_control", {"action": "fullscreen"}) if media._alive() else ("window_action", {"action": "maximize"})


_AMOUNT = rf"(\d+|{'|'.join(NUMBER_WORDS)})"
_LAPTOP = r"(?:laptop|komputer|pc)(?: (?:saya|ini|ku))?"
_REALLY = r"(?: (?:gak|nggak|tidak|enggak|ga|engga|kah))?"


# what an assistant is asked all day (everyday.py): each of these used to be a guess by the small model, or seconds of the big one
EVERYDAY = [
    # ── a greeting ──
    (r"(?:halo+|hallo+|hai+|hei|hey|hi)(?: digidaw| lagi)?", lambda m: ("small_talk", {"kind": "hello"})),
    (r"(?:selamat )?(pagi|siang|sore|malam)(?: digidaw)?", lambda m: ("small_talk", {"kind": "part", "said": m[1]})),
    (r"(?:apa kabar|gimana kabar(?:nya|mu)?|kabar(?:mu|nya)? (?:gimana|bagaimana))(?: digidaw)?", lambda m: ("small_talk", {"kind": "how"})),
    (r"(?:makasih|terima ?kasih|thanks|thank you)(?: banyak| digidaw)*", lambda m: ("small_talk", {"kind": "thanks"})),
    (r"(?:wah |kamu |digidaw )?(?:keren|hebat|mantap|pintar|pinter|bagus)(?: banget| sekali| juga| ya)*(?: digidaw)?", lambda m: ("small_talk", {"kind": "praise"})),
    # ── what was said ──
    (r"(?:tadi )?(?:kamu |anda |digidaw )?(?:bilang|ngomong|berkata) apa(?: tadi)?|apa tadi|ulangi(?: lagi)?|coba ulangi|bisa (?:di)?ulangi|"
     r"ulangi (?:jawaban|yang tadi)(?:nya)?|(?:kurang|nggak|gak|tidak|ga) (?:dengar|kedengaran|jelas tadi)", lambda m: ("repeat_reply", {})),
    (r"(?:apa )?perintah (?:saya |aku )?(?:terakhir|tadi)(?: apa)?", lambda m: ("last_command", {})),
    (r"(?:kamu |anda |digidaw )?bisa apa(?: saja| aja)?|apa (?:saja |aja )?yang bisa (?:kamu|anda|digidaw) (?:lakukan|kerjakan|bantu)|bantuan|help|"
     r"(?:fitur|kemampuan)(?:mu| kamu| digidaw)?(?: apa saja| apa aja)?|perintah apa saja(?: yang (?:ada|bisa))?", lambda m: ("what_i_can_do", {})),
    (r"batal|batalkan|batalin|cancel|(?:tidak|nggak|gak|enggak|ga|ngga) jadi|gajadi", lambda m: ("undo_last_alarm", {}) if alarms.just_added() else ("say", {"text": f"Baik, {sc.T}, tidak jadi"})),
    # ── music ──
    (r"(?:volume|suara)(?:nya)?(?: sekarang| saat ini)? berapa(?: sekarang)?|berapa volume(?:nya)?(?: sekarang)?|cek volume", lambda m: ("get_volume", {})),
    (r"(?:musik|lagu|video)(?:nya)? berhenti|berhentikan (?:musik|lagu|video)(?:nya)?", lambda m: ("media_control", {"action": "pause"})),
    (r"(?:ulang|ulangi|putar ulang|mulai ulang)(?: lagi)? (?:lagu|musik|video)(?:nya)?(?: ini)?(?: dari awal)?|(?:ulang|ulangi)(?: lagu)? dari awal|"
     r"(?:putar|mulai)(?: lagi)? dari awal|ulang lagunya", lambda m: ("media_control", {"action": "restart"})),
    (rf"(?:mundur|mundurkan|balik) {_AMOUNT} (detik|menit)", lambda m: _seek("rewind", m[1], m[2])),
    (rf"(?:maju|majukan|lompat|skip|lewati|percepat) {_AMOUNT} (detik|menit)", lambda m: _seek("forward", m[1], m[2])),
    (r"(?:ulang|ulangi) (?:lagu ini )?terus|loop(?: lagu| video)?(?: ini)?|putar berulang", lambda m: ("media_control", {"action": "loop"})),
    (r"(?:ini )?lagu siapa(?: ini)?|siapa (?:yang )?(?:nyanyi|penyanyi(?:nya)?|menyanyikan)(?: lagu ini| ini)?|lagu ini siapa(?: yang (?:nyanyi|menyanyikan))?",
     lambda m: ("now_playing", {})),
    (r"(?:simpan|tandai) (?:lagu|musik)(?: ini)?(?: (?:ke|sebagai|jadi) (?:favorit|kesukaan))?|favoritkan(?: lagu)?(?: ini)?|tambah(?:kan)?(?: lagu(?: ini)?)? ke favorit|"
     r"(?:aku|saya) suka lagu ini", lambda m: ("like_song", {})),
    (r"(?:layar penuh|fullscreen|full screen)(?:kan)?(?: video| lagu)?|keluar(?: dari)? (?:layar penuh|fullscreen)|perbesar video", lambda m: _fullscreen()),
    (r"(?:putar|setel|mainkan|lanjutkan) (?:lagu|musik)(?: yang)? terakhir|lanjutkan (?:lagu|musik)(?: yang)? (?:tadi|sebelumnya)", lambda m: ("media_control", {"action": "play"})),
    (r"(?:matikan|hentikan|stop|pause) (?:musik|lagu)(?:nya)? (\d+) (menit|jam) lagi",
     lambda m: ("sleep_timer", {"minutes": int(m[1]) * (60 if m[2] == "jam" else 1), "target": "music"})),
    # ── notes ──
    (r"(?:tambah|tambahkan)(?: ke)? catatan (.{2,300})", lambda m: ("append_to_last_note", {"content": m[1], "show": False})),
    (r"(?:baca|bacakan)(?: lagi)? catatan(?: (?:terakhir|saya|ku|tadi))?|catatan terakhir(?: saya)?(?: apa)?|apa catatan (?:terakhir|tadi)(?: saya)?",
     lambda m: ("read_last_note", {})),
    (r"catatan (?:saya|ku)(?: apa saja| apa aja)?|(?:daftar|lihat|cek) catatan(?: saya)?|ada catatan apa(?: saja| aja)?", lambda m: ("list_notes", {})),
    (r"(?:buka|tampilkan) catatan(?: (?:yang )?(?:terakhir|saya|tadi))?", lambda m: ("open_last_note", {})),
    (r"(?:hapus|buang) catatan(?: (?:terakhir|tadi|ini))?", lambda m: ("delete_last_note", {})),
    (r"(?:catat|catatkan|tulis catatan|buat catatan|bikin catatan)(?: ini| bahwa| kalau)? (.{2,300})", lambda m: ("write_note", {"content": m[1], "show": False})),
    # ── the machine ──
    (r"(?:cek |berapa |lihat )?(?:sisa |kapasitas |ruang )?(?:penyimpanan|storage|disk|hardisk|hard disk|ssd)(?: (?:saya|laptop|ku))?(?: (?:sisa|tersisa|berapa|sekarang|masih))*",
     lambda m: ("disk_space", {})),
    (rf"(?:sudah )?berapa lama {_LAPTOP} (?:menyala|nyala|hidup|aktif)|uptime", lambda m: ("uptime", {})),
    (rf"(?:aplikasi|proses|program) apa(?: saja| aja)? yang (?:paling )?(?:berat|makan ram|makan cpu|boros)|apa yang (?:bikin|membuat) {_LAPTOP} (?:lambat|lemot|berat|panas|lelet)|"
     rf"kenapa {_LAPTOP} (?:lambat|lemot|berat|lelet)|(?:cek |lihat )?(?:pemakaian|penggunaan) (?:aplikasi|proses)|"
     r"(?:apa |berapa |cek |lihat |siapa )?(?:pemakaian|penggunaan|pemakai|pengguna|konsumsi) (?:ram|memori|memory|cpu|prosesor)(?: yang)?(?: paling)? (?:terbesar|terbanyak|tertinggi|terberat|besar|banyak|tinggi|boros)|"
     r"(?:apa|siapa)(?: saja| aja)?(?: yang)?(?: paling)? (?:makan|memakan|menghabiskan|boros) (?:ram|memori|memory|cpu)", lambda m: ("heavy_apps", {})),
    (r"tutup(?: (?:aplikasi|program|proses))?(?: yang)?(?: paling)? berat", lambda m: ("close_heaviest", {})),
    (rf"(?:tes|test|cek|ukur)(?: kecepatan)? (?:internet|koneksi|wi ?fi)(?: saya)?(?<!cek internet)(?<!cek koneksi)(?<!cek wifi)|speed ?test|(?:berapa )?kecepatan internet(?: saya| berapa)?|"
     rf"internet(?:nya)? (?:lambat|lemot|kencang|cepat){_REALLY}", lambda m: ("internet_speed", {})),
    (rf"(?:berapa |apa |cek )?(?:alamat )?ip(?: (?:saya|laptop|ku|publik|lokal))?(?: berapa| apa)?|(?:nama )?wi ?fi(?: (?:saya|ku))?(?: apa| yang (?:dipakai|tersambung))?(?<!^wifi)(?<!^wi fi)|"
     rf"(?:pakai|tersambung ke|konek ke) wi ?fi apa|cek (?:internet|koneksi|jaringan)|internet(?:nya)? (?:nyala|jalan|aman|tersambung){_REALLY}", lambda m: ("get_network_info", {})),
    (r"(?:kamu |anda )?(?:bisa|dapat) apa(?: saja)?|apa (?:yang )?(?:kamu|anda) bisa(?: lakukan)?|apa saja (?:kemampuan(?:mu| kamu| anda)?|yang (?:kamu|anda) bisa(?: lakukan)?)",
     lambda m: ("what_i_can_do", {})),
    (r"(?:apa )?(?:aplikasi|program) (?:apa )?(?:yang )?(?:paling )?(?:berat|boros)(?: sekarang)?|kenapa laptop(?:nya)? (?:lambat|lemot|berat)", lambda m: ("heavy_apps", {})),
    (r"(?:berapa )?isi (?:tempat sampah|recycle bin|sampah)|tempat sampah(?:nya)? (?:isinya )?berapa", lambda m: ("recycle_bin_status", {})),
    (r"apakah (.{2,50}?) (?:masih )?(?:berjalan|jalan|terbuka|kebuka|dibuka)(?: sekarang)?",
     lambda m: ("apps_open", {"names": m[1]}) if not re.search(r"\b(?:lampu|internet|wifi|wi fi|timer|alarm|musik|lagu|update)\b", m[1]) else None),
    (r"(?:daftar|apa saja) (?:pengawasan|pantauan)|apa (?:saja )?yang (?:sedang )?(?:kamu|anda) pantau", lambda m: ("list_watches", {})),
    (r"batalkan (?:pengawasan|pantauan)(?: (.{2,40}))?", lambda m: ("cancel_watch", {"what": m[1] or ""})),
    (r"daftar (?:rutinitas|protokol)|(?:rutinitas|protokol) apa saja", lambda m: ("list_protocols", {})),
    (r"(?:bersihkan|hapus|lupakan) (?:semua )?notifikasi", lambda m: ("clear_notifications", {})),
    (r"buka hasil(?: yang)? pertama", lambda m: ("open_first_result", {})),
    (r"(?:mulai )?(?:mode )?dikte", lambda m: ("start_dictation", {})),
    (r"(?:sukai|like) lagu(?: ini)?", lambda m: ("like_song", {})),
    (r"(?:status|progres|progress) (?:update|pembaruan)(?: sistem)?|(?:update|pembaruan)(?:nya)?(?: sistem)?(?: sudah)? sampai mana|sampai mana (?:update|pembaruan)(?:nya)?", lambda m: ("update_status", {})),
    (r"(?:update|perbarui|upgrade)(?: sistem)? (?:lewat|di|pakai) terminal", lambda m: ("update_in_terminal", {})),
    (r"(?:update|perbarui|upgrade) (?:paket )?(?:aur|yay)", lambda m: ("update_aur", {})),
    (r"(?:cek|ada|lihat) (?:update|pembaruan)(?: (?:sistem|gak|nggak|apa saja|apa aja))*|update apa saja", lambda m: ("check_updates", {})),
    (rf"update|(?:update|perbarui|upgrade) (?:sistem|arch|{_LAPTOP})|(?:jalankan|lakukan) update(?: sistem)?", lambda m: ("system_update", {})),
    (r"(?:kecerahan|brightness)(?: layar)?(?: sekarang)? berapa|berapa kecerahan(?: layar)?", lambda m: ("get_brightness", {})),
    (r"(?:naikkan|tingkatkan|tambah|besarkan) (?:kecerahan|brightness)(?: layar)?|terangkan layar|layar(?:nya)? (?:lebih|agak) terang", lambda m: ("brightness_step", {"change": 15})),
    (r"(?:turunkan|kurangi|kecilkan) (?:kecerahan|brightness)(?: layar)?|(?:redupkan|gelapkan) layar|layar(?:nya)? (?:lebih|agak) (?:redup|gelap)", lambda m: ("brightness_step", {"change": -15})),
    (r"(?:lebih|agak|sedikit|dikit) (terang|redup|gelap)(?:an|in|kan)?(?: lagi| sedikit| dikit)?", lambda m: ("brightness_step", {"change": 15 if m[1] == "terang" else -15})),
    (r"(?:(?:nyalakan|aktifkan|pakai) )?(?:mode|tema|tampilan) gelap|dark mode", lambda m: ("dark_mode", {"on": True})),
    (r"(?:(?:pakai|aktifkan) )?(?:mode|tema|tampilan) terang|light mode|matikan (?:mode|tema) gelap", lambda m: ("dark_mode", {"on": False})),
    (r"(?:bersihkan|kosongkan|buang|hapus)(?: isi)? (?:sampah|recycle bin|tempat sampah|trash)", lambda m: ("empty_recycle_bin", {})),
    (r"(?:bersihkan|hapus) (?:file (?:sementara|temp|sampah)|cache|temp)", lambda m: ("clean_temp_files", {})),
    (rf"(?:apakah |apa )?bluetooth(?:nya)? (?:nyala|hidup|aktif|mati|tersambung){_REALLY}|(?:status|cek) bluetooth", lambda m: ("bluetooth", {"action": "status"})),
    (r"(?:sambungkan|hubungkan|konek\w*|connect) bluetooth", lambda m: ("bluetooth", {"action": "connect"})),
    (r"(?:sambungan|sambungkan|sambungin|hubungkan|konekin|konek|connect)(?: ke)? (keyboard|mouse|headset|earphone|earbuds|tws|speaker)(?: bluetooth)?(?: .{1,20})?",
     lambda m: ("bluetooth", {"action": "connect", "device": m[1]})),
    (r"(?:situs|web|website|halaman|tab) apa ini|ini (?:situs|web|website) apa", lambda m: ("active_window_info", {})),
    # ── the window in front, and what is typed in it ──
    (r"tutup (?:jendela )?ini|tutup jendela(?: aktif)?|close window", lambda m: ("window_action", {"action": "close"})),
    (r"tutup (?:lagi|itu|dia|barusan|(?:yang|aplikasi) (?:tadi|barusan|itu))", lambda m: ("close_application", {"name": _that_app("opened")}) if _that_app("opened")
     else ("window_action", {"action": "close"})),
    (r"buka (?:lagi|kembali)(?: (?:yang|aplikasi) tadi)?|buka (?:yang|aplikasi) tadi(?: lagi)?", lambda m: ("open_application", {"name": _that_app("closed") or _that_app("opened")})
     if _that_app("closed") or _that_app("opened") else None),
    (r"minimize|minimalkan|perkecil(?: ini)?|(?:perkecil|kecilkan|sembunyikan|minimize|minimalkan) jendela(?: ini)?", lambda m: ("window_action", {"action": "minimize"})),
    (r"maximize|maksimalkan|perbesar(?: ini)?|(?:perbesar|besarkan|maximize|maksimalkan) jendela(?: ini)?", lambda m: ("window_action", {"action": "maximize"})),
    (r"pulihkan jendela(?: ini)?|kembalikan ukuran jendela", lambda m: ("window_action", {"action": "restore"})),
    (r"jendela (?:apa )?(?:yang )?aktif(?: apa)?|sekarang di (?:aplikasi|jendela) apa|aplikasi apa yang (?:sedang )?aktif", lambda m: ("active_window_info", {})),
    (r"(?:jendela|aplikasi|window|program) apa(?: saja| aja)? yang (?:sedang )?(?:terbuka|dibuka|jalan|berjalan)(?: sekarang)?", lambda m: ("list_windows", {})),
    (r"salin(?: (?:teks|ini|itu|tulisan)(?: ini)?)?|copy(?: ini)?|kopi ini", lambda m: ("edit_action", {"action": "copy"})),
    (r"potong(?: (?:teks|ini|itu))?|cut(?: ini)?", lambda m: ("edit_action", {"action": "cut"})),
    (r"tempel(?:kan)?(?: (?:di sini|ini|teks|(?:salinan |yang )?tadi))?|paste(?:kan)?(?: (?:di sini|(?:yang )?tadi))?", lambda m: ("edit_action", {"action": "paste"})),
    (r"undo|urungkan|batalkan ketikan", lambda m: ("edit_action", {"action": "undo"})),
    (r"redo", lambda m: ("edit_action", {"action": "redo"})),
    (r"simpan(?: (?:file|ini|dokumen)(?: ini)?)?|save", lambda m: ("edit_action", {"action": "save"})),
    (r"pilih semua|select all|blok semua", lambda m: ("edit_action", {"action": "select_all"})),
    (r"(?:hapus|bersihkan) (?:teks|tulisan|ketikan)(?: ini| yang ini)?", lambda m: ("edit_action", {"action": "clear"})),
    (r"(?:tekan |pencet )?(ctrl|control|alt|shift|super)(?: ?\+? ?(ctrl|control|shift|alt))? ?\+? ?([a-z0-9]|f\d{1,2}|enter|tab)",
     lambda m: ("press_keys", {"keys": "+".join(k for k in (m[1], m[2], m[3]) if k)})),
    (r"(?:baca|bacakan|apa isi|isi|lihat|cek) clipboard(?:nya| saya)?|clipboard(?:nya)? isinya apa|apa yang (?:saya|aku) (?:salin|copy)(?: tadi)?", lambda m: ("get_clipboard", {})),
    (r"(?:lihat |baca |cek |daftar )?riwayat clipboard|apa saja yang (?:saya|aku) (?:salin|copy)(?: tadi)?", lambda m: ("clipboard_history", {})),
    (r"(?:tempel|paste)(?:kan)? (?:salinan |clipboard )?(?:yang )?(?:sebelumnya|kedua|ke-?2)", lambda m: ("paste_previous", {"index": 2})),
    (r"(?:tempel|paste)(?:kan)? (?:salinan |clipboard )?(?:yang )?(?:ketiga|ke-?3)", lambda m: ("paste_previous", {"index": 3})),
    (r"(?:salin|copy|ambil) (?:semua )?(?:teks|tulisan) (?:di |dari )?layar|ocr(?: layar)?", lambda m: ("copy_screen_text", {})),
    # ("port" comes back from Google as "power", "faktor" or "chord": all heard on the first day of asking, 6 Nov)
    (r"(?:port|faktor|chord|kord|power) (?:apa|mana)(?: saja| aja)? (?:yang )?terbuka(?: (?:sekarang|saat ini))?|"
     r"port (?:apa|mana)(?: saja| aja)? (?:yang )?(?:aktif|dibuka|dipakai|jalan|berjalan)(?: (?:sekarang|saat ini))?|"
     r"(?:daftar|cek|lihat|tampilkan|sebutkan) port(?: (?:yang )?(?:terbuka|aktif))?|port (?:yang )?terbuka|ada port apa(?: saja| aja)?",
     lambda m: ("open_ports", {})),
    (r"(?:siapa|proses apa|aplikasi apa|apa) (?:yang )?(?:pakai|memakai|menggunakan|jalan di) (?:port |power |faktor )?(\d{2,5})|cek port (\d{2,5})|port (\d{2,5}) dipakai (?:siapa|apa)",
     lambda m: ("port_process", {"port": int(m[1] or m[2] or m[3])})),
    (r"(?:matikan|hentikan|tutup|kill|bebaskan) (?:proses (?:di )?)?port (\d{2,5})", lambda m: ("port_process", {"port": int(m[1]), "kill": True})),
    (r"(?:cek |lihat )?status (?:service|servis|layanan) ([\w@.-]+)|(?:apakah )?(?:service|servis|layanan) ([\w@.-]+) (?:jalan|berjalan|aktif|nyala|mati)",
     lambda m: ("service_control", {"name": m[1] or m[2], "action": "status"})),
    (r"(?:nyalakan|hidupkan|jalankan|start|mulai) (?:service|servis|layanan) ([\w@.-]+)", lambda m: ("service_control", {"name": m[1], "action": "start"})),
    (r"(?:matikan|hentikan|stop) (?:service|servis|layanan) ([\w@.-]+)", lambda m: ("service_control", {"name": m[1], "action": "stop"})),
    (r"(?:restart|mulai ulang) (?:service|servis|layanan) ([\w@.-]+)", lambda m: ("service_control", {"name": m[1], "action": "restart"})),
    (r"(?:jelaskan|apa|kenapa) error (?:ini|tadi|terakhir|di layar|di terminal)|error apa (?:ini|tadi)|ini error apa|kenapa (?:perintah|terminal)(?:nya)? (?:tadi )?(?:error|gagal)|bantu(?: perbaiki)? error (?:ini|di layar)",
     lambda m: ("explain_last_error", {})),
    (r"(?:baca|bacakan) (?:teks )?yang (?:saya |aku )?(?:pilih|blok|sorot)", lambda m: ("get_selected_text", {})),
    (r"(?:mulai )?rekam layar|rekam(?: layar)? sekarang", lambda m: ("screen_record", {"action": "start"})),
    (r"(?:hentikan|stop|selesai|matikan|berhenti) (?:rekaman|rekam|merekam)(?: layar)?", lambda m: ("screen_record", {"action": "stop"})),
    # ── time ──
    (r"(?:(?:mulai|nyalakan|start|jalankan) )?stopwatch", lambda m: ("stopwatch", {"action": "start"})),
    (r"(?:stop|setop|hentikan|matikan) stopwatch", lambda m: ("stopwatch", {"action": "stop"})),
    (r"stopwatch(?:nya)? (?:berapa|sudah berapa)|berapa stopwatch(?:nya)?|cek stopwatch", lambda m: ("stopwatch", {"action": "read"})),
    (r"(?:hari apa|tanggal berapa|hari|tanggal) (besok|lusa|kemarin)|(besok|lusa|kemarin) (?:hari apa|tanggal berapa)", lambda m: ("get_date", {"when": m[1] or m[2]})),
    (r"(?:sekarang )?bulan (?:apa|berapa)(?: (?:sekarang|ini))?", lambda m: ("get_date", {"when": "bulan"})),
    (r"(?:sekarang )?tahun (?:berapa|apa)(?: (?:sekarang|ini))?", lambda m: ("get_date", {"when": "tahun"})),
    (r"berapa hari lagi(?: (?:ke|sampai|menuju))? (.{3,40})", lambda m: ("days_until", {"when": m[1]}) if agenda.parse_date(m[1])[0] else None),
]


# messages and notifications
MESSAGES = [
    # "Mau saya bacakan?" after a message was announced (or the welcome back): "ya" / "bacakan" reads the messages
    (r"(?:ya|iya|iyah|yap|yes|boleh|oke|ok|silakan|silahkan|baik|sip)(?: (?:bacakan|bacain|baca|silakan|boleh|saja|aja))*",
     lambda m: _read_offered()),
    (r"(?:coba )?(?:bacakan|bacain|baca)(?: (?:saja|aja|pesannya|semuanya|semua))*|apa isinya|isinya apa",
     lambda m: _read_offered() or _read_unread()),
    # notifications that came in (WhatsApp etc.): who sent it and what it says
    (r"(?:tolong )?(?:cek|lihat|baca|bacakan|bacain|periksa|tampilkan|kode|cok|cik|check|jek)(?: semua)? (?:pesan|chat)(?: yang)?(?: (?:masuk|baru|terbaru|tadi))?"
     r"(?: (?:di |dari )?(whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: (?:ada )?apa(?: saja)?)?|"
     r"(?:apakah |apa )?ada (?:pesan|chat)(?: (?:baru|masuk|baru masuk))?(?: (?:di |dari )?(whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: tidak| nggak| gak)?|"
     r"(?:apakah |apa )?ada (?:pesan|chat) (whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram)(?: (?:baru|masuk))?",
     lambda m: ("read_notifications", {"messages_only": True, "app": next((g for g in m.groups() if g), "")})),
    (r"(?:tolong )?(?:cek|lihat|baca|bacakan|bacain|periksa|tampilkan)(?: semua)? (?:notifikasi|notif)(?: (?:yang )?(?:masuk|baru|tadi))?"
     r"(?: (?:di |dari )?(whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: (?:ada )?apa(?: saja)?)?|"
     r"(?:apakah |apa )?ada (?:notifikasi|notif)(?: (?:baru|masuk))?(?: (?:di |dari )?(whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?(?: tidak| nggak| gak)?|"
     r"notifikasi apa(?: saja)?(?: yang masuk)?",
     lambda m: ("read_notifications", {"app": next((g for g in m.groups() if g), "")})),
    (r"(?=.*(?:ulang|lagi|tadi|itu))(?:baca|bacakan|bacain|ulangi)(?: ulang| lagi)?(?: (?:pesan|notifikasi|notif))?(?: (?:tadi|itu))?(?: lagi)?"
     r"(?: (?:dari |di )?(whatsapp bisnis|whatsapp business|wa bisnis|wa business|wasistlos|whatsapp|wa|telegram|signal|discord|slack|gmail|email|instagram))?",
     lambda m: ("read_notifications", {"include_read": True, "app": next((g for g in m.groups() if g), "")})),
    (r"(?:sekarang )?(?:jam|pukul|gak|tidak|nggak|enggak) berapa(?: sekarang)?|jam|waktu|jam sekarang|sekarang jam(?: berapa)?",
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
    (r"(?:hari ini |sekarang |ini )?(?:tanggal|hari) (?:berapa|apa)(?: (?:hari ini|sekarang|ini))?", lambda m: ("get_date", {})),
]


# volume and music
SOUND = [
    # per-app volume and system microphone (before general volume so "volume discord 30" hits app_volume)
    (r"(?:(?:atur|set|ubah|setel|kecilkan|besarkan|naikkan|turunkan|pelankan|keraskan) )?(?:volume|suara) (?:whatsapp|wa) (?:bisnis|business) (?:ke |jadi |menjadi |di |pada )?(\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("app_volume", {"app": "wasistlos", "level": int(m[1])})),
    (r"(?:bisukan|mute|matikan) (?:suara|volume|audio) (?:whatsapp|wa) (?:bisnis|business)", lambda m: ("app_volume", {"app": "wasistlos", "action": "mute"})),
    (r"(?:nyalakan|hidupkan|unmute|bunyikan) (?:lagi )?(?:suara|volume|audio) (?:whatsapp|wa) (?:bisnis|business)(?: lagi)?",
     lambda m: ("app_volume", {"app": "wasistlos", "action": "unmute"})),
    (r"(?:(?:atur|set|ubah|setel|kecilkan|besarkan|naikkan|turunkan|pelankan|keraskan) )?(?:volume|suara) (?!ke\b|jadi\b|menjadi\b|sampai\b|hingga\b|di\b|pada\b|naik\b|turun\b|lebih\b|mikrofon\b|mic\b|mike\b|sistem\b|laptop\b)(\w{2,20}) (?:ke |jadi |menjadi |di |pada )?(\d{1,3})(?: ?(?:persen|%))?",
     lambda m: _app_sound(m[1], level=int(m[2]))),
    (r"(?:bisukan|mute|matikan) (?:suara|volume|audio) (?!mikrofon\b|mic\b|mike\b)(\w{2,20})|(?:suara|volume) (?!mikrofon\b|mic\b|mike\b)(\w{2,20}) (?:dibisukan|mute|mati)",
     lambda m: _app_sound(m[1] or m[2], action="mute")),
    (r"(?:nyalakan|hidupkan|unmute|bunyikan) (?:lagi )?(?:suara|volume|audio) (?!mikrofon\b|mic\b|mike\b)(\w{2,20})(?: lagi)?",
     lambda m: _app_sound(m[1], action="unmute")),
    (r"(?:naikkan|besarkan|keraskan|tambah) (?:volume|suara) (?!mikrofon\b|mic\b|mike\b)(\w{2,20})",
     lambda m: _app_sound(m[1], action="up")),
    (r"(?:turunkan|kecilkan|pelankan|kurangi) (?:volume|suara) (?!mikrofon\b|mic\b|mike\b)(\w{2,20})",
     lambda m: _app_sound(m[1], action="down")),
    (r"(?:berapa |cek )?(?:volume|suara) (?!sekarang\b|saat\b|mikrofon\b|mic\b|mike\b)(\w{2,20})(?: berapa| sekarang)+",
     lambda m: _app_sound(m[1])),
    (r"(?:bisukan|mute|matikan|nonaktifkan) (?:mikrofon|mic|mike)(?: sistem| laptop| saya)?",
     lambda m: ("mic_control", {"action": "mute"})),
    (r"(?:nyalakan|hidupkan|aktifkan|unmute) (?:lagi )?(?:mikrofon|mic|mike)(?: sistem| laptop| saya)?(?: lagi)?",
     lambda m: ("mic_control", {"action": "unmute"})),
    (r"(?:(?:atur|set|ubah|setel) )?(?:volume|suara) (?:mikrofon|mic|mike) (?:ke |jadi |di )?(\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("mic_control", {"level": int(m[1])})),
    (r"(?:status|cek) (?:mikrofon|mic|mike)|(?:mikrofon|mic)(?:nya)? (?:nyala|mati|aktif|dibisukan)(?: (?:gak|nggak|tidak))?",
     lambda m: ("mic_control", {"action": "status"})),
    # "volume 50" / "naikkan volume ke 80" set a level; "turunkan volume 5 persen" (no "ke") lowers BY 5 (below)
    (r"(?:(?:atur|set|ubah|setel) )?(?:volume|suara)(?:nya)? (?:ke |jadi |menjadi |sampai |hingga |di |pada )?(\d{1,3})(?: ?(?:persen|%))?|"
     r"(?:naikkan|turunkan|kecilkan|besarkan|keraskan|pelankan|kurangi|tambah) (?:volume|suara)(?:nya)? "
     r"(?:ke|jadi|menjadi|sampai|hingga) (\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("set_volume", {"level": int(m[1] or m[2])})),
    (r"(?:naikkan|naikin|besarkan|besarin|gedein|gedekan|keraskan|kerasin|kencangkan|kencengin|tambah(?:kan)?) (?:volume|suara)(?:nya)?(?: (?:sebanyak |sebesar )?(\d{1,3}) ?(?:persen|%)?)?(?: lagi| dikit| sedikit)?"
     r"|(?:volume|suara) (?:naik|lebih (?:keras|besar|kencang))(?: (\d{1,3}) ?(?:persen|%)?)?(?: lagi)?",
     lambda m: ("volume_up", {"amount": int(m[1] or m[2] or 10)})),
    (r"(?:turunkan|turunin|kecilkan|kecilin|pelankan|pelanin|kurangi|kurangin) (?:volume|suara)(?:nya)?(?: (?:sebanyak |sebesar )?(\d{1,3}) ?(?:persen|%)?)?(?: lagi| dikit| sedikit)?"
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
    (r"next|skip|lewati|(?:ganti|pindah)(?: ke)? (?:lagu|musik|music|video)(?:nya)?(?: (?:yang )?lain)?|(?:lagu|musik|music) (?:yang )?lain(?:nya)?|(?:lagu|musik|music|video) (?:selanjutnya|berikutnya)", lambda m: ("media_control", {"action": "next"})),
    (r"(?:mau|pengen|pingin|ingin|kepengen) (?:denger|dengar|dengerin|mendengarkan|nonton)(?: (?:lagu|musik|video))? (.{2,60})", lambda m: _play(m[1])),
    (r"previous|(?:lagu|musik|music|video)(?: yang)? sebelumnya|(?:putar|putarkan|setel|mainkan|balik ke|kembali ke)(?: lagi)?(?: (?:lagu|musik|music|video))?(?: yang)? (?:sebelumnya|tadi)|(?:lagu|musik|music)(?: yang)? tadi",
     lambda m: ("media_control", {"action": "previous"})),
    (r"(?:ini )?lagu apa(?: ini)?|judul lagu(?: ini|nya)?(?: apa)?|lagu apa yang (?:sedang )?(?:diputar|main)",
     lambda m: ("now_playing", {})),
    (r"(?:putar|putarkan|play|setel|mainkan) (?:playlist|lagu-lagu|mix) (.+)", lambda m: _play(m[1], mix=True)),
    # "putar youtube / putar musik / lanjutkan musik": resume (or pick up the last music), not a search for "youtube"
    (r"(?:putar|putarkan|play|setel|mainkan|lanjutkan|lanjut)(?: lagi)? (?:pemutaran )?(?:youtube|musik(?:nya)?|lagu(?:nya)?|video(?:nya)?)"
     r"(?: (?:di )?youtube)?(?: lagi)?", lambda m: ("media_control", {"action": "play"})),
    # "putar musik tradisional": "musik" stays in the search (a genre); "putar musik I was never there": a title, and the extra
    # word found the wrong song twice; "putar lagu X": X is the title
    (r"(?:putar|putarkan|play|setel|mainkan) (lagu|musik|video) (.+?)(?: di youtube)?",
     lambda m: _play(re.sub(r"^(?:dari|oleh|by) ", "", m[2]) if re.match(r"(?:dari|oleh|by) ", m[2])
                     else f"musik {m[2]}" if m[1] == "musik" and _GENRE_RE.match(m[2]) else m[2])),
    (r"(?:putar|putarkan|setel|mainkan) (?!(?:lagu|musik|video|playlist)$)(.{3,}?)(?: di youtube)?", lambda m: _play(m[1])),
    (r"(?:ambil |tangkap )?(?:screenshot|tangkapan layar|tangkap layar)(?: layar)?", lambda m: ("take_screenshot", {})),
    (r"buka kunci(?: (?:komputer|laptop|pc|layar))?", lambda m: ("unlock_screen", {})),
    (r"kunci (?:komputer|laptop|pc|layar)", lambda m: ("lock_workstation", {})),
]


SCRIPT_WORD = r"(?:skrip|script|scrip|sekrip)"
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
    (r"(?:atur |ubah |ganti |set )?(?:power|daya|mode daya)", lambda m: ("power_mode", {})),
    (r"(?:atur |ubah |ganti |set |\w+ )?(?:power|daya|mode daya)(?: ke| jadi)?(?: mode)? (hemat(?: daya| baterai| batre)?|seimbang|performa(?: tinggi)?)",
     lambda m: ("power_mode", {"mode": m[1]})),
    (r"(?:pakai |aktifkan |ubah ke |ganti ke |set )?mode (hemat(?: daya| baterai| batre)?|seimbang|performa(?: tinggi)?|gaming|game)",
     lambda m: ("power_mode", {"mode": m[1]})),
    (r"(?:matikan|nonaktifkan)(?: mode)? jangan ganggu", lambda m: ("do_not_disturb", {"on": False})),
    (r"(?:aktifkan |nyalakan |pakai )?(?:mode )?jangan ganggu", lambda m: ("do_not_disturb", {"on": True})),
    # "matikan notifikasi" is the opposite of "matikan jangan ganggu": notifications off = do not disturb ON
    (r"(?:matikan|nonaktifkan|senyapkan|diamkan|bisukan) (?:semua )?(?:notifikasi|notif)(?: (?:dulu|semua))?",
     lambda m: ("do_not_disturb", {"on": True})),
    (r"(?:notifikasi|notif)(?:nya)? (?:diem|diam|senyap|mati|matiin|matikan)(?:in|kan)?(?: dulu)?", lambda m: ("do_not_disturb", {"on": True})),
    (r"(?:nyalakan|hidupkan|aktifkan|bunyikan) (?:lagi )?(?:semua )?(?:notifikasi|notif)(?: lagi)?",
     lambda m: ("do_not_disturb", {"on": False})),
    # the user's "fanset 50" (Google also writes "fan set 50"), and the same in words
    (r"(?:fan ?set|van ?set|pan ?set|(?:atur |set |setel |ubah |ganti |naikkan |turunkan |kecilkan |besarkan )?(?:kecepatan )?"
     r"(?:kipas|fan)(?:nya)?(?: laptop)?(?: ke| jadi| di)?) (\d{1,3})(?: ?persen| ?%)?",
     lambda m: ("fan_speed", {"percent": int(m[1])})),
    (r"(?:atur |set |setel |ubah |ganti )?(?:kecepatan )?(?:kipas|fan)(?:nya)?(?: laptop)?(?: ke| jadi| di)? (?:maksimal|maksimum|penuh|full)",
     lambda m: ("fan_speed", {"percent": 100})),
    (r"(?:fan ?set |(?:atur |set |setel |ubah |ganti |kembalikan |balikin )?(?:kecepatan )?(?:kipas|fan)(?:nya)?(?: laptop)?(?: ke| jadi| di)? )"
     r"(?:otomatis|auto)", lambda m: ("fan_speed", {"auto": True})),
    (r"(?:matikan|padamkan) kipas(?:nya)?(?: laptop)?", lambda m: ("fan_speed", {"percent": 0})),
    (r"(?:cek |lihat |berapa )?(?:kecepatan |status )(?:kipas|fan)(?:nya)?(?: laptop)?(?: berapa| sekarang)*|"
     r"kipas(?:nya)?(?: laptop)? berapa(?: persen)?(?: sekarang)?", lambda m: ("fan_speed", {})),
    # the user's scripts in ~/myskrip: by name, or the fzf manager behind Super+S
    (rf"(?:jalankan|jalanin|eksekusi|run|buka) {SCRIPT_WORD} (?!saya$|ku$)(.{{2,40}})", lambda m: ("run_script", {"name": m[1]})),
    (r"(?:jalankan|jalanin|eksekusi|run) (.{2,40})", lambda m: ("run_script", {"name": m[1]}) if pc_control.is_my_script(m[1]) else None),
    # "terangkan bersih": "jalankan" misheard (a lamp verb otherwise, and that sentence has no lamp in it), so it asks
    (r"terangkan (.{2,40})", lambda m: ("run_script", {"name": m[1], "unsure": True}) if pc_control.is_my_script(m[1]) else None),
    # Google hears "skrip" as "SC" and "screen": not on its own, only with a verb that says what's meant
    (r"(?:buka|jalankan|jalanin) (?:sc|screen)", lambda m: ("open_script_manager", {})),
    (rf"tutup(?: jendela)? (?:{SCRIPT_WORD}|sc|screen)(?: saya|ku|nya)?", lambda m: ("close_script_window", {})),
    (rf"(?:buka|tampilkan|lihat|jalankan)?(?: ?(?:pengelola|manajer|manager|menu|daftar))? ?{SCRIPT_WORD}(?: saya|ku)?",
     lambda m: ("open_script_manager", {})),
    (r"(?:pindahkan |pindah |alihkan |ubah |ganti )?(?:suara|audio|keluaran suara)(?:nya)? ke (.{2,30})",
     lambda m: ("audio_output", {"name": m[1]})),
    (r"(?:matikan|padamkan|offkan) (?:layar|monitor)(?: laptop)?", lambda m: ("screen_power", {"on": False})),
    (r"(?:nyalakan|hidupkan|aktifkan|onkan) (?:layar|monitor)(?: laptop)?", lambda m: ("screen_power", {"on": True})),
    (r"(?:hentikan|batalkan|stop|setop) tugas(?:nya)?(?: (?:di )?layar)?", lambda m: ("stop_task", {})),
    (r"lanjutkan tugas(?:nya)?", lambda m: ("resume_task", {})),
    (r"(?:apa )?isi folder (.{2,40}?)(?: apa saja| apa aja)?", lambda m: ("list_folder", {"path": m[1]})),
]


# what Digidaw knows about the user (the facts themselves: memory.py)
MEMORY = [
    (r"(?:lupakan|hapus|jangan ingat)(?: yang)? (?:itu|tadi)(?: saja| aja)?", lambda m: ("forget_latest", {})),
    (r"(?:ingat|ingatlah|simpan di ingatan|catat di ingatan)(?: ya)?(?: bahwa| kalau)? (?!kan\b|gak\b|nggak\b|tidak\b|apa\b|kah\b|ya$)(.{3,120})",
     lambda m: ("remember", {"fact": m[1]})),
    (r"(?:lupakan|hapus (?:ingatan|memori)(?: tentang| soal)?|jangan ingat(?: lagi)?(?: kalau| bahwa)?) (?!percakapan\b)(.{2,60})",
     lambda m: ("forget", {"keyword": m[1]})),
    (r"(?:apa|apa saja|apa aja) yang (?:kamu|anda|kau|lu|digidaw) (?:ingat|tahu|ketahui)(?: tentang (?:aku|saya|diriku))?|"
     r"(?:kamu|anda|kau|lu) (?:ingat|tahu) apa(?: saja| aja)? tentang (?:aku|saya|diriku)|"
     r"apa yang (?:kamu|anda) pelajari tentang (?:aku|saya)", lambda m: ("recall_memories", {})),
    (r"(?:putar|putarkan|setel|mainkan)(?: lagu| musik)?(?: yang)? (?:favorit|kesukaan|biasa|biasanya)(?:ku| aku| saya)?",
     lambda m: ("play_favourite", {})),
]


# power, keyboard, closing everything, "katakan", analysis, security scan, temperature, …
SYSTEM = [
    # ── power: always the real skill (it asks "yakin?"), never an AI that might only SAY it is restarting ──
    (r"(?:restart|reboot|mulai ulang|nyalakan ulang|hidupkan ulang) (?:komputer|laptop|pc|sistem)(?: (?:saya|ku|sekarang))?|restart",
     lambda m: ("restart_pc", {})),
    (r"(?:matikan|shutdown|shut down|matiin) (?:komputer|laptop|pc|sistem|daya)(?: (?:saya|ku|sekarang))?|shutdown|shut down",
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
    (r"(?:atur |set |ubah |setel |turunkan |kecilkan |kurangi |naikkan |tingkatkan |tambah |besarkan |redupkan |terangkan )?"
     r"(?:kecerahan|brightness)(?: layar)?(?:nya)? (?:ke |jadi |menjadi |di )?(\d{1,3})(?: ?(?:persen|%))?",
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
     r"(?:apakah |apa )?(?:laptop|komputer|pc|cpu|prosesor)(?:nya)?(?: (?:saya|ku|aku|ini))? (?:panas|kepanasan|overheat|terlalu panas)"
     r"(?: (?:nggak|gak|tidak|enggak|ga|banget|sekali))*|kenapa kipas(?:nya)?(?: laptop)? (?:kencang|berisik|bunyi)(?: banget| sekali)?",
     lambda m: ("get_temperature", {})),
    (r"(?:status sistem|diagnostik(?: sistem)?)", lambda m: ("get_system_status", {})),
    (r"(?:cek |berapa )?(?:penggunaan|pemakaian|pakai|sisa|kapasitas)? ?(?:ram|memori|memory|cpu|prosesor|processor)"
     r"(?: (?:saya|laptop|komputer|ku))?(?: (?:berapa|sekarang|sudah berapa|terpakai))*(?: persen)?",
     lambda m: ("get_system_status", {})),
    (r"(?:cek |lihat |berapa |sisa |tinggal berapa )*(?:baterai|batre|batrai)(?: (?:saya|laptop|laptopnya|ku|nya))?(?: (?:berapa|tinggal berapa|sisa berapa|sisa|sekarang))*(?: persen)?(?: sekarang)?",
     lambda m: ("get_battery", {})),
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
    # ── arranging windows ──
    (r"(?:bagi|belah|split|pisah|pecah) (?:layar|jendela)(?: antara)? (.{2,30}?) (?:dan|sama|dengan) (.{2,30})|"
     r"(?:sandingkan|jejerkan|sejajarkan|dampingkan) (.{2,30}?) (?:dan|sama|dengan) (.{2,30})",
     lambda m: ("side_by_side", {"left": m[1] or m[3], "right": m[2] or m[4]})),
    (r"(.{2,30}?)(?: di)? kiri(?: dan|,)? (.{2,30}?)(?: di)? kanan", lambda m: ("side_by_side", {"left": m[1], "right": m[2]})),
    (r"(.{2,30}?)(?: di)? kanan(?: dan|,)? (.{2,30}?)(?: di)? kiri", lambda m: ("side_by_side", {"left": m[2], "right": m[1]})),
    (r"(?:taruh|letakkan|tempatkan|pindahkan|pindahin|geser|buka) (?!jendela ini\b|jendela\b|ini\b|ke\b)(.{2,30}?) (?:di|ke)(?: sebelah| bagian)? (kiri|kanan)(?: layar)?",
     lambda m: ("place_window", {"name": m[1], "side": m[2]})),
    (r"(?:taruh|letakkan|tempatkan|pindahkan|pindahin|geser)(?: jendela)?(?: ini)? (?:di|ke)(?: sebelah| bagian)? (kiri|kanan)(?: layar)?",
     lambda m: ("place_window", {"name": "", "side": m[1]})),
    (r"tukar(?: posisi)?(?: jendela(?:nya)?| kiri kanan| layar)?", lambda m: ("swap_windows", {})),
    (r"(?:rapikan|susun|tata)(?: semua)? (?:jendela|aplikasi)(?:nya)?(?: ini)?|rapikan layar", lambda m: ("tidy_windows", {})),
    (r"(?:kembali|balik|pindah)(?: lagi)? ke (?:aplikasi |jendela )?(?:tadi|sebelumnya|terakhir)|(?:aplikasi|jendela) (?:tadi|sebelumnya)",
     lambda m: ("focus_previous", {})),
    (r"tutup semua(?: aplikasi| jendela)?(?: yang)? kecuali (.{2,50})", lambda m: ("close_others", {"keep": m[1]})),
    (rf"(?:pindahkan|pindahin|kirim|kirimkan) semua(?: jendela| aplikasi)?(?: yang (?:ada|terbuka))?(?: di sini)? ke {WORKSPACE} ({WS_NUM})",
     lambda m: _ws_route("move_all_to_workspace", {"workspace": _ws(m[1])})),
    (rf"(?:buka|open|jalankan) (?!{WORKSPACE}\b)(?:(?:situs|website|web|laman|halaman|aplikasi) )?(.{{2,30}}?)(?: (?:di|ke))? {WORKSPACE} ({WS_NUM})",
     lambda m: _ws_route("open_on_workspace", {"name": m[1], "workspace": _ws(m[2])})),
    (rf"(?!pindah|beralih|pergi|ke |balik|kembali)(.{{2,30}}?) di {WORKSPACE} ({WS_NUM})",        # "whatsapp di workspace 1"
     lambda m: _ws_route("open_on_workspace", {"name": m[1], "workspace": _ws(m[2])}) if _resolved("open", m[1], sure_only=True) else None),
    (r"(?:(?:buka|klik|pilih) )?(?:(?:yang|hasil|link|situs) )(?:pertama|paling atas)",
     lambda m: ("open_first_result", {}) if sc.searched_lately() else None),
    (r"(?:buka|lihat|tampilkan) folder", lambda m: ("open_folder", {"name": "home"})),
    (r"(?:buka|lihat|tampilkan) (?:folder )?(?:screenshots?|tangkapan layar)", lambda m: ("open_folder", {"name": "screenshots"})),
    (r"(?:buka|lihat|tampilkan) folder (.{2,40})", lambda m: ("open_folder", {"name": m[1]})),
    (r"(?:tutup|close) (?:semua )?(?:folder|pengelola file)(?: ini| itu| tadi|nya)?", lambda m: ("close_application", {"name": "file manager"})),
    (r"(?:buka|jalankan) (?:terminal|kitty|konsol|console)(?: baru)? (?:di|ke|pada)(?: folder| direktori)? (.{2,40})",
     lambda m: ("open_terminal_in", {"folder": m[1]})),
    # ── workspaces (Hyprland) ──
    (rf"(?:pindahkan|pindahin|kirim|kirimkan|lempar|taruh) ke {WORKSPACE} ({WS_NUM})",      # right after "buka whatsapp": that app
     lambda m: _ws_route("move_to_workspace", {"workspace": _ws(m[1]), "follow": False, "name": _that_app("opened")}) if _that_app("opened") else _ws_route("switch_workspace", {"workspace": _ws(m[1])})),
    (rf"(?:(?:pindah|beralih|switch|balik|kembali|pergi|buka|lompat)(?: ke)? |ke |\w{{3,8}}(?: ke)? )?{WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)",
     lambda m: _ws_route("switch_workspace", {"workspace": _ws(m[1])})),
    (rf"(?:pindahkan|pindahin|kirim|kirimkan|lempar|taruh|move) (.+?) ke {WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)"
     r"( (?:dan|lalu|terus) (?:ikut(?: pindah)?|ikuti|ke sana|pindah ke sana))?",
     lambda m: _ws_route("move_to_workspace", {"workspace": _ws(m[2]), "follow": bool(m[3]),
                                               "name": "" if re.fullmatch(r"(?:jendela|aplikasi)? ?(?:ini|itu|dia|aktif|yang aktif|sekarang)|jendela|aplikasi", m[1]) else m[1]})),
    (rf"pindah(?: ke)? (?!{WORKSPACE} )(.{{2,30}}?) ke {WORKSPACE} ({WS_NUM})",      # "pindah ke Terminal ke WordPress 3": it goes there, it doesn't open
     lambda m: _ws_route("move_to_workspace", {"workspace": _ws(m[2]), "follow": False, "name": m[1]})),
    (r"(?:pindah|beralih|switch|fokus|balik) ke (?:jendela |aplikasi )?(?!halaman|tab|versi|mode)(.+)",
     lambda m: _focus_known(m[1])),
    # "ke WhatsApp" / "kembali ke discord": the same, without the verb (speech often drops it)
    (r"(?:ke|kembali ke|balik ke) (?!halaman|tab|versi|mode|paling|atas|bawah|sana|sini|mana|situ)(.{2,40})",
     lambda m: _focus_known(m[1], raw_fallback=False)),
    (r"(?:pindah|beralih) (?!ke\b|keluar\b|dong\b|lagi\b)(.{2,30})", lambda m: _focus_known(m[1], raw_fallback=False)),   # "pindah WhatsApp"
    (r"(?:pindah|pindah dong|pindah lagi|geser)", lambda m: _ws_route("switch_workspace", {"workspace": "next"})),
    (r"(?:lagu|song) (?!apa\b|ini\b|itu\b|berikutnya\b|selanjutnya\b|sebelumnya\b|yang\b|lagi\b|tadi\b|favorit)(.{2,60})",
     lambda m: _play(m[1])),
    (r"([a-z][a-z ]{1,30}) music", lambda m: _play(m[0])),
    (r"(?:buka|open) (.+?) (?:di|pakai|pake|lewat|dengan) (firefox|chrome|google chrome|brave|edge|chromium)",
     lambda m: _open_in_browser(m[1], m[2])),
    (r"(?:butuh|perlu|pengen buka|mau buka) (.{2,40})", lambda m: _open_known(m[1], single=True)),
    # "untuk Firefox": usually "tutup Firefox" misheard — when it is open, ask instead of answering into the void
    (r"(?:untuk|untung|utuk|tuk|youtube) (.{2,30})", lambda m: _ask_close(m[1])),
    # "pindah" heard as these ("Bina ke Firefox", "menuju Firefox"), when the rest is something that is really there
    (r"(?:indah|bina|binah|dinda|menuju|minta)(?: ke)? (.{2,30})", lambda m: _focus_known(m[1], raw_fallback=False)),
    (r"(?:buka|open) (?!.* (?:dan|sama|serta) )(.{2,30}?) (?:baru|lagi)", lambda m: ("open_application", {"name": m[1], "new_window": True})
     if (_open_known(m[1], single=True) or [""])[0] == "open_application" else None),
    (r"(?:buka|open|jalankan) (.+)", lambda m: _open_known(m[1])),
    (r"(?:tutup|close) (?:musik|lagu|pemutar(?: musik| youtube| lagu)?|player)(?:nya)?", lambda m: ("media_control", {"action": "close"})),
    (r"(?:tutup|close) (.+)", lambda m: _close_known(m[1])),
    # "cari kepiting": on YouTube when YouTube is in front, otherwise Google (not a chat about crabs)
    (r"(?:cari|carikan|search|googling|telusuri)(?: di google)? (?!tahu\b|tau\b|file\b|folder\b|dokumen\b|berkas\b|"
     r"jendela\b|aplikasi\b|tab\b)(.{2,80})", lambda m: _search_route(m[1])),
]


ORDER = [SITE_SEARCH, ASSISTANT, PROJECTS, SECURITY, SCREEN, DAY, LAMPS, EVERYDAY, MESSAGES, WEATHER, SOUND, CONTROL, MEMORY, SYSTEM, WINDOWS]
FAST_ROUTES = [(re.compile(p), h) for group in ORDER for p, h in group]
