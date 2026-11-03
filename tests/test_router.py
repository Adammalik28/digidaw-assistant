"""
Golden tests for Digidaw's command router, from real (often misheard) commands in the log.

Run:  PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_router.py
(also works with pytest). Nothing is opened, closed or typed: only the decision (tool + arguments) is checked,
against a fixed fake desktop, so the result doesn't depend on what is open on the PC right now.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain       # noqa: E402
import resolver    # noqa: E402

# ── a fixed desktop: these apps are installed, these windows are open ──
APPS = [("terminal", "terminal", "Terminal", "kitty"), ("firefox", "firefox", "Firefox", "firefox"),
        ("chrome", "chrome", "Chrome", "google-chrome-stable"), ("file manager", "file manager", "File manager", "thunar"),
        ("whatsapp", "whatsapp", "WhatsApp", "whatsie"), ("mousepad", "mousepad", "Mousepad", "mousepad"),
        ("notepad", "notepad", "Notepad", "mousepad"), ("telegram", "telegram", "Telegram", "telegram-desktop"),
        ("discord", "discord", "Discord", "discord"), ("spotify", "spotify", "Spotify", "spotify")]
SITES = [(s, s, s.title(), f"https://{s}.com") for s in
         ("youtube", "instagram", "tiktok", "github", "gmail", "facebook", "claude", "whatsapp web")]
WINDOWS = [("firefox", "firefox", "Firefox", "firefox"), ("discord", "discord", "Discord", "discord"),
           ("kitty", "kitty", "Terminal", "kitty")]


def setup_module(module=None):
    resolver._static_candidates = lambda: (APPS, SITES)
    resolver._window_candidates = lambda: WINDOWS
    brain._is_open = lambda m: m.kind in ("window", "site") or m.target in {"firefox", "discord", "kitty"}
    brain._active_title = lambda: "tiktok - make your day — mozilla firefox"
    running = {"firefox", "discord", "kitty", "chrome"}            # processes "running" on the fake desktop
    brain.sc._matching_processes = lambda name: [name] if name.replace(" ", "") in running else []


def route(text):
    action = brain._route(text)
    if action and action[0] == "_ask":
        return ("_ask", [label for label, _, _ in action[1]["options"]])
    return action


# (what was heard, the expected decision). None = not a fast command (an AI brain decides).
CASES = [
    # media
    ("Pause music", ("media_control", {"action": "pause"})),
    ("Hidupkan musik", ("media_control", {"action": "play"})),
    ("musik", ("media_control", {"action": "play"})),
    ("lagu berikutnya", ("media_control", {"action": "next"})),
    ("putar musik Do I Wanna Know", ("play_youtube", {"query": "musik do i wanna know", "mix": False})),
    # volume / brightness
    ("naikkan volume 10% lagi", ("volume_up", {"amount": 10})),
    ("turunkan volume 5 persen", ("volume_down", {"amount": 5})),
    ("naikkan volume ke 80", ("set_volume", {"level": 80})),
    ("volume 50", ("set_volume", {"level": 50})),
    ("kecerahan ke 10%", ("set_brightness", {"level": 10})),
    # opening: exact, short forms, several, cut off, in a given browser
    ("IG", ("open_website", {"site": "instagram"})),
    ("tik", ("open_website", {"site": "tiktok"})),             # one cut-off word: only a near-certain name
    ("dingin", None),                                          # ...never a merely similar one
    ("halo", None),
    ("Tik Tok dan Instagram", ("open_many", {"names": ["tiktok", "instagram"]})),
    ("buka YouTube dan Tik Tok", ("open_many", {"names": ["youtube", "tiktok"]})),
    ("buka termi", ("open_application", {"name": "terminal"})),
    ("butuh terminal", ("open_application", {"name": "terminal"})),
    ("buka github di Firefox", ("open_website", {"site": "github", "browser": "firefox"})),
    # switching: with and without a verb, misheard workspace numbers
    ("ke discord", ("focus_window", {"name": "discord"})),
    ("pindah ke disco", ("focus_window", {"name": "discord"})),
    ("ke dokter besok", None),
    ("pindah", ("switch_workspace", {"workspace": "next"})),
    ("pindah keluar SP1", ("switch_workspace", {"workspace": "1"})),
    ("pindah ke workspace 3", ("switch_workspace", {"workspace": "3"})),
    ("pindah kurs PS3", ("switch_workspace", {"workspace": "3"})),   # "kurs" here is a mishearing, not exchange rates
    ("titik y dan enter", ("type_text", {"text": "y", "press_enter": True})),
    ("kembali ke halaman sebelumnya", ("navigate", {"action": "back"})),
    # closing: a site open in the browser is a tab; a misheard name is asked about; "file" is never Firefox
    ("tutup Instagram", ("close_tab", {"name": "instagram"})),
    ("tutup tito", ("_ask", ["Tiktok"])),
    ("tutup semua tik to", ("close_tab", {"name": "tiktok"})),
    ("untuk firefox", ("_ask", ["Firefox"])),                  # "tutup" misheard as "untuk": ask, don't guess
    ("untuk apa ya", None),
    ("tutup file", None),                      # File manager isn't open here: nothing to close (and not Firefox)
    ("tutup chrome", ("close_application", {"name": "chrome"})),
    ("tutup semua aplikasi", ("close_all_apps", {})),
    # searching: YouTube is in front -> YouTube; otherwise Google; "cari tahu" is a question
    ("cari video kucing lucu", ("search_youtube", {"query": "video kucing lucu"})),
    ("cari tahu tentang AI", None),
    ("cari file laporan", None),
    # questions answered without AI
    ("jam berapa sekarang", ("get_time", {})),
    ("cuaca hari ini", ("get_weather", {})),
    ("cuaca di Jakarta", ("get_weather", {"city": "jakarta"})),
    # the future and rain come from the forecast; the laptop's own heat isn't the weather
    ("cuaca besok hujan nggak", ("get_forecast", {"when": "besok"})),
    ("apakah besok hujan", ("get_forecast", {"when": "besok"})),
    ("nanti sore hujan nggak ya", ("get_forecast", {"when": "nanti"})),
    ("apakah hujan", ("get_forecast", {"when": "nanti"})),
    ("prakiraan cuaca lusa", ("get_forecast", {"when": "lusa"})),
    ("suhu laptop berapa", ("get_temperature", {})),
    ("laptop saya panas nggak", ("get_temperature", {})),
    ("berapa suhu cpu sekarang", ("get_temperature", {})),
    ("suhu di luar berapa", None),
    ("cek pesan masuk", ("read_notifications", {"messages_only": True, "app": ""})),
    ("ketik halo dunia", ("type_text", {"text": "halo dunia", "press_enter": False})),
    # smart lamps
    ("nyalakan lampu", ("lamp_power", {"on": True, "name": "lampu"})),
    ("matikan lampu kamar", ("lamp_power", {"on": False, "name": "lampu kamar"})),
    ("redupkan lampu 30 persen", ("lamp_brightness", {"percent": 30, "name": "lampu"})),
    ("redupkan lampu", ("lamp_brightness", {"percent": 30, "name": "lampu"})),
    ("lampu warna merah", ("lamp_color", {"color": "merah", "name": "lampu"})),
    ("ubah lampu jadi putih hangat", ("lamp_color", {"color": "putih hangat", "name": "lampu"})),
    ("kecerahan ke 10%", ("set_brightness", {"level": 10})),             # the screen, not the lamp
    ("lampu mode santai", ("lamp_scene", {"scene": "santai", "name": "lampu"})),
    ("suasana film", ("lamp_scene", {"scene": "film"})),
    ("lampu lebih terang", ("lamp_brightness", {"percent": 30, "relative": True, "name": "lampu"})),
    ("redupkan lampu sedikit", ("lamp_brightness", {"percent": -30, "relative": True, "name": "lampu"})),
    ("lampunya kurang terang", ("lamp_brightness", {"percent": 30, "relative": True, "name": "lampunya"})),
    ("kapan matahari terbenam", ("sun_today", {})),
    ("mode santai", None),                                               # the protocol, not only the lamp
    # the day, and watches
    ("rangkuman hari ini", ("daily_recap", {})),
    ("hari ini aku ngapain aja", ("daily_recap", {})),
    ("berapa lama saya di depan laptop hari ini", ("screen_time", {})),
    ("apa saja yang sedang kamu pantau", ("list_watches", {})),
    ("batalkan pantauan hujan", ("cancel_watch", {"what": "hujan"})),
]


def test_routes():
    setup_module()
    failures = []
    for heard, expected in CASES:
        got = route(heard)
        if got != expected:
            failures.append(f"{heard!r}: dapat {got}, harusnya {expected}")
    assert not failures, "\n".join(failures)


def test_resolver_scores():
    setup_module()
    assert resolver.resolve("termi")[1][0].label == "Terminal"
    assert resolver.resolve("disco")[1][0].label == "Discord"
    assert resolver.resolve("tito")[0] == "ask"                       # 0.73: ask, don't act
    assert resolver.resolve("ho")[0] == "none"                        # too short to mean anything
    assert resolver.resolve("this")[0] == "none"


def test_read_offer():
    """"ya" reads the messages only right after "Mau saya bacakan?", and only once."""
    setup_module()
    import notifications
    notifications.clear_offer()
    assert route("ya") is None
    notifications.offer_reading()                          # the welcome back: all new messages
    assert route("ya bacakan") == ("read_notifications", {"messages_only": True})
    assert route("ya") is None
    notifications.offer_reading("Rina")                    # "ada pesan WhatsApp dari Rina": only hers
    assert route("iya") == ("read_notifications", {"messages_only": True, "sender": "Rina"})


def test_watch_and_reply_parsing():
    """"kabari saya kalau …" and "balas …" are understood before the AI (and the reply keeps its own wording)."""
    import watchers
    assert watchers.parse("kabari saya kalau unduhan selesai") == ("unduhan", "", "")
    assert watchers.parse("kabari kalau Rina membalas") == ("pesan", "rina", "")
    assert watchers.parse("tolong kabari aku kalau hujan sudah reda ya") == ("hujan_reda", "", "")
    assert watchers.parse("kabari kalau baterai di bawah 20 persen") == ("baterai", "", "<20")
    assert watchers.parse("kabari kalau situs siakad.unila.ac.id bisa dibuka") == ("situs", "siakad.unila.ac.id", "")
    assert watchers.parse("kabari saya kalau ada yang chat") == ("pesan", "", "")
    assert watchers.parse("kabari kalau rapat selesai") is None          # not something on this PC
    assert watchers.parse("ingatkan saya jam 5") is None                  # an alarm, not a watch
    assert brain._reply_route("Balas: Oke, aku otw ya!") == ("", "Oke, aku otw ya!")
    assert brain._reply_route("balas ke Rina bilang aku sudah sampai") == ("Rina", "aku sudah sampai")
    assert brain._reply_route("kirim WhatsApp ke Budi Santoso isinya nanti malam jadi") == ("Budi Santoso", "nanti malam jadi")
    assert brain._reply_route("balas email dari dosen") is None
    assert brain._reply_route("kirim wa ke rina oke saya otw") is None    # where the name ends is a guess: the AI asks


def test_misc():
    assert brain.FRAGMENT_RE.fullmatch(brain._normalize("dan tik"))
    assert not brain.FRAGMENT_RE.fullmatch(brain._normalize("terus buka youtube"))
    assert brain.gemini_reason("buka firefox lalu putar musik be my baby") == "beberapa perintah sekaligus"
    assert brain.WAKE_PREFIX_RE.sub("", "Digidaw, buka Firefox.").strip() == "buka Firefox."


if __name__ == "__main__":
    ok = True
    for test in (test_routes, test_resolver_scores, test_read_offer, test_watch_and_reply_parsing, test_misc):
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            ok = False
            print(f"✗ {test.__name__}\n{e}")
    print(f"{len(CASES)} kasus rute + resolver: {'semua lulus' if ok else 'ADA YANG GAGAL'}")
    sys.stdout.flush()                 # os._exit skips the flush: piped output would be lost
    os._exit(0 if ok else 1)
