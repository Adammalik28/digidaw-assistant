"""
Does the local brain follow a conversation? The second sentence of each pair means nothing alone. No tool is run (they
only record); needs Ollama. Not in run_all.sh.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/follow_exam.py
"""
import functools
import os
import pathlib
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agenda   # noqa: E402
import alarms   # noqa: E402
import brain    # noqa: E402
import metrics  # noqa: E402
metrics.count = lambda *a, **k: None      # (an exam isn't use: today's real counters stay as they are)

# (first sentence, Digidaw's reply, tool that ran, its arguments), next sentence, what must happen: a tool's name, "AI" for the online AI, or a regex in the reply
CASES = [
    (("ibu kota jawa barat apa", "Ibu kota Jawa Barat adalah Bandung.", "", {}), "kalau jawa tengah", "semarang"),
    (("ibu kota jepang apa", "Ibu kota Jepang adalah Tokyo.", "", {}), "mata uangnya apa", "yen"),
    (("siapa presiden pertama indonesia", "Presiden pertama Indonesia adalah Soekarno.", "", {}), "wakilnya siapa", "hatta"),
    (("siapa presiden kedua indonesia", "Presiden kedua Indonesia adalah Soeharto.", "", {}), "kalau yang ketiga", "habibie"),
    (("berapa jumlah planet di tata surya", "Ada delapan planet.", "", {}), "yang terbesar apa", "jupiter"),
    (("siapa penulis laskar pelangi", "Penulis Laskar Pelangi adalah Andrea Hirata.", "", {}), "kalau bumi manusia", "pramoedya"),
    (("kapan hari kartini", "Hari Kartini diperingati setiap 21 April.", "", {}), "kalau hari ibu", "22 desember"),
    (("apa itu fotosintesis", "Fotosintesis adalah proses tumbuhan membuat makanan dengan bantuan cahaya matahari.", "", {}), "kenapa daun hijau", "klorofil"),
    (("buka firefox", "Firefox sudah terbuka.", "open_application", {"name": "firefox"}), "tutup lagi", "close_application"),
    (("nyalakan lampu", "Lampu menyala.", "lamp_power", {"on": True}), "warna biru", "lamp_color"),
    (("nyalakan lampu", "Lampu menyala.", "lamp_power", {"on": True}), "redupin dikit", "lamp_brightness"),
    (("matikan lampu", "Lampu mati.", "lamp_power", {"on": False}), "nyalain lagi", "lamp_power"),
    (("cuaca hari ini", "Hari ini cerah.", "get_weather", {}), "kalau besok", "get_forecast"),
    (("set timer 5 menit", "Timer 5 menit dimulai.", "set_timer", {"minutes": 5}), "batalin", "say|undo_last_alarm"),
    (("volume 30", "Volume 30 persen.", "set_volume", {"level": 30}), "naikin lagi", "volume_up"),
    (("putar lagu coldplay", "Memutar Coldplay.", "play_youtube", {"query": "coldplay"}), "yang lain", "media_control|play_youtube"),
    (("putar lagu coldplay", "Memutar Coldplay.", "play_youtube", {"query": "coldplay"}), "pause dulu", "media_control"),
    (("ada acara apa besok", "Besok tidak ada acara.", "list_events", {}), "kalau lusa", "jadwal|acara"),
    (("jam berapa sekarang", "Sekarang jam sepuluh.", "get_time", {}), "tanggal berapa", "get_date"),
    (("berapa baterai", "Baterai 80 persen.", "get_system_status", {}), "suhunya berapa", "get_temperature"),
    (("buka whatsapp", "WhatsApp terbuka.", "open_application", {"name": "whatsapp"}), "pindahin ke workspace 2", "move_to_workspace"),
    (("buka youtube", "YouTube terbuka.", "open_website", {"site": "youtube"}), "cari lagu santai", "search_youtube|play_youtube|google_search|open_website|media_control"),
    (("buka firefox", "Firefox terbuka.", "open_application", {"name": "firefox"}), "kalau chrome", "open_application"),
    (("tutup discord", "Discord ditutup.", "close_application", {"name": "discord"}), "sama spotify juga", "close_application|close_many"),
    (("matikan wifi", "Wifi mati.", "wifi_power", {"on": False}), "nyalain lagi", "wifi_power|toggle_wifi|set_wifi"),
    (("tampilkan catatan", "Ada tiga catatan.", "list_notes", {}), "buka yang terakhir", "open_last_note"),
    (("set alarm jam 7", "Alarm jam tujuh dipasang.", "set_alarm", {"time": "07:00"}), "ganti jadi jam 8", "set_alarm"),
    (("ingatkan saya minum obat jam 9", "Baik, jam sembilan.", "set_reminder_at", {}), "batalkan", "say|undo_last_alarm"),
    (("halo", "Halo, ada yang bisa dibantu?", "", {}), "bagaimana kabarmu", "baik|kabar"),
    (("siapa kamu", "Saya Digidaw.", "", {}), "kamu bisa apa saja", "what_i_can_do"),
    # written after the rewrite worked: are the examples above just what it was tuned on?
    (("siapa presiden keenam indonesia", "Presiden keenam Indonesia adalah Susilo Bambang Yudhoyono.", "", {}), "kalau yang kedelapan", "prabowo"),
    (("ibu kota bali apa", "Ibu kota Bali adalah Denpasar.", "", {}), "kalau lampung", "bandar lampung"),
    (("mata uang thailand apa", "Mata uang Thailand adalah baht.", "", {}), "ibu kotanya apa", "bangkok"),
    (("gunung tertinggi di dunia apa", "Gunung Everest.", "", {}), "kalau di indonesia", "puncak jaya"),
    (("kapan hari pahlawan", "Hari Pahlawan pada 10 November.", "", {}), "kalau hari guru", "25 november"),
    (("buka spotify", "Spotify terbuka.", "open_application", {"name": "spotify"}), "kalau discord", "open_application"),
    (("tutup telegram", "Telegram ditutup.", "close_application", {"name": "telegram"}), "terus firefox", "close_application"),
    (("cuaca di jakarta", "Jakarta cerah.", "get_weather", {"city": "jakarta"}), "kalau di bandung", "get_weather|get_forecast"),
    (("volume 50", "Volume 50 persen.", "set_volume", {"level": 50}), "kalau 20", "set_volume"),
    (("kecerahan 40", "Kecerahan 40 persen.", "set_brightness", {"level": 40}), "kalau 80", "set_brightness"),
    (("lampu warna merah", "Lampu merah.", "lamp_color", {"color": "merah"}), "kalau hijau", "lamp_color"),
    (("timer 10 menit", "Timer 10 menit dimulai.", "set_timer", {"minutes": 10}), "kalau 20 menit", "timer 20"),
    (("putar lagu dewa 19", "Memutar Dewa 19.", "play_youtube", {"query": "dewa 19"}), "kalau sheila on 7", "play_youtube"),
    (("cari di google resep rendang", "Mencari resep rendang.", "google_search", {"query": "resep rendang"}), "kalau resep soto", "google_search"),
    (("baterai berapa", "Baterai 80 persen.", "get_system_status", {}), "kalau ram", "get_system_status|get_memory|get_ram"),
    # said after anything, these aren't follow-ups: no harm may come of them
    (("buka firefox", "Firefox terbuka.", "open_application", {"name": "firefox"}), "yang benar saja", "-"),
    (("ibu kota jepang apa", "Ibu kota Jepang adalah Tokyo.", "", {}), "terus kenapa", "-"),
    (("cuaca hari ini", "Hari ini cerah.", "get_weather", {}), "kalau begitu oke", "-"),
]

ran: list[str] = []


def _recorder(fn, name):
    @functools.wraps(fn)
    def record(*args, **kwargs):
        ran.append(name)
        return f"Selesai: {name}."
    return record


brain.TOOL_MAP.update({n: _recorder(f, n) for n, f in list(brain.TOOL_MAP.items())})
agenda.STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "agenda.json"
alarms.STATE_FILE = agenda.STATE_FILE.with_name("alarms.json")
agenda.add_from_sentence = lambda text: "Selesai: add_event."
brain.ask_agent = lambda text: (ran.append("AI"), "[Gemini]")[1]
brain._local_search_answer = lambda text: None
good = 0
for (first, said, tool, args), second, want in CASES:
    ran.clear()
    brain._system_notes.clear()
    brain._dialog[:] = [{"user": first, "reply": said, "brain": "local", "at": time.time()}]
    brain._last_fast.update(tool=tool, kwargs=args, at=time.time())
    brain._last_action.update(calls=[], at=0)
    brain._recent_app.update(opened=args.get("name", "") if tool == "open_application" else "", closed=args.get("name", "") if tool == "close_application" else "", at=time.time())
    start = time.time()
    second = brain._expand_follow_up(second)
    reply = brain.fast_route(second)
    if reply is None:
        reply = brain._think(second)
    took = time.time() - start
    is_tool = "_" in want or want == "AI"
    ok = (not ran) if want == "-" else (ran and ran[0] in want.split("|")) if is_tool else bool(not ran and reply and not reply.startswith("Maaf") and re.search(want, reply.lower()))
    good += bool(ok)
    print(f"{'  ok ' if ok else 'GAGAL'} {took:4.1f}s {first[:26]:26s} > {second[:24]:24s} {ran[:2]} {'' if ran else (reply or '')[:60]!r}", flush=True)
print(f"== {good}/{len(CASES)} benar")
sys.stdout.flush()
os._exit(0)
