"""
An exam for teach_eval.py that nothing is taught from. What is taught, and the sentences kept aside from teaching while measuring, are in teaching.py.
"""

# written 5 Nov 2026 before any teaching, never tuned for: how someone talks to an assistant, some garbled the way the microphone garbles
HOLDOUT_2 = [
    ("tolong kecilin suaranya dikit", "volume_down"), ("gedein volumenya", "volume_up"), ("musiknya pause dulu", "media_control"), ("ganti lagu yang lain", "media_control"),
    ("setel lagu sheila on 7", "play_youtube"), ("mau denger lagu dewa 19", "play_youtube"), ("lampunya jadi biru dong", "lamp_color"), ("redupin lampu kamar", "lamp_brightness"),
    ("matiin lampunya", "lamp_power"), ("bangunin aku jam 5 pagi", "set_alarm"), ("ingetin aku minum obat jam 8 malam", "set_reminder_at"), ("timer 3 menit buat mie", "set_timer"),
    ("jam berapa sih sekarang", "get_time"), ("hari ini tanggal berapa ya", "get_date"), ("bakal hujan nggak hari ini", "get_forecast|get_weather"), ("cuaca di jakarta gimana", "get_weather"),
    ("baterai laptop masih berapa", "get_system_status"), ("laptopnya panas nggak", "get_temperature"), ("buka spotify", "open_application"), ("tolong bukain kalkulator", "open_application"),
    ("tutup discord dong", "close_application"), ("pindah ke telegram", "focus_window|open_application"), ("ke workspace tiga", "switch_workspace"), ("lempar firefox ke workspace dua", "move_to_workspace"),
    ("buka situs tokopedia", "open_website"), ("cari resep rendang", "AI|open_website|google_search"), ("ketik selamat ulang tahun", "type_text"), ("catat ide bikin konten baru", "write_note"),
    ("ada pesan baru gak di whatsapp", "read_notifications"), ("matikan wifi", "wifi_power"), ("sambungin headset bluetooth", "bluetooth"), ("kunci laptopnya", "lock_workstation"),
    ("kenapa kucing suka tidur", "KNOW"), ("ibukota australia apa", "KNOW"), ("buatin pantun lucu", "KNOW"), ("jelaskan apa itu inflasi", "KNOW"), ("berapa 12 dikali 12", "KNOW|calculate"),
    ("siapa menteri pendidikan sekarang", "AI"), ("kurs dolar hari ini", "AI"), ("skor persib tadi malam", "AI"), ("rangkum halaman ini", "AI|summarize_current_page"), ("buatkan website toko baju", "AI|create_project"),
    ("selamat malam digidaw", "CHAT|small_talk"), ("makasih ya", "CHAT|small_talk"), ("aku capek banget hari ini", "CHAT"), ("kamu lagi ngapain", "CHAT"), ("kamu keren", "CHAT"),
    ("meja lari hijau tiga", "ASK"), ("ya itu tadi yang", "ASK"), ("kapal buka kok", "ASK"), ("dong gitu kan", "ASK"), ("sana lewat aja", "ASK"), ("the the it", "ASK"), ("pak ke mana lampu", "ASK"),
    ("nasi", "ASK"), ("biru", "ASK|lamp_color"), ("oh", "ASK"), ("hmm iya gitu", "ASK|CHAT"), ("loh kok", "ASK"), ("tadi siapa", "ASK"),
]

# written 5 Nov 2026 after the teaching above was finished, before running them even once: the score to quote. Don't tune against it; to measure again later, add new sentences.
HOLDOUT_3 = [
    ("tolong setel lagu noah", "play_youtube"), ("putarkan lagu kangen band", "play_youtube"), ("lagunya stop dulu", "media_control"), ("lanjut lagunya", "media_control"),
    ("musik sebelumnya dong", "media_control"), ("suaranya kekecilan", "volume_up|ASK"), ("pelanin suaranya", "volume_down"), ("volume jadiin 40", "set_volume"),
    ("nyalain lampu", "lamp_power"), ("lampunya ganti merah", "lamp_color"), ("lampu kamar terangin", "lamp_brightness"), ("suasana lampu buat film", "lamp_scene"),
    ("ingatkan aku rapat jam 2 siang", "set_reminder_at|add_event"), ("alarm jam setengah 6 pagi", "set_alarm"), ("hitung mundur 5 menit", "set_timer"), ("batalkan alarm yang tadi", "cancel_timers"),
    ("cuaca hari ini gimana", "get_weather"), ("besok hujan gak ya", "get_forecast"), ("jam berapa ini", "get_time"), ("tanggal berapa hari ini", "get_date"),
    ("ram kepakai berapa", "get_system_status"), ("cek suhu cpu", "get_temperature"), ("tolong bukain vscode", "open_application"), ("jalankan kalkulator", "open_application"),
    ("tutup telegram", "close_application"), ("keluar dari discord", "close_application"), ("pindah ke vscode", "focus_window|open_application"), ("kirim whatsapp ke workspace 3", "move_to_workspace"),
    ("buka shopee", "open_website|open_application"), ("bukain youtube", "open_website|open_application"), ("tolong ketikkan nama saya adam", "type_text"), ("tekan tombol enter", "press_keys"),
    ("catat nomor hp baru", "write_note"), ("ada notif apa aja", "read_notifications"), ("hidupkan bluetooth", "bluetooth"), ("wifi aku nyambung ke mana", "get_network_info"),
    ("kenapa pelangi berwarna", "KNOW"), ("siapa penulis laskar pelangi", "KNOW"), ("buatkan puisi tentang ibu", "KNOW"), ("apa itu machine learning", "KNOW"), ("22 tambah 33 berapa", "KNOW|calculate"),
    ("harga dolar sekarang", "AI"), ("hasil pertandingan timnas kemarin", "AI"), ("berita politik terbaru", "AI|get_news"), ("apa yang ada di layar ini", "AI"), ("buatkan aplikasi catatan", "AI|create_project"),
    ("hari ini aku senang sekali", "CHAT"), ("kamu baik banget", "CHAT"), ("aku ngantuk", "CHAT"), ("kamu umur berapa", "CHAT"), ("kamu cewek atau cowok", "CHAT"),
    ("ya gitu deh pokoknya", "ASK|CHAT"), ("bawa sini sana itu", "ASK"), ("kuda terbang biru", "ASK"), ("pas tadi anu", "ASK"), ("the what now", "ASK"), ("gelas", "ASK"), ("hah", "ASK"), ("eh iya", "ASK|CHAT"), ("nggak tau deh", "ASK|CHAT"),
]

# written before the fourth teaching round, run once at its end
HOLDOUT_4 = [
    ("tolong bukain aplikasi kalkulator", "open_application"), ("gedein suaranya dikit", "volume_up|set_volume"), ("lagi muter lagu apa nih", "now_playing"), ("sambungin ke headset bluetooth", "bluetooth"),
    ("laptop gue panas gak", "get_temperature"), ("ram kepake berapa", "get_system_status"), ("hapus agenda hari jumat", "delete_event"), ("besok aku ada kuliah gak", "list_classes|list_events"),
    ("ingetin aku jemur baju sejam lagi", "set_reminder_at|set_timer"), ("bangunin aku jam lima pagi", "set_alarm"), ("stop musiknya", "media_control"), ("ganti ke video berikutnya", "media_control"),
    ("cariin tutorial masak rendang di youtube", "search_youtube|play_youtube"), ("buka situs tokopedia", "open_website"), ("screenshot dong", "take_screenshot"), ("kecilin jendela ini", "window_action"),
    ("balik ke discord", "focus_window"), ("bikin catatan beli sabun", "write_note"), ("baca pesan yang masuk", "read_notifications"), ("nyalain lampu kamar", "lamp_power"),
    ("lampunya kuning dong", "lamp_color"), ("mode tidur lampu", "lamp_scene"), ("kipasnya gedein", "fan_speed"), ("sisa baterai berapa persen", "get_system_status"),
    ("cek koneksi internet", "get_network_info|internet_speed"), ("ingat saya suka kopi hitam", "remember"), ("apa yang kamu tahu tentang saya", "recall_memories"),
    ("siapa penemu pesawat", "KNOW"), ("kenapa laut warnanya biru", "KNOW"), ("apa bedanya ssd sama hdd", "KNOW"), ("berapa lama cahaya matahari sampai ke bumi", "KNOW"),
    ("ceritain lelucon dong", "CHAT|KNOW"), ("makasih ya bro", "CHAT"), ("kamu lagi ngapain", "CHAT"), ("gimana perasaanmu hari ini", "CHAT"), ("saya lagi bete nih", "CHAT"),
    ("harga ps5 sekarang berapa", "AI"), ("siapa pemenang ballon dor tahun ini", "AI"), ("buatkan saya surat izin sakit", "AI|create_file|KNOW"), ("lihat layar saya dan jelaskan errornya", "AI"), ("jelaskan cara kerja mesin diesel secara detail", "AI|KNOW"),
    ("blablabla wortel terbang", "ASK"), ("yang kemarin itu loh", "ASK"), ("hujan pintu sepuluh", "ASK"),
]
