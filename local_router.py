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
    "delete_event": "hapus acara dari agenda", "get_weather": "cuaca sekarang", "get_forecast": "perkiraan cuaca, hujan, besok",
    "get_system_status": "CPU, RAM, disk, baterai", "get_temperature": "suhu laptop",
    "get_network_info": "wifi yang dipakai, IP, jaringan", "open_folder": "buka folder", "open_file": "buka file",
    "find_files": "cari file", "create_folder": "buat folder baru", "create_file": "buat file baru berisi teks",
    "delete_file": "hapus file", "remember": "simpan fakta / kesukaan user untuk diingat",
    "recall_memories": "sebutkan apa yang kamu ingat tentang user", "learned_habits": "kebiasaan user yang dipelajari",
    "lock_workstation": "kunci laptop", "shutdown_pc": "matikan komputer sekarang", "restart_pc": "restart komputer",
    "sleep_pc": "tidurkan laptop", "run_command": "jalankan perintah terminal yang disebut user",
    "clean_temp_files": "bersihkan file sementara / sampah", "empty_recycle_bin": "kosongkan tempat sampah",
    "read_notifications": "baca pesan / notifikasi masuk", "reply_message": "balas / kirim pesan WhatsApp",
    "watch_for": "kabari user kalau sesuatu terjadi nanti", "list_watches": "apa yang sedang dipantau",
    "cancel_watch": "batalkan pantauan", "daily_recap": "rangkuman hari ini", "screen_time": "lama pakai laptop hari ini",
    "performance_report": "laporan kinerja Digidaw", "sun_today": "jam matahari terbit / terbenam",
    "ai_status": "status / limit AI (Gemini)", "get_selected_text": "baca teks yang sedang dipilih",
    "wifi_power": "nyalakan / matikan Wi-Fi", "wifi_connect": "sambung ke jaringan Wi-Fi tertentu",
    "bluetooth": "nyalakan / matikan Bluetooth, sambung / putuskan perangkat", "power_mode": "mode daya: hemat, seimbang, performa",
    "fan_speed": "atur kecepatan kipas laptop, atau tanya kecepatannya", "open_script_manager": "buka pengelola / daftar skrip saya",
    "run_script": "jalankan skrip buatan saya di folder myskrip menurut namanya", "close_script_window": "tutup jendela skrip",
    "do_not_disturb": "mode jangan ganggu, senyapkan notifikasi", "audio_output": "pindahkan keluaran suara: speaker, HDMI, USB, headset",
    "screen_power": "matikan / nyalakan layar", "list_folder": "lihat isi sebuah folder",
    "read_text_file": "baca isi file teks", "move_file": "pindahkan file", "copy_file": "salin file ke folder lain",
    "rename_file": "ganti nama file", "click_on_screen": "klik satu benda yang terlihat di layar",
    "do_on_screen": "kerjakan tugas beberapa langkah di layar: mencari di browser, mengisi formulir, mengubah pengaturan aplikasi",
    "stop_task": "hentikan tugas di layar yang sedang berjalan", "resume_task": "lanjutkan tugas di layar yang menunggu",
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
    ("daun meja piring", ASK), ("gigi chord buku", ASK), ("itu dari sini kok", ASK),
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
- {ASK}: kalimatnya kacau, terpotong, atau tidak jelas maksudnya. Jangan menebak.

Kalau user minta MELAKUKAN sesuatu di komputer atau menanyakan keadaan komputer, pilih alatnya.

Contoh:
{examples}"""


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
           "watch_for": {"what": ["unduhan", "pesan", "hujan_reda", "hujan", "baterai", "program", "internet", "situs"]},
           "get_forecast": {"when": ["nanti", "hari ini", "besok", "lusa"]},
           "bluetooth": {"action": ["on", "off", "connect", "disconnect"]}}
NUMBER_RE = re.compile(r"\d|\b(?:nol|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|belas|puluh|ratus|"
                       r"setengah|seperempat|sejam|semenit)\b", re.IGNORECASE)
REPEAT_RE = re.compile(r"setiap|tiap|hari kerja|akhir pekan|senin|selasa|rabu|kamis|jumat|sabtu|minggu")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FREE_TEXT_ARGS = {"query", "text", "content", "fact", "message", "target", "task", "title", "description", "new_name", "command"}
GROUNDED_NAMES = {("wifi_connect", "name"), ("audio_output", "name"), ("run_script", "name"), ("power_mode", "mode")}      # a network or an output: never a guess
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


def fill_args(fn, text: str, context: str = "", examples: list[tuple[str, dict]] | None = None) -> dict | None:
    """The arguments for this one skill from the sentence; None when a required one is missing or makes no sense (the
    online AI takes over). Optional ones only count when actually said; otherwise the skill's own default is right."""
    schema, required = args_schema(fn)
    if not schema["properties"]:
        return {}
    doc = re.sub(r"\s+", " ", inspect.getdoc(fn) or "")[:500]
    shots = "".join(f'\n"{t}" -> {json.dumps(a, ensure_ascii=False)}' for t, a in examples or [])
    system = (f"Isi argumen dari kalimat user untuk fungsi ini: {doc}\n"
              "Nilainya diambil dari kata-kata user, hanya bagian yang dimaksud, bukan seluruh kalimat. Isi argumen "
              "lain hanya kalau user menyebutnya. Folder boleh diterjemahkan (unduhan = downloads, dokumen = documents). "
              "Jam dalam format 24 jam HH:MM (jam 9 malam = 21:00, jam 3 sore = 15:00, jam 5 pagi = 05:00)."
              + (f"\nContoh:{shots}" if shots else "") + (f"\n{context}" if context else ""))
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
