"""
Tests for the "more of a butler" batch (1 Nov 2026): watches ("kabari saya kalau …"), the daily recap, answers
spoken while the AI writes them, interrupting Digidaw by voice, and replying to WhatsApp. Offline: no network, no
sound, no real WhatsApp.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_jarvis.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config         # noqa: E402
import notifications  # noqa: E402
import watchers       # noqa: E402
import daily          # noqa: E402
import tts            # noqa: E402
import safety         # noqa: E402


def test_watch_download():
    folder = Path(tempfile.mkdtemp())
    w = {"kind": "unduhan", "target": "", "value": "", "created": time.time(), "state": {"folder": str(folder)}}
    (folder / "film.mkv.part").write_bytes(b"x")
    assert watchers._check_download(w) is None and w["state"]["active"]
    (folder / "film.mkv.part").rename(folder / "film.mkv")             # Firefox finished it
    assert "film.mkv sudah selesai" in watchers._check_download(w)
    w = {"kind": "unduhan", "target": "", "value": "", "created": time.time() - 999, "state": {"folder": str(folder)}}
    assert "tidak melihat unduhan" in watchers._check_download(w)       # nothing was ever downloading


def test_watch_message():
    said = []
    watchers.ANNOUNCE = lambda text, level="info", **kw: said.append((text, level)) or True
    watchers._watches.clear()
    assert "Saya kabari" in watchers.watch_for("pesan", "rina")
    assert "sudah saya pantau" in watchers.watch_for("pesan", "Rina")
    other = {"app": "WhatSie", "summary": "Budi", "body": "halo", "at": time.time(), "read": False}
    assert watchers.on_notification(other) is False                       # someone else: the usual announcement
    hers = {"app": "WhatSie", "summary": "Rina", "body": "oke otw", "at": time.time(), "read": False}
    assert watchers.on_notification(hers) is True                         # hers: announced by the watch instead
    deadline = time.time() + 3
    while not said and time.time() < deadline:
        time.sleep(0.02)
    assert said and "Rina sudah membalas" in said[0][0] and said[0][1] == "reminder", said
    assert notifications.take_offer() == "Rina"                           # "ya" reads her message
    assert notifications.last_chat()["name"] == "Rina"                    # "balas …" answers her
    assert not watchers._watches                                          # done: watched once


def test_watch_battery_and_list():
    class Battery:
        def __init__(self, percent, plugged):
            self.percent, self.power_plugged = percent, plugged
    import psutil
    real = psutil.sensors_battery
    try:
        psutil.sensors_battery = lambda: Battery(97, True)
        w = {"kind": "baterai", "value": "penuh", "state": {}}
        assert watchers._check_battery(w) is None
        psutil.sensors_battery = lambda: Battery(100, True)
        assert "penuh" in watchers._check_battery(w)
        psutil.sensors_battery = lambda: Battery(18, False)
        assert "18 persen" in watchers._check_battery({"kind": "baterai", "value": "<20", "state": {}})
    finally:
        psutil.sensors_battery = real
    watchers._watches.clear()
    watchers.watch_for("pesan", "budi")
    assert "pesan dari budi" in watchers.list_watches()
    assert "berhenti memantau pesan dari budi" in watchers.cancel_watch("budi")
    assert "Tidak ada" in watchers.list_watches()


def test_daily_recap():
    daily.DIR = Path(tempfile.mkdtemp())
    with daily._lock:
        day = daily._current()
        day.update(active=5 * 3600 + 20 * 60, apps={"Firefox": 2 * 3600, "VS Code": 3600 + 1800, "Discord": 60},
                   commands=34, tools={"lamp_power": 3, "lamp_scene": 1, "play_youtube": 1})
    text = daily.screen_time()
    assert "5 jam 20 menit" in text and "Firefox 2 jam" in text and "Discord" not in text, text
    recap = daily.daily_recap()
    assert "34 perintah" in recap and "soal lampu" in recap, recap
    assert daily.app_name({"class": "com.ktechpit.whatsie"}) == daily.app_name({"class": "whatsie"}) == "WhatsApp"
    assert daily.app_name({"class": "hyprlock"}) == ""
    daily.save()
    assert (daily.DIR / f"{daily._today()}.json").exists()


def test_live_speech_sentences():
    """The AI's text, arriving in pieces, becomes whole sentences in order (short ones wait for the next)."""
    spoken = []

    def fake_speak_live(self, live):
        i = 0
        while (s := live.sentence(i)) is not None:
            spoken.append(s)
            i += 1
        live.finished.set()
    real = tts.NeuralVoice.speak_live
    tts.NeuralVoice.speak_live = fake_speak_live
    try:
        import persona
        live = tts.LiveSpeech(shape=lambda s, first: persona.live_sentence(s.replace("Sir", "Tuan"), first))
        for piece in ["Langit biru karena ham", "buran Rayleigh, Sir. Ya. Cahaya ", "biru punya gelombang 450 nm, lebih ",
                      "pendek dari merah.\n- Jadi lebih tersebar"]:
            live.feed(piece)
            time.sleep(0.05)
        live.close()
        live.wait(3)
    finally:
        tts.NeuralVoice.speak_live = real
    assert spoken == ["Langit biru karena hamburan Rayleigh, Tuan.",            # the title stays in the first one
                      "Ya. Cahaya biru punya gelombang 450 nm, lebih pendek dari merah.",   # "Ya." waited
                      "Jadi lebih tersebar"], spoken                                # (the list bullet isn't said)


def test_merge_stream():
    """Streamed chunks become the same response generate_content gives (text + its thought signature, calls)."""
    from google.genai import types
    import brain

    def chunk(*parts, finish=None):
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=list(parts)), finish_reason=finish)])
    text = brain._merge_stream([chunk(types.Part(text="Halo, ")), chunk(types.Part(text="Tuan.")),
                                chunk(types.Part(text="", thought_signature=b"sig"), finish="STOP")])
    parts = text.candidates[0].content.parts
    assert len(parts) == 1 and parts[0].text == "Halo, Tuan." and parts[0].thought_signature == b"sig"
    call = brain._merge_stream([chunk(types.Part(function_call=types.FunctionCall(name="get_weather", args={"city": "X"}),
                                                 thought_signature=b"s")), chunk(types.Part(text=""), finish="STOP")])
    assert [p.function_call.name for p in call.candidates[0].content.parts] == ["get_weather"]
    assert call.function_calls and call.function_calls[0].name == "get_weather"
    empty = brain._merge_stream([])
    assert empty.candidates[0].content is None


def test_barge_in():
    """While Digidaw talks (echo cancellation on), only "Digidaw …" or "stop" interrupts it."""
    import core
    import aec
    stopped = []
    real_stop, real_chime = core.tts.stop, core.chime
    core.tts.stop = lambda: stopped.append(True)
    core.chime = lambda up=True: None
    aec._ready.set()
    saved_barge = config.BARGE_IN
    config.BARGE_IN = True                                              # (off by default since 3 Nov)
    try:
        c = core.DigidawCore(voice_enabled=False)
        assert c.mic_muted() is False                                   # it keeps listening while it talks
        assert c._barge_in("terus dia bilang begitu") is False and not stopped     # TV / echo: ignored
        assert c._barge_in("stop") is False and stopped                 # stop: speech ends, nothing else happens
        stopped.clear()
        assert c._barge_in("Digidaw, cukup") is False and stopped
        stopped.clear()
        assert c._barge_in("Digidaw, putar musik") is True and stopped  # stop talking, then do this
        stopped.clear()
        assert c._barge_in("Digidaw") is True and stopped               # stop, and listen for the command
    finally:
        aec._ready.clear()
        config.BARGE_IN = saved_barge
        core.tts.stop, core.chime = real_stop, real_chime
    assert c.mic_muted() == core.tts.is_audible()                       # without echo cancellation: as before


def test_reply_needs_yes():
    """A WhatsApp reply is never sent without "ya", even with confirmations turned off."""
    import whatsapp
    sent = []
    whatsapp._send = lambda name, text: sent.append((name, text)) or f"Terkirim ke {name}."
    whatsapp.shutil.which = lambda name: "/usr/bin/" + name
    saved = config.CONFIRM_DANGEROUS_ACTIONS
    config.CONFIRM_DANGEROUS_ACTIONS = False
    try:
        notifications._last_chat.update(name="Rina", app="WhatSie", at=time.time())
        result = whatsapp.reply_message("oke saya otw")
        assert result.startswith(safety.CONFIRMATION_TOKEN) and not sent
        assert 'ke Rina: "oke saya otw"' in safety.pending_description()
        assert safety.confirm() == "Terkirim ke Rina." and sent == [("Rina", "oke saya otw")]
        notifications._last_chat.update(name="Ibu", app="org.telegram.desktop", at=time.time())
        assert "baru WhatsApp" in whatsapp.reply_message("oke")
        notifications._last_chat.update(at=0)
        assert "ke siapa" in whatsapp.reply_message("oke")
    finally:
        config.CONFIRM_DANGEROUS_ACTIONS = saved


def test_whatsapp_send_checks():
    """The send only presses Enter in the right chat, with the right text in the box."""
    import whatsapp
    import importlib
    whatsapp = importlib.reload(whatsapp)

    class FakeTab:
        def __init__(self, header, compose_after_typing=None):
            self.header, self.compose, self.keys, self.after = header, "", [], compose_after_typing

        def js(self, expr):
            if expr == whatsapp.JS_READY:
                return True
            if expr.lstrip().startswith("((want)"):
                return {"x": 10, "y": 20, "title": "Rina Kusuma"}
            if expr == whatsapp.JS_HEADER:
                return self.header
            if expr == whatsapp.JS_FOCUS_COMPOSE:
                return True
            if expr == whatsapp.JS_COMPOSE_TEXT:
                return self.compose
            return None

        def call(self, method, params=None, timeout=10):
            if method == "Input.insertText":
                self.compose = self.after if self.after is not None else params["text"]
            if method == "Input.dispatchKeyEvent" and params["type"] == "keyDown":
                self.keys.append(params["key"])
                if params["key"] == "Enter":
                    self.compose = ""
            return {}

        def close(self):
            pass

    whatsapp.time.sleep = lambda s: None
    tab = FakeTab("Rina Kusuma")
    whatsapp._connect = lambda: (tab, "")
    assert whatsapp._send("rina", "oke saya otw").startswith("Terkirim ke Rina Kusuma") and "Enter" in tab.keys
    tab = FakeTab("Budi")                                              # another chat opened: nothing is sent
    whatsapp._connect = lambda: (tab, "")
    assert "tidak saya temukan" in whatsapp._send("rina", "oke") and "Enter" not in tab.keys
    tab = FakeTab("Rina Kusuma", compose_after_typing="ok")            # the text didn't arrive whole: not sent
    whatsapp._connect = lambda: (tab, "")
    assert "tidak masuk dengan benar" in whatsapp._send("rina", "oke saya otw") and "Enter" not in tab.keys


if __name__ == "__main__":
    ok = True
    for test in (test_watch_download, test_watch_message, test_watch_battery_and_list, test_daily_recap,
                 test_live_speech_sentences, test_merge_stream, test_barge_in, test_reply_needs_yes,
                 test_whatsapp_send_checks):
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
