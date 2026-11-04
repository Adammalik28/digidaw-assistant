"""
Tests for the response-time work: the DNS cache (dnscache.py), the environment programs Digidaw opens get
(oscompat.user_env), and the early chime from the laptop's own recognizer (stt.py + core.wake_hint).

Run:  PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_speed.py
(also works with pytest). Nothing goes to the network, nothing is spoken or played.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dnscache             # noqa: E402
import oscompat             # noqa: E402

V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 0, 0, 0))
V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))


def test_dns_cache():
    calls = []

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, port, type))
        if host == "gone.example":
            raise socket.gaierror(-2, "Name or service not known")
        return [V6, V4]

    real, dnscache._real = dnscache._real, fake
    dnscache._cache.clear()
    try:
        first = dnscache.getaddrinfo("www.google.com", 443, 0, socket.SOCK_STREAM)
        assert first[0][4] == ("2001:db8::1", 443, 0, 0) and first[1][4] == ("192.0.2.1", 443)
        again = dnscache.getaddrinfo("WWW.google.com", 80, 0, socket.SOCK_STREAM)      # other port, other case
        assert again[1][4] == ("192.0.2.1", 80) and len(calls) == 1                    # answered from memory
        dnscache.getaddrinfo("www.google.com", 53, 0, socket.SOCK_DGRAM)               # UDP: not cached
        dnscache.getaddrinfo("127.0.0.1", 80, 0, socket.SOCK_STREAM)                   # a number: not a lookup
        dnscache.getaddrinfo("localhost", 11434, 0, socket.SOCK_STREAM)
        assert len(calls) == 4
        key = ("www.google.com", 0)
        dnscache._cache[key] = (time.time() - dnscache.REFRESH - 1, [V4])            # old: still used, renewed
        assert dnscache.getaddrinfo("www.google.com", 443, 0, socket.SOCK_STREAM)[0][4] == ("192.0.2.1", 443)
        for _ in range(50):
            if dnscache._cache[key][1] == [V6, V4]:
                break
            time.sleep(0.02)
        assert dnscache._cache[key][1] == [V6, V4]                                     # renewed in the background
        try:
            dnscache.getaddrinfo("gone.example", 443, 0, socket.SOCK_STREAM)
            raise AssertionError("an unknown name must fail like the real lookup")
        except socket.gaierror:
            pass
    finally:
        dnscache._real = real
        dnscache._cache.clear()


def test_user_env():
    os.environ["DIGIDAW_NO_CONSOLE"], os.environ["OMP_NUM_THREADS"] = "1", "1"
    oscompat.OWN_ENV.add("OMP_NUM_THREADS")
    try:
        env = oscompat.user_env()
        assert "DIGIDAW_NO_CONSOLE" not in env and "OMP_NUM_THREADS" not in env and env.get("PATH")
        assert oscompat.detached()["env"] == env
    finally:
        oscompat.OWN_ENV.discard("OMP_NUM_THREADS")


def test_early_chime():
    """"Digidaw" heard on the laptop: the chime comes before Google; Google hearing something else takes it back."""
    import config
    import local_stt
    import stt
    engine = stt.stt_engine
    seen = []
    saved = (local_stt.available, local_stt.transcribe, engine._recognize, config.STT_PRIVACY_MODE)
    engine.on_wake_hint = lambda text: seen.append(("hint", text)) or True
    engine.on_wake_unconfirmed = lambda: seen.append(("undo",))
    local_stt.available = lambda size=None: True
    config.STT_PRIVACY_MODE = True
    engine.online = True
    try:
        local_stt.transcribe = lambda audio, size=None: "Digidaw."
        engine._recognize = lambda audio: "Digidaw"
        assert engine._recognize_smart(None, 0.8) == "Digidaw" and seen == [("hint", "Digidaw.")]
        seen.clear()
        engine._recognize = lambda audio: "pigi chord"                     # Google misheard the name: chime stands
        engine._recognize_smart(None, 0.8)
        assert seen == [("hint", "Digidaw.")]
        seen.clear()
        engine._recognize = lambda audio: "dia bilang begitu ke aku tadi"   # someone else talking: take it back
        engine._recognize_smart(None, 0.8)
        assert seen == [("hint", "Digidaw."), ("undo",)]
        seen.clear()
        engine._recognize = lambda audio: ""                               # Google heard nothing: imagined, undo
        engine._recognize_smart(None, 0.8)
        assert seen == [("hint", "Digidaw."), ("undo",)]
        seen.clear()
        engine._recognize_smart(None, 3.5)                                 # 3.5 s of sound isn't "Digidaw" alone
        assert seen == []
        seen.clear()
        local_stt.transcribe = lambda audio, size=None: "lagi apa kamu"     # not for Digidaw: never leaves the PC
        assert engine._recognize_smart(None, 0.8) == "lagi apa kamu" and seen == []
    finally:
        local_stt.available, local_stt.transcribe, engine._recognize, config.STT_PRIVACY_MODE = saved
        engine.on_wake_hint = engine.on_wake_unconfirmed = None


class _Done:
    def wait(self, timeout=None):
        return True


def test_wake_hint():
    import config
    import core
    import media
    import tts
    chimes = []
    import presence
    saved = (core.chime, media.duck, tts.prewarm, core._briefing_due, presence.busy_reason)
    presence.busy_reason = lambda front=None: ""                 # (this test process isn't Digidaw: its mic is "a call")
    core.chime = lambda up=True: chimes.append(up)
    media.duck = lambda: None
    tts.prewarm = lambda: None
    core._briefing_due = lambda: False
    try:
        c = core.DigidawCore(voice_enabled=False)
        assert not c.wake_hint("Digidaw, buka Firefox.")          # a whole command: Google's text runs it
        assert c.wake_hint("Digidaw.") and chimes == [True] and time.time() < c.armed_until
        c.on_heard("Digidaw", time.time() - 1, time.time())         # Google confirms: no second chime
        assert chimes == [True] and c._awaiting_command
        c.close_conversation()
        assert c.wake_hint("Hei Digidaw!")
        c.wake_unconfirmed()                                         # Google heard something else: only doubted
        assert time.time() < c.armed_until and c._armed_loose
        c.close_conversation()
        c.voice_enabled, speak = True, tts.speak
        tts.speak = lambda text: None
        c.announce("Tuan, ada pesan WhatsApp dari Rina. Mau saya bacakan?", "message", listen=10)
        tts.speak = speak
        heard = []
        c.submit = lambda text, source: heard.append(text) or threading.Event().set() or _Done()
        c.on_heard("tulang lunak itu bagian tubuh", time.time() - 1, time.time())   # TV after the question: ignored
        assert not heard
        c.on_heard("ya bacakan", time.time() - 1, time.time())                       # the answer: taken
        assert heard == ["ya bacakan"], heard
        c.close_conversation()
        core._briefing_due = lambda: True
        config.STARTUP_BRIEFING, saved_briefing = True, config.STARTUP_BRIEFING
        assert c.wake_hint("Digidaw.")         # the day's first call chimes at once too (the summary comes later)
        config.STARTUP_BRIEFING = saved_briefing
    finally:
        core.chime, media.duck, tts.prewarm, core._briefing_due, presence.busy_reason = saved


if __name__ == "__main__":
    ok = True
    for test in (test_dns_cache, test_user_env, test_early_chime, test_wake_hint):
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            ok = False
            print(f"✗ {test.__name__}\n{e}")
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
