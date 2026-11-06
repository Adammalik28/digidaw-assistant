"""What real use showed on 5-6 Nov: "besok" after midnight, an agent out of quota, a story that became a file, music words, its own words clicked on the screen, a filler in the way
of the answer, side talk answered. No sound, no network, nothing opened; the agenda and alarms stay on temp files."""
import datetime
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import agenda
import alarms
import antigravity_analysis
import brain
import config
import core
import generator
import jadwal_kuliah
import knowledge
import linux_desktop as ld
import media
import metrics
import pc_control
import project_builder
import screen_agent
import system_control
import tts
import weather
import window_manager_linux as wm

metrics.count = lambda *a, **k: None
metrics.timing = lambda *a, **k: None
core.media.duck = lambda: None
core.tts.prewarm = lambda: None
config.STREAM_REPLIES = False
config.WEEKLY_SCHEDULE_FILE = config.BASE_DIR / "tests" / "tidak-ada-jadwal.tsv"
agenda.STATE_FILE = Path(tempfile.mkdtemp()) / "agenda.json"
alarms.STATE_FILE = Path(tempfile.mkdtemp()) / "alarms.json"     # (never the real one: a timer of the user's was in it when this test first ran)
agenda._sync_google = lambda: None
TODAY = datetime.date.today()
DAY = datetime.timedelta(days=1)


def at_night(on: bool = True):
    agenda._hour = (lambda: 1) if on else (lambda: 10)
    agenda._clock = (lambda: "01:00") if on else (lambda: "10:00")    # (the same night whatever the real time is: this test once only passed before 09.00)


def test_besok_after_midnight():
    at_night()
    try:
        assert agenda.parse_date("besok")[0] == TODAY and agenda.parse_date("lusa")[0] == TODAY + DAY
        assert agenda.when_text(TODAY) == "besok" and agenda.when_text(TODAY + DAY) == "lusa"
        name = agenda.HARI[TODAY.weekday()]
        assert agenda.parse_date(name)[0] == TODAY                     # at 00:50 on a Friday, "jumat" is the morning ahead
        assert name.capitalize() in agenda.said_back(TODAY) and str(TODAY.day) in agenda.said_back(TODAY) and agenda.said_back(TODAY).startswith("besok")
        assert agenda._still_ahead(TODAY, "00:00", "besok") == TODAY + DAY     # "besok jam 2" said at 03:00 can't be an hour ago
        assert agenda._still_ahead(TODAY, "23:59", "besok") == TODAY
        assert agenda._still_ahead(TODAY, "00:00", "hari ini") == TODAY
        when = alarms._day_offset("besok", 23, 59)
        assert when.date() == TODAY and when.hour == 23
        assert alarms._day_offset("lusa", 23, 59).date() == TODAY + DAY
        assert jadwal_kuliah._day_said(TODAY, TODAY) == "Besok" and jadwal_kuliah._day_said(TODAY + DAY, TODAY) == "Lusa"
        assert name in system_control.get_date("besok").lower()
    finally:
        at_night(False)
    assert agenda.parse_date("besok")[0] == TODAY + DAY and agenda.parse_date("lusa")[0] == TODAY + 2 * DAY     # during the day, as always
    assert agenda.when_text(TODAY) == "hari ini" and agenda.when_text(TODAY + DAY) == "besok" and agenda.when_text(TODAY + 2 * DAY) == "lusa"
    assert agenda.parse_date(agenda.HARI[TODAY.weekday()])[0] == TODAY + 7 * DAY
    assert alarms._day_offset("besok", 23, 59).date() == TODAY + DAY
    assert jadwal_kuliah._day_said(TODAY, TODAY) == "Hari ini" and jadwal_kuliah._day_said(TODAY + DAY, TODAY) == "Besok"


def test_event_is_said_back_with_its_day():
    agenda._events.clear()
    at_night()
    try:
        said = agenda.handle("kuliah mengumpulkan besok jam 09 pagi")
        assert agenda._events and agenda._events[-1]["date"] == TODAY.isoformat(), agenda._events
        name = agenda.HARI[TODAY.weekday()].capitalize()
        assert said and name in said and f"{TODAY.day} " in said and "jam 09.00" in said, said
        assert "malam sebelumnya" not in said, said                    # there is no evening left before the morning ahead
        assert "Ada 1 acara" in agenda._events_said(0)
        agenda._events.clear()
        assert name in agenda._events_said(0)                          # with nothing on it: the day's name, whichever word was asked with
    finally:
        at_night(False)
        agenda._events.clear()
    said = agenda.add_event("rapat", (TODAY + 3 * DAY).isoformat())
    assert agenda.HARI[(TODAY + 3 * DAY).weekday()].capitalize() in said and "3 hari lagi" in said, said
    agenda._events.clear()


def test_forecast_after_midnight():
    data = {"daily": {"time": [(TODAY + i * DAY).isoformat() for i in range(3)], "precipitation_probability_max": [10, 10, 10],
                      "weather_code": [0, 0, 0], "temperature_2m_min": [24, 24, 24], "temperature_2m_max": [32, 32, 32]}}
    real = weather.place, weather.forecast, weather._hours
    weather.place, weather.forecast, weather._hours = (lambda city: {"name": "Bandung"}), (lambda where: data), (lambda d: [])
    try:
        at_night()
        assert weather.get_forecast("besok").startswith(f"Besok, {weather.HARI[TODAY.weekday()]}")
        assert weather.get_forecast("lusa").startswith(f"Lusa, {weather.HARI[(TODAY + DAY).weekday()]}")
        at_night(False)
        assert weather.get_forecast("besok").startswith(f"Besok, {weather.HARI[(TODAY + DAY).weekday()]}")
    finally:
        at_night(False)
        weather.place, weather.forecast, weather._hours = real


def test_several_days_deleted():
    agenda._events.clear()
    for offset, title in ((3, "satu"), (4, "dua"), (5, "makan dan minum")):
        agenda._events.append({"id": title, "title": title, "date": (TODAY + offset * DAY).isoformat(), "time": "", "yearly": False, "remind_days": 1, "created": 0})
    a, b = (TODAY + 3 * DAY).day, (TODAY + 4 * DAY).day
    said = agenda.delete_event(f"tanggal {a} dan {b}")
    assert "Satu" in said and "Dua" in said and [e["title"] for e in agenda._events] == ["makan dan minum"], said
    assert "dihapus" in agenda.delete_event("makan dan minum") and not agenda._events      # a title with "dan" in it stays one title
    assert agenda._several_days("besok dan lusa") == ["besok", "lusa"] and agenda._several_days("rapat") == []


def test_agent_out_of_quota():
    error = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 63h48m40s."
    try:
        said = project_builder.agent_trouble(error)
        assert said.startswith("Kuota Antigravity habis") and "2 hari 15 jam" in said, said
        assert project_builder.agent_trouble("Permission denied") == "Permission denied"
        job = {"steps": 0, "files": set()}
        project_builder._handle_event({"event": "result", "result": {"status": "ERROR", "response": "", "error": error}}, job)
        assert job["agent_error"].startswith("Kuota Antigravity")
        started = []
        real = generator.threading.Thread
        generator.threading.Thread = lambda *a, **k: started.append(a) or real(target=lambda: None)
        try:
            assert "kuota Antigravity habis" in generator._submit("file", "buat file", "x") and not started
        finally:
            generator.threading.Thread = real
        real_gather = antigravity_analysis._gather_baseline
        antigravity_analysis._gather_baseline = lambda: {"cpu_percent": 9, "ram_used_gb": 6, "ram_total_gb": 15, "disk_percent": 30}
        try:
            answer = antigravity_analysis.analisis_sistem()
            assert "Kuota Antigravity habis" in answer and "CPU 9 persen" in answer and "selesai" not in answer, answer
        finally:
            antigravity_analysis._gather_baseline = real_gather
    finally:
        project_builder._quota["until"] = 0.0
    assert project_builder.quota_left() == ""


def test_failed_analysis_opens_nothing():
    said, opened = [], []
    real = antigravity_analysis.ANNOUNCE, antigravity_analysis.webbrowser.open
    antigravity_analysis.ANNOUNCE = lambda text, level: said.append((text, level))
    antigravity_analysis.webbrowser.open = lambda url: opened.append(url)
    try:
        antigravity_analysis._tell_without_agent({"cpu_percent": 9, "ram_used_gb": 6, "ram_total_gb": 15, "disk_percent": 30}, "Kuota Antigravity habis")
    finally:
        antigravity_analysis.ANNOUNCE, antigravity_analysis.webbrowser.open = real
    assert said and said[0][1] == "warning" and "belum bisa" in said[0][0] and "selesai" not in said[0][0] and not opened


def test_stories_are_told():
    for told in ("coba buat cerita", "berikan saya cerita", "berikan saya pantun", "buatkan puisi tentang hujan", "kasih aku lelucon dong", "ceritakan dongeng kancil",
                 "bikin pantun lucu", "buat ucapan ulang tahun"):
        assert knowledge.wants_it_told(told), told
    for not_told in ("buat file cerita", "buatkan surat lamaran", "buat presentasi tentang puisi", "ceritakan sejarah indonesia", "kasih lagu dong", "buat kata sandi",
                     "buat catatan belanja", "buka cerita"):
        assert not knowledge.wants_it_told(not_told), not_told
    seen = {}

    def agent(text):
        seen.update(only=brain._only_words, tools_off=brain._generation_config("gemini-3.1-flash-lite").tool_config, notes=list(brain._system_notes))
        return "Pada suatu hari."
    real = brain.ask_agent, brain.ask_local
    brain.ask_agent, brain.ask_local = agent, lambda *a, **k: "bukan ini"
    brain._system_notes.clear()
    try:
        assert brain._think("coba buat cerita") == "Pada suatu hari."
        assert seen["only"] and seen["tools_off"].function_calling_config.mode.value == "NONE" and "diucapkan" in seen["notes"][-1]
        assert not brain._only_words and brain._generation_config("gemini-3.1-flash-lite").tool_config is None    # the next command has its skills back
        said_locally = []
        brain.ask_agent = lambda text: None
        real_talk = brain._local_talk
        brain._local_talk = lambda text, offline: (said_locally.append(offline), "Cerita lokal")[1]
        try:
            assert brain._think("coba buat cerita") == "Cerita lokal" and said_locally == [True]   # no online AI: the local model tells it
        finally:
            brain._local_talk = real_talk
    finally:
        brain.ask_agent, brain.ask_local = real
        brain._system_notes.clear()


def _route(sentence):
    return brain._route(brain._normalize(sentence))


def test_music_words():
    for sentence in ("ganti music", "ganti musik", "ganti lagu", "pindah lagu", "musik lain", "lagu lain", "musik yang lain", "lagu berikutnya", "musik selanjutnya", "next"):
        assert _route(sentence) == ("media_control", {"action": "next"}), (sentence, _route(sentence))
    for sentence in ("musik sebelumnya", "lagu sebelumnya", "putar musik yang sebelumnya", "putar yang tadi", "lagu yang tadi", "putar lagu tadi", "kembali ke lagu sebelumnya"):
        assert _route(sentence) == ("media_control", {"action": "previous"}), (sentence, _route(sentence))
    assert _route("putar lagu coldplay")[0] == "play_youtube"
    assert _route("putar musik")[1] == {"action": "play"} and _route("lanjutkan musik")[1] == {"action": "play"}


def test_previous_song():
    calls = []
    real = media._yt_status, media._yt_js, media.play_youtube, list(media._played)
    media.play_youtube = lambda query, mix=False: f"Memutar {query}"
    media._yt_js = lambda script, await_promise=False: calls.append(script) or True
    try:
        media._yt_status = lambda: {"paused": True}
        assert "Melanjutkan" in media.media_control("previous")          # paused: that one
        calls.clear()
        media._yt_status = lambda: {"paused": False}
        assert "sebelumnya" in media.media_control("previous") and calls == ["history.back()"]
        media._yt_status = lambda: None
        media._played[:] = ["arctic monkeys 505"]
        assert media.media_control("previous") == "Memutar arctic monkeys 505"      # the player was closed: what it was playing
    finally:
        media._yt_status, media._yt_js, media.play_youtube = real[:3]
        media._played[:] = real[3]


def test_heaviest_apps_named():
    for sentence in ("apa pemakaian ram terbesar", "pemakaian ram paling besar", "penggunaan memori terbanyak", "apa yang paling makan ram", "siapa yang makan cpu", "aplikasi apa yang paling berat"):
        assert _route(sentence) == ("heavy_apps", {}), (sentence, _route(sentence))
    assert _route("berapa baterai sekarang") != ("heavy_apps", {})


def test_own_windows_painted_out():
    from PIL import Image
    monitor = {"name": "X", "x": 100, "y": 0, "width": 2000, "height": 1000, "scale": 2.0, "activeWorkspace": {"id": 1}}
    hud = {"title": "DIGIDAW HUD", "pid": 1, "at": [200, 50], "size": [200, 100], "workspace": {"id": 1}, "mapped": True}
    other = {"title": "Firefox", "pid": 2, "at": [500, 50], "size": [200, 100], "workspace": {"id": 1}, "mapped": True, "fullscreen": 0}
    real = ld._hypr_json

    def picture(*clients):
        ld._hypr_json = lambda what: list(clients)
        return pc_control._paint_out_digidaw(Image.new("RGB", (2000, 1000), (255, 255, 255)), monitor)
    try:
        painted = picture(hud, other)
        assert painted.getpixel((400, 200)) == (24, 24, 24)            # HUD: layout x 200 is 100 from the monitor's edge, 200 in pixels at scale 2; y 50 is 100
        assert painted.getpixel((1000, 200)) == (255, 255, 255)        # Firefox untouched
        assert picture({**hud, "workspace": {"id": 2}}, other).getpixel((400, 200)) == (255, 255, 255)   # on another workspace it isn't in the picture
        assert picture(hud, {**other, "fullscreen": 2}).getpixel((400, 200)) == (255, 255, 255)          # behind a fullscreen window: nothing of it shows
    finally:
        ld._hypr_json = real
    assert screen_agent.OWN_WINDOW_RE.search("klik tombol restart di HUD") and not screen_agent.OWN_WINDOW_RE.search("Klik baca komik.")


def test_filler_gives_way():
    engine = tts.tts_engine
    stopped, chunks = [], []
    real = engine.stop, engine._speak_chunks, engine.before_speak, engine.after_speak
    engine.stop, engine._speak_chunks = (lambda: stopped.append(True)), (lambda c: chunks.append(c))
    engine.before_speak = engine.after_speak = None
    try:
        engine._saying_filler = True
        engine.speak("Ini jawabannya.")
        assert stopped and chunks and engine._saying_filler is False
        stopped.clear()
        engine.speak("Berikutnya.")
        assert not stopped                                      # an answer doesn't cut off another answer
    finally:
        engine.stop, engine._speak_chunks, engine.before_speak, engine.after_speak = real
    said = []
    real_filler, real_speaking = core.tts.speak_filler, core.tts.is_speaking
    core.tts.speak_filler, core.tts.is_speaking = (lambda text: said.append(text)), (lambda: False)
    try:
        c = core.DigidawCore(voice_enabled=False)
        c.busy.set()
        live = tts.LiveSpeech()
        live.raw = "Presiden"
        c._live_answer = live
        c._say_filler()
        assert not said                                         # it is being written: heard in a moment
        c._live_answer = None
        c._say_filler()
        assert said
    finally:
        core.tts.speak_filler, core.tts.is_speaking = real_filler, real_speaking


def test_move_uses_the_name_as_heard():
    from collections import namedtuple
    Match = namedtuple("Match", "key")
    real = wm._find
    wm._find = lambda name: {"title": "Discord"} if name == "discord" else None
    import resolver
    real_resolve = resolver.resolve
    resolver.resolve = lambda heard, kinds, keep=None, sure_only=False: ("sure", [Match("discord")]) if heard == "diskor" else ("none", [])
    try:
        assert wm._target_by_sound("diskor") == {"title": "Discord"}
        assert wm._target_by_sound("apa ini") is None and wm._target_by_sound("") is None
    finally:
        wm._find = real
        resolver.resolve = real_resolve


def test_closing_words():
    spoken = []
    real_speak, real_chime = core.tts.speak, core.chime
    core.tts.speak, core.chime = (lambda text: spoken.append(text)), (lambda up=True: None)
    try:
        for word, reply in (("terima kasih", "Sama-sama, Tuan."), ("makasih ya", "Sama-sama, Tuan."), ("cukup", "Baik, Tuan."), ("sudah", "Baik, Tuan.")):
            c = core.DigidawCore(voice_enabled=False)
            c.armed_until = time.time() + 5
            spoken.clear()
            c.on_heard(word, time.time(), time.time())
            assert spoken == [reply], (word, spoken)
    finally:
        core.tts.speak, core.chime = real_speak, real_chime


def test_side_talk_is_not_answered():
    spoken, chimes = [], []
    real_speak, real_chime = core.tts.speak, core.chime
    core.tts.speak, core.chime = (lambda text: spoken.append(text)), (lambda up=True: chimes.append(up))
    try:
        c = core.DigidawCore(voice_enabled=False)
        c.open_conversation = lambda seconds: None
        c._run_with_deadline = lambda text: ("Maaf, kurang jelas, Tuan. Maksudnya apa?", False)
        c._unasked = True
        c._handle("ya allah dikit lagi", "voice")
        assert not spoken and chimes == [False] and c._unasked is False
        c._handle("ya allah dikit lagi", "voice")               # called by name, or after the chime: it says it didn't understand
        assert spoken == ["Maaf, kurang jelas, Tuan. Maksudnya apa?"]
        spoken.clear()
        c._unasked = True
        c._run_with_deadline = lambda text: ("Lampu sekarang merah.", False)
        c._handle("lampu merah", "voice")
        assert spoken == ["Lampu sekarang merah."]              # understood: answered as always
    finally:
        core.tts.speak, core.chime = real_speak, real_chime


def test_pieces_end_where_one_breathes():
    pieces = tts.in_clauses("Presiden Indonesia dari yang pertama sampai ketujuh adalah Soekarno, Soeharto, B.J. Habibie, dan Joko Widodo.")
    assert not any(re.search(r"\b(?:dari|yang|dan|untuk)[,]?$", p) for p in pieces), pieces
    assert not pieces[0].endswith("dari") and pieces[0].startswith("Presiden Indonesia dari yang")
    long_one = "Aku tidak tahu status Discord dan Telegram dari hasil sistem nomor 12 karena datanya belum tersedia di sini dan belum pernah dicatat"
    pieces = tts.in_clauses(long_one)
    assert " ".join(pieces) == long_one and all(len(p) <= tts.FIRST_PIECE_CHARS + 5 for p in pieces), pieces


# ───────────────────────────── what the log of 6 Nov showed ─────────────────────────────

def _barge_core(guesses, second):
    """A core that hears these readings over its own voice, with this second opinion. Returns (stopped?, what it did)."""
    import stt
    c = core.DigidawCore(voice_enabled=False)
    stt.stt_engine.guesses = lambda audio: guesses
    c._second_opinion = staticmethod(lambda heard: second)
    stops = []
    tts.stop, kept = (lambda: stops.append(1)), tts.stop
    chime, core.chime = core.chime, lambda **k: None
    try:
        audio = (np.sin(np.arange(16000) * 0.1) * 6000).astype(np.int16).tobytes()
        return c._barge_phrase(audio, 10.0, 10.8, True), bool(stops)
    finally:
        tts.stop, core.chime = kept, chime


def test_a_short_sound_gets_a_second_opinion():
    tts.tts_engine.saying = "Selamat pagi Tuan hari ini tidak ada agenda"
    assert _barge_core([], "") == (False, False), "nobody speaks in it: it goes on talking"
    assert _barge_core([], "ada agenda") == (False, False), "its own words left by the filter"
    assert _barge_core([], "terima kasih") == (True, True), "someone said something short: that is stop, whatever the reading"
    assert _barge_core([], None) == (True, True), "no second opinion to be had: as before"


def test_the_second_opinion_without_a_prompt():
    import local_stt
    seen = {}

    class Segment:
        def __init__(self, text, no_speech):
            self.text, self.no_speech_prob = text, no_speech

    class Model:
        def transcribe(self, samples, **options):
            seen.update(options)
            return iter(answer), None

    class Audio:
        def get_raw_data(self, **k):
            return b"\0\0" * 8000

    kept = local_stt.available, local_stt._model
    local_stt.available, local_stt._model = (lambda size=None: True), (lambda size: Model())
    try:
        answer = [Segment(" cukup", 0.2)]
        assert local_stt.speech_in(Audio()) == "cukup"
        assert "initial_prompt" not in seen, "with the prompt it hears the prompt in a cough"
        answer = [Segment(" oh", 0.9)]
        assert local_stt.speech_in(Audio()) == ""
        local_stt.available = lambda size=None: False
        assert local_stt.speech_in(Audio()) is None
    finally:
        local_stt.available, local_stt._model = kept


def test_stopping_is_not_a_broken_voice():
    """Every "cukup" made the next answers speak with the robotic voice for 30 s: the sentence being fetched came back empty, and that counted against Edge."""
    engine = tts.tts_engine
    kept = engine._race_stream, engine.link.stream, engine.edge_down_until, metrics.count
    counted = []
    metrics.count = lambda name, n=1: counted.append(name)

    async def nothing(text, on_chunk, stoppable=True):
        return True

    def broken(*a, **k):
        raise ConnectionError("koneksi lama putus")

    engine._race_stream, engine.link.stream = nothing, broken
    engine.edge_down_until = 0.0
    engine._stop.set()
    try:
        path, cached = engine._synthesize("Cuaca hari ini mendung sekali di Bandung.", ["edge", "piper"])
        assert path is None and not cached
        assert engine.edge_ok(), "Edge wasn't down: the speech was stopped"
        assert "suara_cadangan" not in counted
        engine._stop.clear()
        try:
            engine._synth_edge("Ya.", Path(tempfile.gettempdir()) / "ddw_tes.mp3")
            raise AssertionError("no audio without a stop is still a failure")
        except InterruptedError:
            raise AssertionError("no audio without a stop is still a failure")
        except ConnectionError:
            pass
    finally:
        engine._stop.clear()
        engine._race_stream, engine.link.stream, engine.edge_down_until, metrics.count = kept


def test_quiet_hours_cross_midnight():
    assert core._quiet_hours(23) and core._quiet_hours(2) and core._quiet_hours(5)
    assert not core._quiet_hours(6) and not core._quiet_hours(12) and not core._quiet_hours(22)


def _welcome(hour, seconds=2 * 3600, messages="", held=()):
    import notifications
    import presence
    c = core.DigidawCore(voice_enabled=False)
    said = []
    c.announce = lambda text, level="info", **k: said.append(text) or True
    c._quiet = lambda: True
    kept = core._quiet_hours, presence.busy_reason, presence.take_held, notifications.unread_summary, config.STARTUP_BRIEFING
    left = list(held)

    def take():
        out, left[:] = left[:], []
        return out

    core._quiet_hours = lambda h=None: hour >= 23 or hour < 6
    presence.busy_reason = lambda: ""
    presence.take_held = take
    notifications.unread_summary = lambda since=0: messages
    config.STARTUP_BRIEFING = False
    try:
        c.welcome_back(seconds, "input", False, time.time() - seconds)
    finally:
        core._quiet_hours, presence.busy_reason, presence.take_held, notifications.unread_summary, config.STARTUP_BRIEFING = kept
    return said, left


def test_no_welcome_in_the_small_hours():
    assert _welcome(5) == ([], []), "a hand on the laptop at 05.42: nothing to say, nothing said"
    said, kept_news = _welcome(2, held=["Baterai sudah terisi penuh, Tuan."])
    assert not said and kept_news, "small news waits for the morning"
    said, _ = _welcome(3, messages="ada pesan dari Ayu")
    assert said and "pesan dari Ayu" in said[0], "a message is still told"
    assert _welcome(9)[0], "by day it greets as before"
    said, _ = _welcome(9, held=["Baterai sudah terisi penuh, Tuan."])
    assert "Baterai" in said[0]


def test_an_alarm_leaves_its_life_in_the_log():
    printed, kept = [], (alarms.print if hasattr(alarms, "print") else None)
    alarms.print = lambda *a, **k: printed.append(" ".join(map(str, a)))
    keep_items, keep_ring, keep_say = list(alarms._items), alarms._ring, alarms._say
    alarms._items.clear()
    try:
        item = alarms.add("alarm", "kuliah", datetime.datetime.now() + datetime.timedelta(hours=3))
        assert "dipasang" in printed[-1] and "kuliah" in printed[-1], printed
        alarms.cancel_id(item["id"], "HUD HP")
        assert "dibatalkan lewat HUD HP" in printed[-1], printed
        assert not alarms._items

        # whatever breaks on the way to ringing is said, in the log and out loud
        said = []
        alarms._say = lambda text, level="reminder": said.append((text, level))

        def broken(i, text):
            alarms._ringing = i
            raise OSError("speaker hilang")

        alarms._ring = broken
        alarms._fire({"id": "x", "kind": "alarm", "label": "", "time": "05:00", "due": time.time()})
        assert any("GAGAL berbunyi" in p for p in printed), printed
        assert said and said[0][1] == "warning" and "05.00" in said[0][0]
        assert not alarms.is_ringing(), "a broken ring doesn't stay ringing for ever"
    finally:
        alarms._items[:] = keep_items
        alarms._ring, alarms._say = keep_ring, keep_say
        if kept is None:
            del alarms.print
        else:
            alarms.print = kept
        alarms._changed()


def test_moving_windows_by_what_the_user_calls_them():
    hud = {"hwnd": "0x1", "title": "DIGIDAW HUD", "pid": 1, "process": "chrome", "class": "chrome", "workspace": "2"}
    music = {"hwnd": "0x2", "title": "Arctic Monkeys - 505 - YouTube", "pid": 2, "process": "chrome", "class": "chrome", "workspace": "2"}
    chat = {"hwnd": "0x3", "title": "(3) WhatsApp", "pid": 3, "process": "whatsie", "class": "whatsie", "workspace": "1"}
    moved = []
    kept = wm._windows, wm._is_digidaw, ld.move_to_workspace, media._player_target
    wm._windows = lambda: [hud, music, chat]
    wm._is_digidaw = lambda w: w is hud
    ld.move_to_workspace = lambda hwnd, target, follow=False: moved.append((hwnd, target)) or True
    media._player_target = lambda: {"title": "Arctic Monkeys - 505"}
    try:
        assert "Digidaw" in wm.move_to_workspace("1", "sistem") and moved[-1] == ("0x1", "1")
        assert moved[-1] == ("0x1", "1")
        wm.move_to_workspace("3", "musik")
        assert moved[-1] == ("0x2", "3")
        said = wm.move_to_workspace("4", "whatsapp")
        assert "WhatsApp" in said and "Whatsie" not in said, said
        said = wm.move_to_workspace("1", "whatsapp")
        assert "sudah ada" in said and "WhatsApp" in said and "(3)" not in said, said
    finally:
        wm._windows, wm._is_digidaw, ld.move_to_workspace, media._player_target = kept


def test_routes_of_this_round():
    route = brain._route
    assert route("pindah ke Terminal ke WordPress 3") == ("move_to_workspace", {"workspace": "3", "follow": False, "name": "terminal"})
    assert route("pindah Terminal ke workspace 2")[0] == "move_to_workspace"
    assert route("pindah ke workspace 3")[0] == "switch_workspace", "just going there is still going there"
    assert route("pindah ke firefox")[0] != "move_to_workspace"
    for said in ("berapa baterai sekarang", "cek baterai", "baterai berapa", "sisa batre", "baterai laptop berapa persen"):
        assert route(said) == ("get_battery", {}), said
    assert route("status sistem") == ("get_system_status", {})
    assert route("Berhitung 1-50") == ("count_aloud", {"start": 1, "end": 50})
    assert route("hitung 2 sampai 5") == ("count_aloud", {"start": 2, "end": 5})
    assert route("berhitung satu sampai sepuluh") == ("count_aloud", {"start": 1, "end": 10})
    assert route("hitung mundur dari 10") == ("count_aloud", {"start": 10, "end": 1})
    assert route("hitung 5-3") == ("calculate", {"sum_said": "5-3"}), "a sum stays a sum"
    assert route("dong buka terminal") == route("buka terminal")
    assert route("latih panggilan") == ("train_my_call", {})


def test_counting_out_loud():
    import everyday
    assert everyday.count_aloud(1, 5) == "1, 2, 3, 4, 5."
    assert everyday.count_aloud(3, 1) == "3, 2, 1."
    assert "terlalu panjang" in everyday.count_aloud(1, 5000)


def test_battery_alone():
    import psutil

    class Battery:
        percent, power_plugged, secsleft = 62.4, False, 5400

    kept = psutil.sensors_battery
    psutil.sensors_battery = lambda: Battery
    try:
        said = system_control.get_battery()
        assert "62 persen" in said and "memakai baterai" in said and "1 jam 30 menit" in said and "CPU" not in said, said
        Battery.power_plugged = True
        assert "sedang dicas" in system_control.get_battery()
        psutil.sensors_battery = lambda: None
        assert "tidak memberi tahu" in system_control.get_battery()
    finally:
        psutil.sensors_battery = kept


def test_closing_answers_before_the_app_is_gone():
    import threading
    release = threading.Event()
    calls = []

    def slow(procs):
        calls.append(1)
        release.wait(5)
        return [], []

    kept = system_control._quit_processes, system_control.ANNOUNCE
    system_control._quit_processes = slow
    try:
        started = time.time()
        assert system_control._quit_soon(["app"], "Firefox", "firefox") == ([], [])
        assert time.time() - started < 1.5, "an answer doesn't wait for the app to be gone"
        thread = system_control._closing["firefox"]
        assert thread.is_alive(), "it goes on behind the answer"

        # what really won't stop is said afterwards
        said = []
        system_control.ANNOUNCE = lambda text, level="info": said.append(text)
        release.set()
        thread.join(3)
        assert not said
        hard = threading.Event()

        def stubborn(procs):
            hard.wait(5)
            return ["still here"], []

        system_control._quit_processes = stubborn
        system_control._quit_soon(["app"], "Firefox", "firefox")
        hard.set()
        system_control._closing["firefox"].join(3)
        assert said and "tidak mau berhenti" in said[0], said

        # quick ones answer with the real result
        system_control._quit_processes = lambda procs: (["x"], [])
        assert system_control._quit_soon(["app"], "Firefox") == (["x"], [])
    finally:
        release.set()
        hard.set()
        system_control._quit_processes, system_control.ANNOUNCE = kept


def test_a_routine_says_it_started():
    import protocols
    said = []
    kept = protocols.load, protocols._run_step, protocols._quietly, tts.speak_filler, brain.speaking_aloud
    brain.speaking_aloud = lambda: True
    protocols.load = lambda: {"gabut": {"steps": [{"say": "a"}, {"say": "b"}, {"say": "c"}]}, "kecil": {"steps": [{"say": "a"}]}}
    protocols._run_step = lambda step: "ok"
    import contextlib
    protocols._quietly = contextlib.nullcontext
    tts.speak_filler = lambda text: said.append(text)
    try:
        assert "gabut" in protocols.run_protocol("gabut")
        for _ in range(100):
            if said:
                break
            time.sleep(0.01)
        assert said == ["Menjalankan rutinitas gabut."], said
        said.clear()
        protocols.run_protocol("kecil")
        time.sleep(0.1)
        assert not said, "one step is done before it could be announced"
        brain.speaking_aloud = lambda: False
        protocols.run_protocol("gabut")
        time.sleep(0.1)
        assert not said, "asked from the phone, set to answer there: the PC stays quiet"
    finally:
        protocols.load, protocols._run_step, protocols._quietly, tts.speak_filler, brain.speaking_aloud = kept


def test_only_one_sentence_for_waiting():
    c = core.DigidawCore(voice_enabled=False)
    said = []
    kept = tts.speak_filler, tts.is_speaking, tts.filler_said_within
    tts.speak_filler, tts.is_speaking = (lambda text: said.append(text)), (lambda: False)
    c.busy.set()
    try:
        tts.filler_said_within = lambda seconds: True
        c._say_filler()
        assert not said, "\"Menjalankan rutinitas\" was just said: no \"Sebentar\" on top of it"
        tts.filler_said_within = lambda seconds: False
        c._say_filler()
        assert said
    finally:
        c.busy.clear()
        tts.speak_filler, tts.is_speaking, tts.filler_said_within = kept


def test_lagi_dong_alone_is_the_call():
    c = core.DigidawCore(voice_enabled=False)
    armed = []
    c.arm = lambda *a, **k: armed.append(1)
    c.submit = lambda *a, **k: (_ for _ in ()).throw(AssertionError("it isn't a command"))
    now = time.time()
    c.followup_until = now + 8
    c.on_heard("lagi dong", now - 1, now - 0.5)
    assert armed, "Google's way of writing the name, said in the follow-up window"


if __name__ == "__main__":
    ok = True
    for test in (test_besok_after_midnight, test_event_is_said_back_with_its_day, test_forecast_after_midnight, test_several_days_deleted, test_agent_out_of_quota,
                 test_failed_analysis_opens_nothing, test_stories_are_told, test_music_words, test_previous_song, test_heaviest_apps_named, test_own_windows_painted_out,
                 test_filler_gives_way, test_move_uses_the_name_as_heard, test_closing_words, test_side_talk_is_not_answered, test_pieces_end_where_one_breathes, test_a_short_sound_gets_a_second_opinion, test_the_second_opinion_without_a_prompt, test_stopping_is_not_a_broken_voice, test_quiet_hours_cross_midnight, test_no_welcome_in_the_small_hours, test_an_alarm_leaves_its_life_in_the_log, test_moving_windows_by_what_the_user_calls_them, test_routes_of_this_round, test_counting_out_loud, test_battery_alone, test_closing_answers_before_the_app_is_gone, test_a_routine_says_it_started, test_only_one_sentence_for_waiting, test_lagi_dong_alone_is_the_call):
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as e:
            ok = False
            import traceback
            print(f"✗ {test.__name__}: {e!r}")
            traceback.print_exc()
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
