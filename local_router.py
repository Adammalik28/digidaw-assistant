"""
The local brain's decision, made reliable: what should happen with this sentence?

The small local model (Qwen 4B) used to get every tool as a schema and was free to answer in words. Measured on real
commands (4 Nov 2026, tests/exam_brain.py): most of the time it just talked and said it had done something
("aku buat timer 3 menit") without doing it. Here it can't: Ollama restricts its output to one choice from the list,
token by token, and it doesn't "think" first (0.3 s instead of 0.85 s). Then, in a second short step, the arguments
of that one tool are filled in, again restricted to that tool's schema.

Choices besides the tools: hand over to the online AI, small talk, general knowledge, or "that made no sense: ask".
Examples of what worked before (experience.py) go along with each decision: that is how it learns from use, without
any training run (a 4B model can't be fine-tuned on this laptop: about 10 GB of video memory, it has 4).
"""
import inspect
import json
import re
import typing

import local_llm

AI = "serahkan_ke_ai"          # needs the internet or the big AI
CHAT = "ngobrol"               # small talk, feelings, thanks
KNOW = "pengetahuan"           # general knowledge: the big AI is more reliable; offline, the local one tries
ASK = "tanya_balik"            # garbled by the speech recognition

CATALOG = {
    "open_application": "buka aplikasi", "open_many": "buka beberapa aplikasi / situs sekaligus",
    "open_website": "buka situs web", "close_application": "tutup aplikasi", "close_many": "tutup beberapa aplikasi",
    "close_all_apps": "tutup SEMUA aplikasi", "focus_window": "pindah ke jendela / aplikasi yang sudah terbuka",
    "window_action": "atur jendela ini: perbesar, kecilkan, pulihkan, tutup, layar penuh",
    "list_windows": "sebutkan jendela yang terbuka", "list_running_apps": "sebutkan aplikasi yang berjalan",
    "switch_workspace": "pindah ke workspace lain", "move_to_workspace": "pindahkan jendela ke workspace lain",
    "close_tab": "tutup tab browser tertentu", "switch_tab": "pindah ke tab browser tertentu",
    "close_current_tab": "tutup tab yang sedang dilihat", "navigate": "gulir, kembali, maju, muat ulang halaman",
    "play_youtube": "putar lagu / musik / video apa pun (pilihan utama untuk memutar)",
    "play_favourite": "putar lagu kesukaan user, tanpa judul",
    "play_spotify": "putar di Spotify (hanya kalau Spotify disebut)",
    "media_control": "jeda, lanjutkan, lagu berikutnya / sebelumnya", "now_playing": "judul lagu yang sedang diputar",
    "set_volume": "volume ke angka tertentu", "volume_up": "volume naik", "volume_down": "volume turun",
    "volume_mute": "bisukan suara", "set_brightness": "kecerahan LAYAR laptop",
    "lamp_power": "nyalakan / matikan LAMPU", "lamp_brightness": "terang atau redup LAMPU",
    "lamp_color": "warna LAMPU", "lamp_scene": "suasana lampu: baca, kerja, santai, film, tidur, pesta",
    "lamp_status": "keadaan lampu sekarang", "take_screenshot": "ambil tangkapan layar",
    "type_text": "ketik teks ke jendela yang aktif", "press_keys": "tekan tombol / shortcut keyboard",
    "get_clipboard": "baca isi clipboard", "set_clipboard": "salin teks ke clipboard",
    "write_note": "catat sesuatu di catatan", "read_last_note": "bacakan catatan terakhir",
    "set_timer": "hitung mundur sekian menit / jam", "set_alarm": "alarm bangun pada jam tertentu",
    "set_reminder_at": "ingatkan sesuatu pada jam tertentu", "list_timers": "sisa waktu timer, alarm, pengingat",
    "cancel_timers": "batalkan timer / alarm / pengingat",
    "sleep_timer": "matikan komputer sekian menit lagi", "cancel_shutdown": "batalkan jadwal mematikan komputer",
    "get_time": "jam sekarang", "get_date": "tanggal hari ini",
    "add_event": "catat acara atau janji pada tanggal tertentu", "list_events": "lihat jadwal / agenda",
    "list_classes": "jadwal kuliah: hari ini, besok, atau per hari",
    "delete_event": "hapus acara dari agenda", "open_agenda_page": "buka halaman agenda / jadwal", "test_alarm": "coba bunyikan alarm",
    "sound_settings": "ganti suara alarm", "get_weather": "cuaca sekarang", "get_forecast": "perkiraan cuaca, hujan, besok",
    "get_system_status": "CPU, RAM, disk, baterai", "get_temperature": "suhu laptop",
    "get_network_info": "wifi yang dipakai, IP, jaringan", "open_folder": "buka folder", "open_file": "buka file",
    "find_files": "cari file", "create_folder": "buat folder baru", "create_file": "buat file baru berisi teks",
    "delete_file": "hapus file", "remember": "simpan fakta / kesukaan user untuk diingat",
    "recall_memories": "sebutkan apa yang kamu ingat tentang user", "learned_habits": "kebiasaan user yang dipelajari",
    "lock_workstation": "kunci laptop", "shutdown_pc": "matikan komputer sekarang", "restart_pc": "restart komputer",
    "sleep_pc": "tidurkan laptop", "run_command": "jalankan perintah terminal yang disebut user", "run_as_root": "jalankan perintah yang butuh sudo atau root", "coin_price": "harga kripto", "gold_price": "harga emas", "exchange_rate": "kurs mata uang asing ke rupiah",
    "world_time": "jam di kota atau negara lain", "apps_open": "apakah aplikasi tertentu masih berjalan", "get_battery": "sisa baterai saja",
    "clean_temp_files": "bersihkan file sementara / sampah", "empty_recycle_bin": "kosongkan tempat sampah",
    "read_notifications": "baca pesan / notifikasi masuk", "reply_message": "balas / kirim pesan WhatsApp atau WhatsApp bisnis",
    "watch_for": "kabari user kalau sesuatu terjadi nanti", "list_watches": "apa yang sedang dipantau",
    "cancel_watch": "batalkan pantauan", "daily_recap": "rangkuman hari ini", "screen_time": "lama pakai laptop hari ini",
    "performance_report": "laporan kinerja Digidaw", "sun_today": "jam matahari terbit / terbenam",
    "ai_status": "status / limit AI (Gemini)", "get_selected_text": "baca teks yang sedang dipilih",
    "wifi_power": "nyalakan / matikan Wi-Fi", "wifi_connect": "sambung ke jaringan Wi-Fi tertentu",
    "bluetooth": "nyalakan / matikan Bluetooth, sambung / putuskan perangkat", "power_mode": "mode daya: hemat, seimbang, performa",
    "side_by_side": "tampilkan dua aplikasi berdampingan, bagi layar", "place_window": "taruh jendela di kiri atau kanan layar",
    "tidy_windows": "rapikan jendela yang berantakan", "close_others": "tutup semua aplikasi kecuali yang disebut",
    "open_terminal_in": "buka terminal di folder tertentu", "focus_previous": "kembali ke jendela sebelumnya",
    "fan_speed": "atur kecepatan kipas laptop, atau tanya kecepatannya", "open_script_manager": "buka pengelola / daftar skrip saya",
    "run_script": "jalankan skrip buatan saya di folder myskrip menurut namanya", "close_script_window": "tutup jendela skrip",
    "do_not_disturb": "mode jangan ganggu, senyapkan notifikasi", "audio_output": "pindahkan keluaran suara: speaker, HDMI, USB, headset",
    "screen_power": "matikan / nyalakan layar", "list_folder": "lihat isi sebuah folder",
    "read_text_file": "baca isi file teks", "move_file": "pindahkan file", "copy_file": "salin file ke folder lain",
    "rename_file": "ganti nama file", "click_on_screen": "klik satu benda yang terlihat di layar",
    "do_on_screen": "kerjakan tugas beberapa langkah di layar: mencari di browser, mengisi formulir, mengubah pengaturan aplikasi",
    "stop_task": "hentikan tugas di layar yang sedang berjalan", "resume_task": "lanjutkan tugas di layar yang menunggu",
    "app_volume": "atur atau cek volume suara satu aplikasi tertentu (misal discord, spotify, firefox)",
    "mic_control": "bisukan, nyalakan, atau atur volume mikrofon sistem",
    "clipboard_history": "lihat riwayat teks yang disalin ke clipboard",
    "paste_previous": "tempel salinan sebelumnya dari riwayat clipboard",
    "copy_screen_text": "salin teks yang terlihat di layar ke clipboard (OCR)",
    "port_process": "cek proses yang memakai SATU nomor port jaringan tertentu atau hentikan",
    "open_ports": "daftar semua port jaringan yang sedang terbuka di komputer ini",
    "service_control": "cek status, jalankan, hentikan, atau restart service systemd",
    "explain_last_error": "jelaskan error yang sedang dipilih, di clipboard, atau di layar terminal",
}

EXAMPLES = [
    ("gedein suaranya", "volume_up"), ("musik jazz buat kerja dong", "play_youtube"), ("terangin layar", "set_brightness"),
    ("nyalain lampunya", "lamp_power"), ("lampu warna ungu", "lamp_color"), ("kamarnya terlalu terang, redupin", "lamp_brightness"),
    ("ingetin aku jemput adik jam 4 sore", "set_reminder_at"), ("bangunin aku jam 6", "set_alarm"),
    ("timer 10 menit", "set_timer"), ("catat nomor rekening baru", "write_note"), ("sisa batre berapa", "get_system_status"),
    ("jendela apa aja yang kebuka", "list_windows"), ("tangkap layar", "take_screenshot"), ("cuaca besok gimana", "get_forecast"),
    ("ada pesan baru nggak", "read_notifications"), ("jadwalku minggu ini apa", "list_events"), ("tekan alt tab", "press_keys"),
    ("ketik selamat pagi", "type_text"), ("jalankan ls di terminal", "run_command"), ("bikin folder proyek", "create_folder"),
    ("copy kata selamat ke clipboard", "set_clipboard"), ("matikan laptop 10 menit lagi", "sleep_timer"),
    ("hapus sampah di laptop", "clean_temp_files"), ("aku pakai jaringan apa", "get_network_info"),
    ("hidupkan wifi", "wifi_power"), ("konekin bluetooth ke speaker", "bluetooth"), ("pakai mode hemat baterai", "power_mode"),
    ("isi folder dokumen apa aja", "list_folder"), ("pindahin foto.jpg ke dokumen", "move_file"), ("klik tombol simpan", "click_on_screen"),
    ("buka chrome terus cari tiket kereta lalu buka hasil pertama", "do_on_screen"), ("ganti tema vs code jadi terang", "do_on_screen"),
    ("kurs yen hari ini", AI), ("siapa menteri keuangan sekarang", AI), ("ringkas halaman ini", AI), ("nyalakan hotspot", AI),
    ("apa yang ada di layarku", AI), ("selamat pagi", CHAT), ("aku lagi sedih", CHAT), ("makasih banyak", CHAT), ("kamu siapa", CHAT),
    ("kamu bisa apa aja", CHAT), ("kenapa air laut asin", KNOW), ("ibu kota jepang apa", KNOW), ("berapa 15 ditambah 8", KNOW),
    ("buatkan puisi tentang hujan", KNOW), ("ceritain dongeng", KNOW), ("bikin lelucon", KNOW),
    ("apa yang kamu ingat tentang aku", "recall_memories"), ("buatkan aplikasi kasir", AI),
    ("kipasnya bikin 60 persen", "fan_speed"), ("tolong jalanin skripku yang cek cpu", "run_script"),
    ("mana daftar skrip buatanku", "open_script_manager"),
    ("kecilkan suara discord ke 30 persen", "app_volume"), ("bisukan mikrofon", "mic_control"),
    ("siapa yang pakai port 8080", "port_process"), ("port mana saja yang lagi kebuka", "open_ports"),
    ("restart service bluetooth", "service_control"),
    ("salin teks di layar", "copy_screen_text"), ("jelaskan error ini", "explain_last_error"),
    ("daun meja piring", ASK), ("gigi chord buku", ASK), ("itu dari sini kok", ASK),
    # what the real use taught (logs, Nov 2026; teaching.py has the rest)
    ("buka get up", "open_application"), ("buka warship lo", "open_application"), ("tutup semua tuner", "close_application"), ("pigi chord digi buka terminal", "open_application"),
    ("gigi apakah ada pesan masuk", "read_notifications"), ("222 enter", "type_text"), ("cek email", "read_notifications"), ("perbaiki project-nya tidak bisa berjalan", AI),
    ("buka note dan ketik halo", "write_note"), ("ceritakan tentang anda", CHAT), ("you do", ASK), ("roar", ASK), ("lagi", ASK), ("ngento", ASK), ("hidup rindu", ASK), ("buka dikit dong", ASK),
    ("langsung buka", ASK), ("nanti cepet ah", ASK), ("sibuk sayang chord", ASK), ("lama lu belum buka x", ASK), ("apakah perangkat saya masih", ASK), ("detik claude", ASK),
]


def _catalog(extra: dict | None = None) -> dict:
    return {**CATALOG, **(extra or {})}


def _system(names: dict) -> str:
    tools = "\n".join(f"- {n}: {d}" for n, d in names.items())
    examples = "\n".join(f'"{t}" -> {c}' for t, c in EXAMPLES if c in names or c in (AI, CHAT, KNOW, ASK))
    return f"""Kamu bagian pemilih tindakan asisten komputer DIGIDAW. Dari kalimat user (hasil pengenalan suara, kadang salah dengar) pilih SATU.

Alat yang ada:
{tools}

Pilihan lain:
- {AI}: butuh internet atau informasi terbaru (berita, harga, kurs, skor, siapa yang menjabat sekarang), melihat isi layar, merangkum halaman, tugas rumit, membuat program, beberapa perintah sekaligus, atau permintaan di komputer yang tidak ada alatnya.
- {CHAT}: sapaan, terima kasih, obrolan, perasaan, pertanyaan tentang dirimu.
- {KNOW}: pengetahuan umum yang tidak berubah, hitungan, minta dibuatkan pantun, puisi, cerita, atau lelucon.
- {ASK}: kalimatnya kacau, terpotong, atau tidak jelas maksudnya. Jangan menebak. Satu atau dua kata yang bukan sapaan dan bukan perintah juga ini, walaupun terdengar seperti obrolan.

Hal yang sering keliru:
- {CHAT} hanya untuk sapaan, terima kasih, perasaan, atau pertanyaan tentang dirimu yang jelas. Kata tak jelas bukan obrolan.
- "buka X" dengan X nama yang asing tetap open_application: nama aplikasi sering terdengar aneh. Tapi "buka" tanpa nama yang jelas ("buka di", "buka dikit") itu {ASK}.
- "tutup X" dengan X nama yang asing tetap close_application.
- Kata seperti "gigi", "biji", "pigi chord", "7 daun" di awal kalimat adalah nama Digidaw yang salah dengar: abaikan, pilih menurut sisa kalimatnya.

Kalau user minta MELAKUKAN sesuatu di komputer atau menanyakan keadaan komputer, pilih alatnya.

Contoh:
{examples}"""


# What a sentence has to say before the small model's choice is believed. Its choice is forced, so nonsense still gets a tool: "hapus teks"
# closed a browser tab, "maju 30 detik" set a timer, "to" turned the lamp red, "terangkan bersih" offered to delete files. Tools that only
# tell something aren't here: a wrong answer costs less than a wrong action.
_LAMP_WORDS = r"lampu|warna|terang|redup|gelap|merah|hijau|biru|kuning|oranye|jingga|ungu|pink|toska|cyan|putih|hangat"
MUST_SAY = {
    "reply_message": r"balas|bales|kirim|bilang|jawab|sampaikan",
    "open_application": r"buka|bukakan|open|jalankan|mulai|masuk|pindah|ke\b|pakai|butuh|mau",
    "close_application": r"tutup|close|matikan|keluar|hentikan|quit|kill|untuk|utuk", "close_many": r"tutup|close|matikan",
    "close_all_apps": r"tutup|close", "close_others": r"tutup|close", "close_tab": r"tutup|close", "close_current_tab": r"tutup|close",
    "set_timer": r"timer|hitung mundur|countdown", "set_alarm": r"alarm|bangunkan|bangunin|weker",
    "set_reminder_at": r"ingatkan|ingetin|ingatin|pengingat|reminder", "cancel_timers": r"batal|hapus|matikan|hentikan|stop|cancel",
    "sleep_timer": r"menit|jam", "write_note": r"catat|catatan|tulis|note|editor|notepad", "read_last_note": r"catatan|note|notepad",
    "lamp_power": r"lampu|nyala|mati|hidup|padam", "lamp_brightness": _LAMP_WORDS + r"|\d", "lamp_color": _LAMP_WORDS, "lamp_scene": _LAMP_WORDS + r"|suasana|nuansa|santai|tidur|film|baca|kerja|pesta",
    "volume_up": r"volume|suara|keras|kencang|besar|naik", "volume_down": r"volume|suara|kecil|pelan|turun", "volume_mute": r"volume|suara|bisu|mute|senyap",
    "set_volume": r"volume|suara|\d", "set_brightness": r"kecerahan|brightness|terang|redup|layar|\d", "power_mode": r"power|daya|hemat|performa|seimbang|gaming|game|batre|baterai",
    "clean_temp_files": r"bersih|hapus|sampah|temp|cache|sementara", "system_cleanup": r"bersih|hapus|sampah|temp|cache|sementara",
    "empty_recycle_bin": r"sampah|recycle|trash|kosong", "delete_file": r"hapus|buang|delete", "delete_event": r"hapus|batal|buang|delete", "delete_protocol": r"hapus|buang|delete",
    "add_event": r"agenda|acara|jadwal|tanggal|tgl|besok|lusa|rapat|ujian|janji|kuliah|senin|selasa|rabu|kamis|jumat|sabtu|minggu",
    "shutdown_pc": r"matikan|shutdown|shut|daya", "restart_pc": r"restart|reboot|mulai ulang|nyalakan ulang|hidupkan ulang", "sleep_pc": r"tidur|sleep", "log_off": r"keluar|log ?out|log ?off",
    "lock_workstation": r"kunci|lock", "play_favourite": r"kesukaan|favorit|biasa", "navigate": r"scroll|gulir|geser|kembali|balik|back|maju|forward|refresh|reload|muat|tab|halaman|atas|bawah|turun|naik",
    "window_action": r"jendela|window|perbesar|perkecil|kecilkan|besarkan|layar penuh|full ?screen|minimize|maximize|pulihkan|kiri|kanan|snap|desktop", "focus_window": r"pindah|beralih|fokus|ke\b|balik|kembali|tampilkan|menuju|lihat|buka",
    "switch_workspace": r"workspace|work|word|space|ws\b|sp\b|pindah|ke\b|geser|desktop", "move_to_workspace": r"workspace|work|word|space|ws\b|sp\b|pindah|kirim|lempar|geser|taruh|tindakan",
    "press_keys": r"tekan|klik|pencet|press|enter|ctrl|shift|alt|tombol|tab\b|escape|esc|spasi", "type_text": r"ketik|tulis|type|masukkan", "set_clipboard": r"salin|copy|kopi|clipboard",
    "fan_speed": r"kipas|fan|van|pan\b|friend", "bluetooth": r"bluetooth|headset|earphone|earbuds|tws|speaker|keyboard|mouse|sambung", "wifi_power": r"wi ?fi", "wifi_connect": r"wi ?fi|sambung", "screen_power": r"layar|monitor",
    "do_not_disturb": r"ganggu|notif|senyap", "take_screenshot": r"screenshot|tangkap|layar|foto", "audio_output": r"suara|audio|speaker|headset|keluaran", "cancel_watch": r"pantau|kabari", "remember": r"ingat|simpan|catat|nama|suka|tahu",
    "list_timers": r"timer|alarm|pengingat|sisa|reminder", "run_script": r"jalankan|jalanin|skrip|script|scrip|run|eksekusi|sc\b|terangkan",
    "app_volume": r"volume|suara|audio|bisu|mute|kecil|besar|naik|turun",
    "mic_control": r"mikrofon|mic\b|mike\b",
    "clipboard_history": r"clipboard|riwayat|salinan|copy|kopi",
    "paste_previous": r"tempel|paste|salinan",
    "copy_screen_text": r"salin|copy|ocr|teks|tulisan",
    "port_process": r"port\b",
    "open_ports": r"port\b",
    "service_control": r"service|servis|layanan|daemon|systemctl",
    "explain_last_error": r"error|gagal|kenapa|salah|exception|traceback",
}
MUST_NOT_SAY = {"take_screenshot": r"rekam", "set_timer": r"\b(?:maju|mundur|lompat)\b", "lamp_brightness": r"\b(?:layar|monitor)\b", "lamp_power": r"\b(?:layar|monitor)\b"}
_MUST_SAY = {tool: re.compile(words, re.IGNORECASE) for tool, words in MUST_SAY.items()}
_MUST_NOT_SAY = {tool: re.compile(words, re.IGNORECASE) for tool, words in MUST_NOT_SAY.items()}


# two words or less, called small talk by the model: only with a word that really is small talk. "rawr", "oh", "do you" got an answer about them.
TALK_WORDS = re.compile(r"\b(?:halo+|hallo+|hai+|hi|hei|hey|pagi|siang|sore|malam|selamat|makasih|terima kasih|thanks|thank you|oke|ok|okay|sip|siap|keren|mantap|bagus|hebat|"
                        r"maaf|sorry|capek|cape|lelah|sedih|senang|bosan|ngantuk|lapar|kamu|anda|siapa|apa kabar|kabar\w*|wah|hebat|pintar|pinter|bodoh|tolol)\b", re.IGNORECASE)


def settle(choice: str, text: str, recent: str = "", online: bool = True) -> str:
    """The model's choice, after what it can't be trusted with. A choice that doesn't fit the sentence: the online AI reads a longer one, a short one is asked back."""
    if not believable(choice, text, recent):
        return AI if online and len(text.split()) >= 3 else ASK
    if choice == CHAT and len(text.split()) <= 2 and not TALK_WORDS.search(text):
        return ASK
    return choice


def believable(tool: str, text: str, recent: str = "") -> bool:
    """Does this sentence say anything about what the model chose? recent: the tool just used: then a follow-up counts ("ke 20%")."""
    forbidden = _MUST_NOT_SAY.get(tool)
    if forbidden and forbidden.search(text):
        return False
    words = _MUST_SAY.get(tool)
    return words is None or tool == recent or words.search(text) is not None


def pick(text: str, last_turn: tuple[str, str] | None = None, similar: list[tuple[str, str]] | None = None,
         extra: dict | None = None) -> str:
    """The choice for this sentence: a tool's name, AI, CHAT, KNOW or ASK."""
    names = _catalog(extra)
    schema = {"type": "object", "properties": {"pilihan": {"type": "string", "enum": [*names, AI, CHAT, KNOW, ASK]}},
              "required": ["pilihan"]}
    user = text
    if last_turn:                          # follow-ups ("yang dari whatsapp", "ya") mean something only after this
        user = f"(Sebelumnya user: {last_turn[0][:120]} | Digidaw: {last_turn[1][:160]})\nKalimat sekarang: {text}"
    if similar:                            # what worked before for sentences like this one
        user = "Mirip yang pernah terjadi: " + "; ".join(f'"{t}" -> {c}' for t, c in similar) + "\n" + user
    answer = local_llm.generate(_system(names), user, schema=schema, max_tokens=40, temperature=0.0)
    return json.loads(answer)["pilihan"]


EXPAND_SYSTEM = "Kamu menulis ulang kalimat lanjutan menjadi pertanyaan atau perintah lengkap, dengan meniru bentuk kalimat sebelumnya dan mengganti bagian yang berubah. Jangan menjawab. Tulis hanya kalimat lengkapnya."
_EXPAND_SHOTS = [("cuaca di medan hari ini", "kalau di padang", "cuaca di padang hari ini"), ("jadwal saya hari ini", "kalau kamis", "jadwal saya kamis"), ("buka telegram", "kalau signal", "buka signal"),
                 ("siapa sutradara film dilan", "pemainnya siapa", "siapa pemain film dilan"), ("berapa harga beras", "terus gula", "berapa harga gula"), ("tampilkan foto", "buka yang pertama", "buka foto yang pertama")]
STATES_RE = re.compile(r"\b(?:adalah|ialah|yaitu|merupakan|bukan|akan)\b", re.IGNORECASE)


def _expand_turn(before: str, follow_up: str) -> str:
    return f"Sebelumnya: {before}\nLanjutan: {follow_up}"


def expand(text: str, last_user: str) -> str:
    """"kalau besok" after "cuaca hari ini": "cuaca besok". A whole sentence is something the rules and the choice can read.
    '' when it answered or echoed instead of rewriting."""
    answer = local_llm.generate(EXPAND_SYSTEM, _expand_turn(last_user[:120], text), max_tokens=30, temperature=0.0, stop=["\n"],
                                history=[(_expand_turn(before, then), whole) for before, then, whole in _EXPAND_SHOTS])
    answer = (answer or "").strip().strip('".')
    if ":" in answer or (STATES_RE.search(answer) and not STATES_RE.search(last_user + " " + text)):
        return ""
    return answer


def _param_schema(kind) -> dict:
    origin = typing.get_origin(kind)
    if origin in (list, typing.List):
        return {"type": "array", "items": {"type": "string"}}
    if origin is typing.Union:             # "str | None"
        kinds = [k for k in typing.get_args(kind) if k is not type(None)]
        return _param_schema(kinds[0]) if kinds else {"type": "string"}
    return {"type": {int: "integer", float: "number", bool: "boolean"}.get(kind, "string")}


def args_schema(fn) -> tuple[dict, list[str]]:
    """(JSON schema, required names) from a skill's signature."""
    hints = typing.get_type_hints(fn)
    props, required = {}, []
    choices = CHOICES.get(fn.__name__, {})
    for name, param in inspect.signature(fn).parameters.items():
        props[name] = _param_schema(hints.get(name, str))
        if name in choices:
            props[name]["enum"] = choices[name]
        if param.default is inspect.Parameter.empty:
            required.append(name)
    forced = [n for n in props if n in KEEP_FLAGS or n in choices]    # the model must decide these, not leave them out
    return {"type": "object", "properties": props, "required": sorted({*required, *forced})}, required


KEEP_FLAGS = {"on", "relative"}    # true / false here is the point of the command ("matikan lampu": on=false)
# arguments with a fixed set of values: the model can only pick one, and its pick counts even if not said word for word
CHOICES = {"sleep_timer": {"target": ["music", "laptop"]},
           "window_action": {"action": ["minimize", "maximize", "restore", "close", "snap_left", "snap_right", "minimize_all"]},
           "media_control": {"action": ["pause", "play", "toggle", "next", "previous", "forward", "rewind", "restart",
                                        "fullscreen", "loop", "skip_ad", "close"]},
           "navigate": {"action": ["scroll_down", "scroll_up", "top", "bottom", "back", "forward", "refresh", "new_tab",
                                   "next_tab", "previous_tab", "reopen_tab"]},
           "watch_for": {"what": ["unduhan", "pesan", "hujan_reda", "hujan", "baterai", "program", "internet", "situs",
                                  "cpu", "update"]},
           "get_forecast": {"when": ["nanti", "hari ini", "besok", "lusa"]},
           "bluetooth": {"action": ["on", "off", "connect", "disconnect", "status"]},
           "mic_control": {"action": ["mute", "unmute", "status"]},
           "service_control": {"action": ["status", "start", "stop", "restart"]}}
NUMBER_RE = re.compile(r"\d|\b(?:nol|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|belas|puluh|ratus|"
                       r"setengah|seperempat|sejam|semenit)\b", re.IGNORECASE)
REPEAT_RE = re.compile(r"setiap|tiap|hari kerja|akhir pekan|senin|selasa|rabu|kamis|jumat|sabtu|minggu")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FREE_TEXT_ARGS = {"query", "text", "content", "fact", "message", "target", "task", "title", "description", "new_name", "command"}
GROUNDED_NAMES = {("wifi_connect", "name"), ("audio_output", "name"), ("run_script", "name"), ("power_mode", "mode"), ("side_by_side", "left"),
                  ("side_by_side", "right"), ("close_others", "keep"), ("open_terminal_in", "folder"), ("open_on_workspace", "name")}      # a network or an output: never a guess
NAME_ARGS = {"name", "site"}
NOT_A_NAME = {"buka", "tutup", "pindah", "langsung", "aplikasi", "situs", "website", "jendela", "dikit", "dong", "di", "ke"}
TIME_SAID_RE = re.compile(r"\b(?:jam|pukul|pagi|siang|sore|malam)\b|\d[:.]\d", re.IGNORECASE)
QUERY_VERB_RE = re.compile(r"^(?:putar(?:kan)?|mainkan|setel|cari(?:kan)?|telusuri)\s+", re.IGNORECASE)
GENERIC_WORDS = {"lagu", "musik", "video", "putar", "mainkan", "yang", "dan", "di", "ke", "untuk", "buat", "saya", "aku", "the"}


def _words(text) -> list[str]:
    return re.findall(r"\w+", str(text).lower())


def _said(value, sentence: str) -> bool:
    """An optional value is used only when the user really said it: the small model likes to invent them ("browser:
    firefox", press_enter: true, which would send a half-written chat message)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0 and re.search(rf"\b{int(value)}\b", sentence) is not None
    if isinstance(value, str) and DATE_RE.fullmatch(value.strip()):
        return True
    words = _words(value)
    said = _words(sentence)
    return bool(words) and len(words) < len(said) and all(w in said for w in words)


def _grounded(value, sentence: str) -> bool:
    """Free text the skill will use must come from the sentence, at least half of its words beyond the generic ones:
    "aku pengen denger lagu galau" must not become query "putar lagu-lagu Tulus"."""
    words = [w for w in _words(value) if w not in GENERIC_WORDS]
    said = [w for w in _words(sentence) if len(w) >= 3]
    found = sum(any(w == s or (len(w) >= 3 and (s.startswith(w) or w.startswith(s))) for s in said) or w in sentence.lower()
                for w in words)
    return found * 2 >= len(words)


def _makes_sense(tool: str, name: str, value, text: str, params) -> bool:
    """Is this value for a required argument what the user meant? All of these were real answers to real sentences:
    "atur kecerahan di 50%" became brightness 0, "coba sambungkan lagi" Wi-Fi "connect_wifi_reconnect", "buatkan pantun"
    a note saying "buatkan pantun", "buka dikit dong" an app of that name."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        if "relative" in params:
            return True
        digits = re.findall(r"\d+", text)
        return (float(value) == int(value) and str(int(value)) in digits) if digits else bool(NUMBER_RE.search(text))
    if not isinstance(value, str):
        return True
    low = value.strip().lower()
    words, said = _words(low), _words(text)
    if low == tool.lower() or ("_" in low and low not in text.lower()):
        return False                       # its own name, or some identifier: it didn't understand
    if name == "time_hhmm" and not TIME_SAID_RE.search(text):
        return False                       # no time was said ("ingatkan firefox ke workspace 2": not 21:00)
    if name in NAME_ARGS and set(words) & NOT_A_NAME:
        return False
    if name != "task" and len(said) >= 2 and words == said:
        return False                       # the whole sentence is no value ("task" is meant to be)
    if (name in FREE_TEXT_ARGS or (tool, name) in GROUNDED_NAMES) and not _grounded(value, text):
        return False                       # words the user never said
    return True


def _tool_candidates(tool_name: str) -> str:
    """Live candidates from the PC so the small model picks an existing script / sink / audio app."""
    try:
        import pc_control
        if tool_name == "run_script":
            names = sorted(pc_control._my_scripts().keys())
            return f"Skrip yang tersedia: {', '.join(names[:20])}" if names else ""
        if tool_name == "audio_output":
            sinks = [s.get("description", "") for s in pc_control._sinks() if s.get("description")]
            return f"Keluaran suara yang tersedia: {', '.join(sinks[:8])}" if sinks else ""
        if tool_name == "app_volume":
            apps = pc_control.playing_audio_apps()
            return f"Aplikasi yang sedang bersuara: {', '.join(apps[:8])}" if apps else ""
    except Exception:
        pass
    return ""


def fill_args(fn, text: str, context: str = "", examples: list[tuple[str, dict]] | None = None) -> dict | None:
    """The arguments for this one skill from the sentence; None when a required one is missing or makes no sense (the
    online AI takes over). Optional ones only count when actually said; otherwise the skill's own default is right."""
    schema, required = args_schema(fn)
    if not schema["properties"]:
        return {}
    doc = re.sub(r"\s+", " ", inspect.getdoc(fn) or "")[:500]
    shots = "".join(f'\n"{t}" -> {json.dumps(a, ensure_ascii=False)}' for t, a in examples or [])
    extra_ctx = _tool_candidates(fn.__name__)
    full_ctx = " | ".join(c for c in (context, extra_ctx) if c)
    system = (f"Isi argumen dari kalimat user untuk fungsi ini: {doc}\n"
              "Nilainya diambil dari kata-kata user, hanya bagian yang dimaksud, bukan seluruh kalimat. Isi argumen "
              "lain hanya kalau user menyebutnya. Folder boleh diterjemahkan (unduhan = downloads, dokumen = documents). "
              "Jam dalam format 24 jam HH:MM (jam 9 malam = 21:00, jam 3 sore = 15:00, jam 5 pagi = 05:00)."
              + (f"\nContoh:{shots}" if shots else "") + (f"\n{full_ctx}" if full_ctx else ""))
    values = json.loads(local_llm.generate(system, text, schema=schema, max_tokens=120, temperature=0.0))
    params = inspect.signature(fn).parameters
    filled = {}
    for name, value in values.items():
        if name not in params or value in ("", None, []):
            continue
        if name in required:
            if name == "query" and isinstance(value, str):
                value = QUERY_VERB_RE.sub("", value.strip()) or value    # "putar lagu tenang" -> "lagu tenang"
            if not _makes_sense(fn.__name__, name, value, text, params):
                return None
            filled[name] = value
        elif (name in KEEP_FLAGS and isinstance(value, bool)) or name in CHOICES.get(fn.__name__, {}) or _said(value, text):
            filled[name] = value
    if "press_enter" in params and re.search(r"\b(?:enter|kirim)\b", text.lower()):
        filled["press_enter"] = True
    if "repeat" in filled and not REPEAT_RE.search(str(filled["repeat"]).lower()):
        del filled["repeat"]                # "besok" is not a repeat
    if any(name not in filled for name in required):
        return None
    return filled
