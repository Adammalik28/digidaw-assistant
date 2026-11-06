"""
Teaching the small local model what its choices should be. Its weights can't be trained on this laptop (a LoRA run needs about 10 GB of video memory, it has 4), so it is
taught the way that works without any: what really reached it, with what should have happened (logs of 30 Oct - 5 Nov 2026, labelled by hand), is shown to it as examples when
a new sentence looks like one. Names of people and places left out: this file goes into git. tests/teach_eval.py measures it on sentences left out of the teaching.

Each: (sentence, what's right; several separated by |, the first is what is shown). AI = hand to the online AI, CHAT, KNOW, ASK = garbled, ask back.
"""
import re
import zlib

import local_router

TAUGHT = [
    # meant something
    ("youtube chrome gigi", "play_youtube"), ("tutup white droid", "close_application"), ("tutup ta", "close_current_tab|close_application|ASK"),
    ("mengapa langit biru", "KNOW"), ("kucing mau makan apa", "KNOW"), ("siapa anda", "CHAT"), ("buatkan saya cerita singkat", "KNOW"),
    ("saya sedang menggunakan queen atau gemini", "CHAT|ASK"), ("siapa penemu lampu", "KNOW"), ("buatkan saya puisi", "KNOW"), ("halo", "CHAT|small_talk"), ("manfaat kopi", "KNOW"),
    ("analisis layar", "AI"), ("buatkan pantun", "KNOW"), ("rangkum tentang diri saya", "recall_memories"), ("tutup mox", "close_application"),
    ("tutup semua tuner", "close_application"), ("buatkan puisi random dan masukkan ke editor", "write_note|AI"), ("minecraft firefox ke work ps3", "move_to_workspace|ASK"),
    ("buka warship lo", "open_application"), ("perbaiki project-nya tidak bisa berjalan", "AI"), ("buka beetal", "open_application|ASK"), ("buka beetalk", "open_application|open_website"),
    ("youtube mouse", "open_many|ASK"), ("analisis situs", "AI"), ("berikan saya urutan film iron man", "KNOW|AI"), ("berikan briefing harian", "AI|daily_recap"),
    ("siapa kamu", "CHAT"), ("cuaca di bandung hari ini", "get_weather"), ("cuaca di surabaya hari ini", "get_weather"), ("222 enter", "type_text|press_keys"),
    ("apa itu cash flow", "KNOW"), ("putar lagi do i wanna know", "play_youtube"), ("buka note dan ketik halo", "write_note|AI|open_application"),
    ("buatkan kalkulator dengan tema rgb", "AI|create_project|do_on_screen"), ("tutup file mana", "close_application"), ("buka vi", "open_application"), ("apa itu fotosintesis", "KNOW"),
    ("tutup pro", "close_application|ASK"), ("buat lelucon", "KNOW"), ("hitung 1 sampai 10", "KNOW"), ("buka get up", "open_application"), ("buka game free fire pro", "open_application"),
    ("charger lagu kapal langit", "play_youtube"), ("tutup chro", "close_application"), ("buka gta", "open_application"), ("buka white dro", "open_application"), ("buka why droid", "open_application"),
    ("putar lagu yang tenang untuk tidur", "play_youtube"), ("suhu", "get_temperature"), ("namanya siapa", "CHAT|ASK"), ("youtube.com", "open_website"), ("wah keren", "CHAT"),
    ("berita di surabaya hari ini", "AI|get_news"), ("harga bitcoin hari ini", "AI"), ("berita terbaru tentang teknologi", "AI|get_news"), ("siapa presiden indonesia sekarang", "AI"),
    ("buka game dvd", "open_application"), ("pagi", "CHAT|small_talk"), ("tutup file", "close_application"), ("kenapa langit biru", "KNOW"), ("siapa penemu telepon", "KNOW"), ("harga emas hari ini", "AI"),
    ("siapa presiden pertama indonesia", "KNOW"), ("siapa juara liga inggris musim lalu", "AI"), ("bandingkan kelebihan dan kekurangan belajar pagi dan malam", "KNOW|AI"), ("kenapa bulan bersinar", "KNOW"),
    ("berita hari ini", "AI|get_news"), ("matikan komputer 30 menit lagi", "sleep_timer"), ("pigi chord digi buka terminal", "open_application"), ("nikita 3 + 3", "KNOW"),
    ("olahraga di jam 15 besok", "add_event|set_reminder_at"), ("berhitung 1 sampai 10", "KNOW"), ("cek limit gemini", "ai_status"), ("gigi daun buka wasit lost", "open_application"),
    ("instagram tik to", "open_many|open_website"), ("gigi apakah ada pesan masuk", "read_notifications"), ("ceritakan tentang anda", "CHAT"), ("back to friend", "play_youtube|ASK"),
    ("tutup kamera", "close_application|ASK"), ("reboot system", "restart_pc"), ("youtube chrome", "open_website|focus_window|open_application|ASK|open_many"), ("buka ada pesan masuk", "read_notifications"),
    ("pesan dari siapa saja", "read_notifications"), ("yang dari whatsapp", "read_notifications|ASK"), ("gigi buka fire", "open_application|focus_window"), ("sambungan keyboard", "bluetooth"),
    ("buka medanpedia", "open_website|open_application"), ("situs apa ini", "AI|active_window_info"), ("anjuka, putar lagu", "play_youtube|media_control|ASK"), ("shakira", "play_youtube|ASK"),
    ("berita hari ini di indonesia", "AI|get_news"), ("carikan berita elon mas hari ini", "AI|get_news"), ("atur ke mode performa", "power_mode"), ("cyber security besok jam jam 07", "add_event"),
    ("cek email", "read_notifications|AI"), ("tanggal 7 november 6 acaranya kuliah", "add_event"), ("tutup semua", "close_all_apps"), ("putar musik yang enak", "play_youtube"),
    # scraps and mishearings: asking back is right, talking about them isn't
    ("buka aplikasi", "ASK|open_application"), ("cover", "ASK"), ("dredown", "ASK"), ("kalau", "ASK"), ("langsung buka", "ASK"), ("buka dikit dong", "ASK"), ("ti", "ASK"), ("dino", "ASK"), ("begitu dong", "ASK|CHAT"),
    ("i don't make you happy", "ASK|CHAT"), ("dicky", "ASK"), ("dingin", "ASK"), ("da", "ASK"), ("lucky draw", "ASK"), ("gigi da", "ASK"), ("you do", "ASK"), ("my", "ASK"), ("lama lu belum buka x", "ASK"),
    ("dikit dong", "ASK"), ("detik claude", "ASK"), ("detik claude resume", "ASK"), ("apakah perangkat saya masih", "ASK"), ("ke", "ASK"), ("sekalian lah sekali", "ASK"), ("ini hp baru dia", "ASK|CHAT"),
    ("dikit", "ASK"), ("rawr", "ASK"), ("roar", "ASK"), ("ror", "ASK"), ("book", "ASK"), ("just the part of it", "ASK"), ("sibuk sayang chord", "ASK"), ("dingin dong", "ASK|CHAT"), ("drawner", "ASK"),
    ("jungan", "ASK"), ("youtube dan kita", "ASK"), ("buka www", "ASK"), ("daun", "ASK"), ("ya tunggu", "ASK|CHAT"), ("persamaan nonton tv sana men nggak ada laptop", "ASK|AI"), ("salah", "ASK|CHAT"),
    ("rang", "ASK"), ("tidak ke whatsapp", "ASK"), ("jarvis", "ASK|CHAT"), ("dan tik", "ASK"), ("this", "ASK"), ("vivo ke-1", "ASK"), ("youtube file mana", "ASK"), ("my baby", "ASK|play_youtube"), ("novel manage", "ASK"),
    ("buka digital buka ti", "ASK"), ("minta ke words s1", "ASK|switch_workspace"), ("bilakah firefox", "ASK|focus_window"), ("anjay", "ASK|CHAT"), ("iki", "ASK"), ("apakah bisa", "ASK|CHAT"), ("lagi", "ASK"),
    ("ngento", "ASK"), ("kalori", "ASK|KNOW"), ("nanti cepet ah", "ASK"), ("buka di", "ASK"), ("itu video voice in video chat", "ASK"), ("kok bisa", "ASK|CHAT"), ("tulang lunak", "ASK|KNOW"), ("dalam rangka apa", "ASK"),
    ("do you", "ASK"), ("dinda chord ps3", "ASK"), ("binder tolol", "ASK"), ("ceritakan", "ASK"), ("ke print ps ke print ps", "ASK|switch_workspace"), ("kok orang yang namanya bisnis", "ASK"),
    ("download power ps3", "ASK|AI"), ("ido", "ASK"), ("dalam buku merah", "ASK|lamp_color"), ("dalam buku warna", "ASK"), ("pindahkan whatsapp ke uang", "ASK|move_to_workspace"), ("7 daun gak berapa sekarang", "ASK|get_time"),
    ("coba lagi", "ASK"), ("maki kandar ya", "ASK"), ("jam terbang", "ASK"), ("tolol", "ASK|CHAT"), ("to", "ASK"), ("two player pro", "ASK"), ("balap", "ASK"), ("hidup rindu", "ASK"), ("muka perminar", "ASK|open_application|_ask"),

    # fourth round: what reached the model in real use since the third
    ("coba sambungkan lagi", "wifi_connect|bluetooth|ASK"), ("buka hasil pertama", "do_on_screen|ASK"), ("tutup vivo", "ASK|close_application"), ("hijau", "ASK|lamp_color"), ("buka yang pertama", "ASK|do_on_screen|open_website"),
    ("tambahkan agenda", "add_event"), ("tutup revo", "close_application|ASK"), ("youtube library office", "ASK|open_website"), ("buka agenda", "open_agenda_page|list_events"),
    ("tambah agenda", "add_event"), ("tanggal 7 november 6 acaranya kuli", "add_event"), ("tutup agenda hijau", "close_application|ASK"), ("tambah rutini", "ASK"), ("gambar rutinitas baru", "ASK"), ("modul jam terbang", "ASK"),
    ("7 ubah lampu ke warna putih", "lamp_color"), ("bala empuarlomerah", "ASK"), ("buat rutini", "ASK"), ("model ga", "ASK"), ("baby doll", "ASK"), ("tambahkan agenda di november", "add_event"), ("buat agenda tanggal 6", "add_event"),
    ("fi", "ASK"), ("ke 20%", "ASK|set_volume"), ("untuk file manager", "close_application|open_application|ASK"), ("tindakan whatsapp ke workspace", "move_to_workspace"), ("full screen firefox", "window_action"), ("tambah agenda tanggal", "add_event"),
    ("nyanyi dong", "CHAT|play_youtube"), ("untuk pencarian", "ASK|close_application"), ("tutup pencarian", "close_application"), ("itu volvo", "ASK"), ("iya", "CHAT|ASK"),
    ("putuskan koneksi bluetooth headset", "bluetooth"), ("apakah", "ASK"), ("apakah discord dan telegram masih berjalan", "list_running_apps"),
]

MIN_SIMILARITY = 0.34
_NAMES = {"AI": local_router.AI, "CHAT": local_router.CHAT, "KNOW": local_router.KNOW, "ASK": local_router.ASK}
_cache: dict = {}


def part(sentence: str) -> str:
    """Every sentence falls by its own text into "ajar" (may be taught) or "uji" (left out, for an honest score)."""
    return "uji" if zlib.crc32(sentence.encode()) % 2 else "ajar"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _grams(text: str) -> frozenset:
    key = _norm(text)
    if key not in _cache:
        padded = f"  {key} "
        _cache[key] = frozenset(padded[i:i + 3] for i in range(len(padded) - 2))
    return _cache[key]


def similar(text: str, limit: int = 3, only: str = "", leave_out: str = "") -> list[tuple[str, str]]:
    """The taught sentences most like this one: (sentence, choice), best first. only: "ajar" while measuring."""
    grams = _grams(text)
    scored = []
    for sentence, choice in TAUGHT:
        if (only and part(sentence) != only) or sentence == leave_out:
            continue
        other = _grams(sentence)
        score = len(grams & other) / max(1, len(grams | other))
        if score >= MIN_SIMILARITY:
            first = choice.split("|")[0]
            scored.append((score, sentence, _NAMES.get(first, first)))
    scored.sort(key=lambda s: -s[0])
    return [(sentence, choice) for _, sentence, choice in scored[:limit]]
