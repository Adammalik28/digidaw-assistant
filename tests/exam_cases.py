"""The exam for the local brain (tests/exam_brain.py): real and natural sentences that no fast route catches, with what
should happen: a tool's name (several: any of them), AI (handed to the online AI), CHAT (words, no tool), ASK
(garbled: asking back is right). Optional third item: words the arguments must contain."""
CASES = [
    # from real use
    ("tingkatkan kecerahan lampu", "lamp_brightness"), ("suhu laptop berapa", "get_temperature|get_system_status"),
    ("tolong ingat ya tanggal 20 november ada pameran buku", "add_event|remember|set_reminder_at"),
    ("putar lagu yang tenang untuk tidur", "play_youtube", "tidur"), ("bersihkan sampah", "clean_temp_files|empty_recycle_bin"),
    ("matikan komputer 30 menit lagi", "sleep_timer", "30"), ("reboot system", "restart_pc"),
    ("pesan dari siapa saja", "read_notifications"), ("cek limit gemini", "ai_status"),
    ("berapa menit lagi pengingat", "list_timers"), ("olahraga di jam 15 besok", "add_event|set_reminder_at", "15"),
    ("apa saja jadwal saya", "list_events"), ("buka situs gitar", "open_website|AI"),
    # devices and apps, said freely
    ("layarnya kesilauan nih tolong diturunin", "set_brightness"), ("aku mau dengerin lagu semangat buat olahraga", "play_youtube", "semangat"),
    ("baterai laptop masih sisa berapa ya", "get_system_status"), ("ram kepakai berapa sih", "get_system_status"),
    ("aplikasi apa aja yang lagi kebuka", "list_running_apps|list_windows"), ("salin tulisan halo dunia ke clipboard", "set_clipboard", "halo dunia"),
    ("isi clipboard sekarang apa", "get_clipboard"), ("foto layarnya dong", "take_screenshot"), ("kunci laptopnya", "lock_workstation"),
    ("tolong bukakan folder unduhan", "open_folder"), ("cari file laporan keuangan", "find_files", "laporan"),
    ("ingetin aku minum obat jam 9 malam", "set_reminder_at", "21:00"), ("bangunin aku jam 5 pagi besok", "set_alarm", "05:00"),
    ("timer masak mie 3 menit", "set_timer", "3"), ("matiin lampunya ya", "lamp_power"), ("bikin kamar jadi remang remang", "lamp_brightness|lamp_scene"),
    ("suasana nonton dong", "lamp_scene"), ("besok bakal hujan gak", "get_forecast|get_weather"),
    ("tulis halo semuanya di jendela ini", "type_text", "halo semuanya"), ("tekan control s", "press_keys", "s"),
    ("jalankan perintah df -h di terminal", "run_command", "df -h"), ("besarkan jendela ini", "window_action", "maxim"),
    ("ingat ya kopi favoritku americano", "remember", "americano"), ("buat folder baru namanya tugas kuliah", "create_folder", "tugas kuliah"),
    ("buatkan file teks isinya daftar belanja", "create_file|write_note"), ("tutup semua aplikasi", "close_all_apps"),
    ("hapus file laporan_lama.txt di folder downloads", "delete_file|find_files", "laporan_lama"), ("apa aja yang kamu ingat tentang aku", "recall_memories"),
    ("catat ide bikin channel youtube", "write_note", "channel"), ("lagu ini judulnya apa", "now_playing"),
    ("pindahkan jendela discord ke workspace dua", "move_to_workspace", "discord"), ("volume dikecilin dikit", "volume_down|set_volume"),
    ("jaringan aku pakai wifi apa", "get_network_info"), ("rangkumkan halaman yang lagi kebuka", "summarize_current_page|AI"),
    ("lihat layarku ada apa", "analyze_screen|AI"), ("kabari aku kalau baterainya penuh", "watch_for"),
    ("balas ke rina bilang aku telat", "reply_message", "telat"), ("tidurkan laptop", "sleep_pc"),
    # needs the internet or more than a local tool
    ("siapa juara liga inggris musim lalu", "AI"), ("harga emas hari ini berapa", "AI"), ("siapa presiden indonesia sekarang", "AI"),
    ("berita hari ini apa", "AI"), ("kurs dollar sekarang berapa", "AI"), ("ganti tema desktop jadi gelap", "AI|run_command|do_on_screen"),
    # the PC itself: settings, files, the screen
    ("wifinya dimatiin aja", "wifi_power"), ("bluetoothnya nyalain dong", "bluetooth"), ("hemat baterai dong laptopnya", "power_mode"),
    ("jangan kasih notif dulu ya", "do_not_disturb"), ("keluarin suaranya lewat hdmi", "audio_output", "hdmi"),
    ("coba liat isi folder unduhan", "list_folder"), ("pindahin foto.jpg ke folder dokumen", "move_file", "foto.jpg"),
    ("baca file catatan.txt di dokumen", "read_text_file", "catatan"), ("pencet tombol download yang di layar", "click_on_screen", "download"),
    ("buka firefox terus cari harga rtx 4060 lalu buka hasil pertamanya", "do_on_screen|AI"),
    ("ubah tema vs code jadi gelap lewat menunya", "do_on_screen|AI"), ("udah, hentiin aja kerjaan yang di layar", "stop_task"),
    ("sudah kuisi, terusin tugasnya", "resume_task"),
    # talk and general knowledge
    ("halo", "CHAT"), ("makasih ya", "CHAT"), ("kenapa bulan bersinar", "CHAT|AI"), ("berapa 20 kali 30", "CHAT|AI"),
    ("aku capek banget hari ini", "CHAT"), ("ceritakan tentang dirimu", "CHAT"), ("siapa presiden pertama indonesia", "CHAT|AI"),
    ("apa bedanya ram dan rom", "CHAT|AI"), ("kasih aku pantun lucu", "CHAT|AI"), ("kenapa langit biru", "CHAT|AI"),
    # garbled by the speech recognition
    ("sibuk sayang chord", "ASK"), ("dalam buku warna", "ASK"), ("novel manage", "ASK"), ("gigi daun buka wasit lost", "ASK"),
    ("itu video voice in video chat", "ASK"), ("rawr", "ASK"), ("kapal chord ps3", "ASK"),
]

# never used while tuning the prompt or the examples: the honest score
HOLDOUT = [
    ("lampunya kurang terang, tambahin", "lamp_brightness"), ("kasih tau kalau download udah selesai", "watch_for"),
    ("aku pengen denger lagu galau", "play_youtube", "galau"), ("musiknya berhentiin dulu", "media_control"),
    ("ganti ke lagu selanjutnya dong", "media_control"), ("hari ini tanggal berapa sih", "get_date"),
    ("laptopku panas nggak", "get_temperature|get_system_status"), ("sisa memori berapa", "get_system_status"),
    ("buatin catatan belanja telur dan gula", "write_note", "telur"), ("besok jam 7 bangunin ya", "set_alarm", "07:00"),
    ("kasih timer 15 menit buat istirahat", "set_timer", "15"), ("ingetin aku rapat jam 2 siang", "set_reminder_at", "14:00"),
    ("acara apa aja minggu depan", "list_events"), ("tambah jadwal dokter gigi tanggal 12 desember", "add_event", "dokter"),
    ("tolong cari file skripsi", "find_files", "skripsi"), ("buka folder dokumen", "open_folder"),
    ("bikin folder bernama foto liburan", "create_folder", "foto liburan"), ("copy kata terima kasih", "set_clipboard", "terima kasih"),
    ("ketikin selamat siang bapak", "type_text", "selamat siang"), ("tekan tombol enter", "press_keys", "enter"),
    ("keluar dari aplikasi ini", "window_action|close_application"), ("pindahin firefox ke workspace tiga", "move_to_workspace", "firefox"),
    ("ada yang baru di whatsapp", "read_notifications"), ("harga bensin hari ini", "AI"), ("siapa gubernur jakarta sekarang", "AI"),
    ("hasil pertandingan timnas semalam", "AI"), ("terjemahkan halaman ini", "AI"), ("selamat malam digidaw", "CHAT"),
    ("kamu pintar juga", "CHAT"), ("aku bosan nih", "CHAT"), ("apa ibu kota australia", "CHAT|AI"),
    ("pagar motor lampu kanan", "ASK"), ("yang tadi itu lho", "ASK"), ("sate bunga kemarin dua", "ASK"),
    ("nyalain wifinya lagi dong", "wifi_power"), ("performa laptopnya naikin, mau main game", "power_mode"),
    ("bunyinya pindahin ke speaker bluetooth", "audio_output|bluetooth"), ("notifnya diemin dulu", "do_not_disturb"),
    ("pencet tulisan setuju yang ada di layar", "click_on_screen", "setuju"), ("di firefox bikin tab baru terus masuk ke youtube", "do_on_screen|open_website|AI"),
    ("file apa aja yang ada di folder gambar", "list_folder"), ("salin laporan.pdf ke folder dokumen", "copy_file", "laporan"),
    ("batalin kerjaan yang lagi jalan di layar", "stop_task"), ("ganti nama file draft.txt jadi final.txt", "rename_file", "final"),
]
