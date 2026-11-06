"""Interrupting Digidaw by voice (aec.py), and what the 5 Nov round fixed around it: answers to its questions, speech that starts sooner, a runner that gives memory back.
No sound, no microphone, no network, nothing opened."""
import os
import sys
import time

import numpy as np

import aec
import brain
import config
import core
import habits
import local_llm
import metrics
import system_control
import tts

metrics.count = lambda *a, **k: None
metrics.timing = lambda *a, **k: None
core.chime = lambda up=True: None
core.media.duck = lambda: None
core.tts.prewarm = lambda: None
config.STREAM_REPLIES = False
RATE = aec.RATE


def speech_like(seconds: float, seed: int = 1) -> np.ndarray:
    """Noise shaped like talking: bursts with gaps between them."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    sound = rng.normal(0, 1, n)
    sound = np.convolve(sound, np.ones(6) / 6, mode="same")
    envelope = np.abs(np.sin(np.arange(n) / RATE * 2 * np.pi * 2.3)) ** 2
    return sound * envelope * 6000


def test_canceller_learns_the_echo():
    speaker = speech_like(12)
    path = np.zeros(1600)
    path[800], path[950], path[1300] = 1.8, -0.7, 0.25     # 50 ms later, louder than what the speakers were given, with a little of the room
    mic = np.convolve(speaker, path)[:len(speaker)] + np.random.default_rng(2).normal(0, 50, len(speaker))
    canceller = aec.EchoCanceller()
    left = np.zeros(len(mic))
    for i in range(0, len(mic) - aec.BLOCK, aec.BLOCK):
        left[i:i + aec.BLOCK], _ = canceller.process(speaker[i:i + aec.BLOCK], mic[i:i + aec.BLOCK])
    late = len(mic) // 2
    gained = 10 * np.log10(np.mean(mic[late:] ** 2) / np.mean(left[late:] ** 2))
    assert gained > 15, gained
    assert canceller.learned_seconds() >= aec.LEARNED_SECONDS - 1
    before = canceller.learned_blocks
    canceller.process(np.zeros(aec.BLOCK), np.zeros(aec.BLOCK))
    assert canceller.learned_blocks == before              # nothing playing: nothing to learn from


def _frame(level: float, tone_hz: float = 400.0) -> bytes:
    t = np.arange(480) / RATE
    return (np.sin(2 * np.pi * tone_hz * t) * level * 1.414).astype(np.int16).tobytes()


def _feed(listener, frames, start=100.0):
    """(level, echo predicted) per frame. Returns the phrases that came out."""
    out = []
    for i, (level, predicted, *tone) in enumerate(frames):
        phrase = listener.feed(_frame(level, *tone), level, predicted, start + i * 0.03)
        if phrase:
            out.append(phrase)
    return out


def test_listener_tells_a_voice_from_the_echo():
    held = []
    listener = aec.Listener(lambda: held.append(True))
    listener.note_room(300)
    assert not _feed(listener, [(3000, 13000)] * 40) and not held     # what the filter leaves of a loud echo
    assert not _feed(listener, [(4000, 13000)] * 3) and not held      # over the echo share, not by much: the false alarms measured stayed under 1.5
    assert not listener.recording()
    # a word in a gap of the voice
    _feed(listener, [(100, 200)] * 8)
    phrases = _feed(listener, [(6000, 200)] * 15 + [(100, 0)] * 15)
    assert held and len(phrases) == 1
    audio, started, ended, clear = phrases[0]
    assert audio and clear and ended > started


def test_listener_limits_false_alarms():
    held = []
    listener = aec.Listener(lambda: held.append(True))
    _feed(listener, [(100, 0)] * 8)
    _feed(listener, [(6000, 0)] * 6 + [(100, 0)] * 14)
    assert len(held) == 1
    listener.judged(False)
    assert listener.not_before > time.time()               # not held again at once
    listener.not_before = 0.0
    now = time.time()
    _feed(listener, [(6000, 0)] * 6 + [(100, 0)] * 14, start=now)
    assert len(held) == 2
    listener.judged(False)
    listener.not_before = 0.0
    _feed(listener, [(6000, 0)] * 6 + [(100, 0)] * 14, start=now + 5)
    assert len(held) == 2                                  # a TV: after two the answer just finishes
    listener.new_answer()
    _feed(listener, [(6000, 0)] * 6 + [(100, 0)] * 14, start=now + 9)
    assert len(held) == 3


def test_listener_room_and_beeps():
    held = []
    listener = aec.Listener(lambda: held.append(True))
    for _ in range(50):
        listener.note_room(1500)
    assert not _feed(listener, [(3000, 0)] * 10) and not held         # a loud room isn't a voice
    phrases = _feed(listener, [(100, 0)] * 8 + [(6000, 0, 2460)] * 15 + [(100, 0)] * 20)
    assert held and phrases and phrases[0][3] is False                # a beep holds the speech, but it doesn't count as a voice
    assert aec.voice_share(_frame(5000, 2460)) < aec.VOICE_SHARE <= aec.voice_share(_frame(5000, 400))


def test_frame_level_is_rms_without_the_rumble():
    t = np.arange(aec.FRAME_SAMPLES) / RATE
    assert abs(aec.frame_level(np.sin(2 * np.pi * 1000 * t) * 3000) - 2121) < 60
    assert abs(aec.frame_level(np.sin(2 * np.pi * 400 * t) * 6000) - 4243) < 120
    assert aec.frame_level(np.sin(2 * np.pi * 50 * t) * 6000) < 500      # rumble doesn't count


def test_speaker_gain_follows_the_system_volume():
    outputs = {"get-sink-volume": "Volume: front-left: 32768 /  50% / -18.06 dB", "get-sink-mute": "Mute: no"}

    class Done:
        def __init__(self, stdout):
            self.stdout = stdout

    real = aec.subprocess.run
    aec.subprocess.run = lambda args, **k: Done(outputs[args[1]])
    try:
        assert abs(aec._speaker_gain() - 0.125) < 0.001
        outputs["get-sink-mute"] = "Mute: yes"
        assert aec._speaker_gain() == 0.0
    finally:
        aec.subprocess.run = real


def test_filter_is_only_trusted_while_it_works():
    trust = aec.Trust()
    for _ in range(100):
        trust.note(6000, 6000, 600)            # what is left is as loud as the echo: learning, or the two streams are out of step
    assert not trust.good()
    for _ in range(200):
        trust.note(1500, 6000, 600)            # working: about a quarter is left
    assert trust.good()
    for _ in range(200):
        trust.note(8000, 6000, 600)            # the speakers were moved
    assert not trust.good()
    quiet = aec.Trust()
    for _ in range(200):
        quiet.note(900, 700, 600)              # at a low volume the echo is too small to pass for a voice
    assert quiet.good()


def _core_with(guesses):
    stopped, heard = [], []
    core.tts.stop = lambda: stopped.append(True)
    core.stt.stt_engine.guesses = lambda audio: list(guesses)
    c = core.DigidawCore(voice_enabled=False)
    c.on_heard = lambda text, started, ended: heard.append(text)
    c._second_opinion = lambda heard: None           # (no Whisper here: test_real_use has what it decides)
    return c, stopped, heard


def test_what_is_said_over_the_voice():
    real_stop, real_guesses, saying = core.tts.stop, core.stt.stt_engine.guesses, tts.tts_engine.saying
    tts.tts_engine.saying = "Besok gerimis, suhunya cukup panas di Bandung"
    audio = bytes(4000)
    try:
        for said in (["stop"], ["Stop, stop!"], ["sudah cukup"], ["berhenti"], ["Oke cukup"], ["stok"], ["apa", "stop dulu"]):
            c, stopped, heard = _core_with(said)
            assert c._barge_phrase(audio, 10.0, 11.0) is True and stopped and not heard, said
        c, stopped, heard = _core_with(["Digidaw, putar musik"])
        assert c._barge_phrase(audio, 10.0, 11.0) is True and stopped and heard == ["Digidaw, putar musik"]
        c, stopped, heard = _core_with(["Digidaw stop"])
        assert c._barge_phrase(audio, 10.0, 11.0) is True and stopped and not heard
        c, stopped, heard = _core_with(["Digidaw"])
        assert c._barge_phrase(audio, 10.0, 11.0) is True and stopped and heard == ["Digidaw"]      # stopped, and listening for the command
        c, stopped, heard = _core_with(["besok pagi aku ke kampus"])
        assert c._barge_phrase(audio, 10.0, 11.0) is False and not stopped                          # not for Digidaw: it goes on
        c, stopped, heard = _core_with(["cukup"])
        assert c._barge_phrase(audio, 10.0, 11.0) is False and not stopped                          # its own "cukup", what was left of the echo
        c, stopped, heard = _core_with(["suhunya cukup panas"])
        assert c._barge_phrase(audio, 10.0, 11.0) is False and not stopped
        # nobody could read it
        c, stopped, heard = _core_with([])
        assert c._barge_phrase(audio, 10.0, 11.0, True) is True and stopped                         # a short clear voice
        c, stopped, heard = _core_with([])
        assert c._barge_phrase(audio, 10.0, 11.0, False) is False and not stopped                   # a beep, a clink
        c, stopped, heard = _core_with([])
        assert c._barge_phrase(audio, 10.0, 12.2, True) is False and not stopped                    # long: someone talking, or the TV
    finally:
        core.tts.stop, core.stt.stt_engine.guesses, tts.tts_engine.saying = real_stop, real_guesses, saying


def test_a_stopped_answer_stays_stopped():
    real_stop, real_speak = core.tts.stop, core.tts.speak
    spoken = []
    core.tts.stop = lambda: None
    core.tts.speak = lambda text: spoken.append(text)
    try:
        c = core.DigidawCore(voice_enabled=False)
        c.open_conversation = lambda seconds: None

        def hushed_meanwhile(text):
            c._hush("cukup")                                   # said during the filler
            return "jawabannya panjang sekali", False
        c._run_with_deadline = hushed_meanwhile
        c._handle("ceritakan sesuatu", "voice")
        assert not spoken
        c._run_with_deadline = lambda text: ("jawaban berikutnya", False)
        c._handle("ceritakan lagi", "voice")
        assert spoken == ["jawaban berikutnya"]                # the next command speaks as always
        # "cukup... cukup": the second must not reach the brain (it would pause the music)
        got, spoken_before = [], len(spoken)
        c._hush("cukup")
        c.submit = lambda text, source: got.append(text)
        c.armed_until = time.time() + 5
        c.on_heard("cukup", time.time(), time.time())
        assert not got and len(spoken) == spoken_before
        c._hushed_at = time.time() - core.HUSH_REPEAT_SECONDS - 1
        c.on_heard("cukup", time.time(), time.time())
        assert got or len(spoken) > spoken_before              # later it is an ordinary word again ("Sama-sama, Tuan.")
    finally:
        core.tts.stop, core.tts.speak = real_stop, real_speak


def test_listening_for_stop_is_not_a_call():
    import json
    import subprocess
    import presence

    class Recorder:
        pid = 4242

    class Done:
        def __init__(self, data):
            self.stdout = json.dumps(data)

    def listing(streams):
        def run(args, **k):
            return Done({"sources": [{"index": 60, "name": "mic"}],
                         "clients": [{"index": 1, "properties": {"application.process.id": "4242"}}, {"index": 2, "properties": {"application.process.id": "777"}}],
                         "source-outputs": streams}[args[-1]])
        return run

    ours = {"client": 1, "source": 60, "properties": {"application.name": "parec"}}
    other = {"client": 2, "source": 60, "properties": {"application.name": "Discord"}}
    real_run, real_recorders = subprocess.run, list(aec._recorders)
    aec._recorders[:] = [Recorder()]
    try:
        subprocess.run = listing([ours])
        presence._call.update(at=0.0, app="")
        assert presence.in_call() == ""
        subprocess.run = listing([ours, other])
        presence._call.update(at=0.0, app="")
        assert presence.in_call() == "Discord"                 # a real call still counts
    finally:
        subprocess.run = real_run
        aec._recorders[:] = real_recorders
        presence._call.update(at=0.0, app="")


def test_everyday_microphone_stays_shut_while_talking():
    c = core.DigidawCore(voice_enabled=False)
    engine = tts.tts_engine
    saved = engine.speaking
    engine.speaking = True
    try:
        assert c.mic_muted() is True
    finally:
        engine.speaking = saved


def test_holding_the_voice():
    engine = tts.tts_engine
    engine.speaking = False
    engine.pause()
    assert engine._paused is False                             # nothing to hold
    engine.speaking = True
    try:
        engine.pause()
        assert engine._paused is True
        engine.resume()
        assert engine._paused is False
        engine.pause()
        engine.stop()
        assert engine._paused is False                         # stopped for good: nothing is left waiting
    finally:
        engine.speaking = False
        engine._stop.clear()


def test_own_words_are_not_an_interruption():
    engine = tts.tts_engine
    saved = engine.saying
    engine.saying = "Cuaca hari ini cukup panas, Tuan."
    try:
        assert tts.echo_of_own_voice("cukup")
        assert tts.echo_of_own_voice("cuaca hari cukup panas salah")   # a mishearing among them
        assert not tts.echo_of_own_voice("stop")
        assert not tts.echo_of_own_voice("")
    finally:
        engine.saying = saved


def test_long_first_sentence_starts_with_a_clause():
    forecast = "Besok, Jumat, di Bandung: gerimis, suhu 25 sampai 33 derajat, peluang hujan 97 persen, paling mungkin sekitar jam 13.00, Tuan."
    pieces = tts.in_clauses(forecast)
    assert " ".join(pieces) == forecast
    assert len(pieces) > 1 and all(len(p) <= tts.FIRST_PIECE_CHARS for p in pieces)
    assert all(len(p) >= tts.SHORTEST_PIECE for p in pieces)   # no lone ", Tuan."
    assert tts.in_clauses("Baterai 2,7 giga tersisa.") == ["Baterai 2,7 giga tersisa."]
    long_no_comma = "Aku tidak tahu status Discord dan Telegram dari hasil sistem karena datanya belum tersedia di sini dan belum pernah dicatat"
    assert " ".join(tts.in_clauses(long_no_comma)) == long_no_comma and len(tts.in_clauses(long_no_comma)) > 1
    engine = tts.tts_engine
    short = ["Timer sepuluh menit diset."]
    assert engine._quick_start(short) == short
    real_exists = tts.Path.exists
    tts.Path.exists = lambda self: True
    try:
        whole = ["Besok gerimis, suhu 25 sampai 33 derajat, peluang hujan 97 persen, Tuan."]
        assert engine._quick_start(whole) == whole             # its whole recording is here: instant anyway
    finally:
        tts.Path.exists = real_exists
    assert len(engine._quick_start([forecast, "Bawa payung."])) > 2
    live = tts.LiveSpeech()
    live._speaker = object()
    live.feed(forecast + " Bawa payung ya, Tuan.")
    live.flush()
    assert live._sentences[0] == tts.in_clauses(tts.clean_for_speech(forecast))[0] and len(live._sentences[0]) <= tts.FIRST_PIECE_CHARS
    assert live._sentences[-1].startswith("Bawa payung")


def _picking(heard, purpose, options):
    brain._pending_pick.clear()
    brain._pending_pick.update(heard=heard, purpose=purpose, options=options, at=time.time())


def test_answering_which_one_is_meant():
    ran, learned = [], []
    real_tools, real_learn = dict(brain.TOOL_MAP), habits.learn_alias
    for name in list(brain.TOOL_MAP):
        brain.TOOL_MAP[name] = lambda _name=name, **k: (ran.append((_name, k)), "ok")[1]
    habits.learn_alias = lambda a, b: learned.append((a, b))
    options = [("Firefox", "firefox", ("open_application", {"name": "firefox"})), ("Notepad", "notepad", ("open_application", {"name": "notepad"}))]
    try:
        for reply in ("tutup notepad", "hapus notepad", "pindah ke notepad"):       # another command: not an answer ("tutup notepad" once opened it)
            _picking("firefox tutup notepad", "open", options)
            assert brain._answer_pick(reply) is None and not ran and not brain._pending_pick, reply
        for reply, name in (("notepad", "notepad"), ("buka notepad", "notepad"), ("yang notepad aja", "notepad"), ("firefox dong", "firefox"), ("iya", "firefox"), ("yang kedua", "notepad")):
            ran.clear()
            _picking("firefox tutup notepad", "open", options)
            assert brain._answer_pick(reply) == "ok" and ran == [("open_application", {"name": name})], reply
        assert not learned                                     # two commands in one sentence are no mishearing of a name
        _picking("tito", "open", [("TikTok", "tiktok", ("open_website", {"site": "tiktok"}))])
        brain._answer_pick("ya")
        assert learned == [("tito", "tiktok")]
    finally:
        brain.TOOL_MAP.clear()
        brain.TOOL_MAP.update(real_tools)
        habits.learn_alias = real_learn
        brain._pending_pick.clear()


def test_two_commands_without_a_joining_word():
    ran = []
    real_tools = dict(brain.TOOL_MAP)
    for name in list(brain.TOOL_MAP):
        brain.TOOL_MAP[name] = lambda _name=name, **k: (ran.append((_name, k.get("name"))), "ok")[1]
    real_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        for sentence in ("buka firefox tutup notepad", "buka firefox dan tutup notepad"):
            ran.clear()
            assert brain._compound(sentence)
            assert ran == [("open_application", "firefox"), ("close_application", "notepad")], (sentence, ran)
        for sentence in ("buka tutup botol", "cara buka tutup botol", "buka firefox"):
            ran.clear()
            assert brain._compound(sentence) is None and not ran, sentence
    finally:
        time.sleep = real_sleep
        brain.TOOL_MAP.clear()
        brain.TOOL_MAP.update(real_tools)


def test_runner_gives_memory_back():
    posts, warmed = [], []

    class Response:
        def raise_for_status(self):
            pass

    real_post, real_memory, real_warm = local_llm.requests.post, local_llm._runner_memory_mb, local_llm.warm_up
    saved = local_llm._warm_request, local_llm._last_request
    local_llm.requests.post = lambda url, json=None, timeout=None: (posts.append(json), Response())[1]
    local_llm.warm_up = lambda *a: warmed.append(a)
    local_llm._warm_request = ("sistem", ())
    try:
        local_llm._runner_memory_mb = lambda: 6300
        local_llm._last_request = time.time()
        assert local_llm.tidy_runner() is False and not posts   # in use: not now
        local_llm._last_request = time.time() - local_llm.RUNNER_IDLE_SECONDS - 1
        assert local_llm.tidy_runner() is True
        assert posts[0]["keep_alive"] == 0 and warmed == [("sistem", ())]
        posts.clear()
        local_llm._runner_memory_mb = lambda: 900
        assert local_llm.tidy_runner() is False and not posts   # small: left alone
    finally:
        local_llm.requests.post, local_llm._runner_memory_mb, local_llm.warm_up = real_post, real_memory, real_warm
        local_llm._warm_request, local_llm._last_request = saved


def test_close_answers_with_the_word_said():
    class Proc:
        info = {"name": "mousepad", "pid": 999999}
        pid = 999999
    real = system_control._matching_processes, system_control._quit_processes
    system_control._matching_processes = lambda name: [Proc()]
    system_control._quit_processes = lambda procs: ([], [])
    try:
        import window_manager
        real_list = window_manager._windows
        window_manager._windows = lambda: []
        try:
            assert system_control.close_application("notepad").startswith("Notepad sudah ditutup")
            assert system_control.close_application("mousepad").lower().startswith("mousepad sudah ditutup")
        finally:
            window_manager._windows = real_list
    finally:
        system_control._matching_processes, system_control._quit_processes = real


if __name__ == "__main__":
    ok = True
    for test in (test_canceller_learns_the_echo, test_listener_tells_a_voice_from_the_echo, test_listener_limits_false_alarms, test_listener_room_and_beeps, test_frame_level_is_rms_without_the_rumble,
                 test_speaker_gain_follows_the_system_volume, test_filter_is_only_trusted_while_it_works, test_what_is_said_over_the_voice, test_a_stopped_answer_stays_stopped, test_listening_for_stop_is_not_a_call,
                 test_everyday_microphone_stays_shut_while_talking, test_holding_the_voice, test_own_words_are_not_an_interruption, test_long_first_sentence_starts_with_a_clause,
                 test_answering_which_one_is_meant, test_two_commands_without_a_joining_word, test_runner_gives_memory_back, test_close_answers_with_the_word_said):
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
