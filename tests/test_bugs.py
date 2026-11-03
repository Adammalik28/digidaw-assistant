"""
Regression tests for bugs found in the logs (31 Oct 2026): confirmations answered by chatter or hours later, a timed-out
command acting after its apology, the phone getting another command's answer, and the systemd restart.

Run:  PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_bugs.py
(also works with pytest). Nothing is run, opened or spoken.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config               # noqa: E402
import events               # noqa: E402
import safety               # noqa: E402


def test_confirmation_answers():
    ran = []
    config.CONFIRM_DANGEROUS_ACTIONS = True
    safety.guard("mematikan komputer dalam 15 menit", lambda: ran.append(1))
    try:
        for said in ("ya", "iya lanjut", "Ya, matikan saja.", "oke matikan komputer", "iya bener"):
            assert safety.classify_answer(said) == "yes", said
        for said in ("tidak", "nggak usah", "jangan dulu", "batal saja"):
            assert safety.classify_answer(said) == "no", said
        for said in ("ya ampun", "oke nanti aku telepon", "iya sih tapi capek", "lanjutkan musiknya"):
            assert safety.classify_answer(said) == "other", said            # chatter, not an answer
        assert safety.has_pending()
        safety._pending["at"] -= safety.PENDING_SECONDS + 1
        assert not safety.has_pending() and ran == []                        # expired, never run
    finally:
        safety._pending = None


def test_timed_out_command_stays_quiet():
    import brain
    tool = next(f for f in brain.TOOLS if f.__name__ == "get_time")    # every skill has the same wrapper
    brain._command_local.id = 999_999
    brain._cancelled.add(999_999)
    try:
        try:
            tool()
            raise AssertionError("a cancelled command must not run a skill")
        except brain.Cancelled:
            pass
    finally:
        brain._cancelled.discard(999_999)
        brain._command_local.id = None


def test_reply_belongs_to_its_command():
    results = {}

    def worker(cmd_id, text, delay):
        events.working_on(cmd_id)
        time.sleep(delay)
        events.emit("digidaw", text=text)
        events.working_on(None)

    a = threading.Thread(target=worker, args=(1, "jawaban perintah HP", 0.05))
    b = threading.Thread(target=worker, args=(None, "Sama-sama, Tuan.", 0.0))     # said by the voice thread
    a.start(); b.start(); a.join(); b.join()
    for e in list(events.history):
        if e.get("type") == "digidaw" and e.get("text") in ("jawaban perintah HP", "Sama-sama, Tuan."):
            results[e["text"]] = e.get("cmd")
    assert results == {"jawaban perintah HP": 1, "Sama-sama, Tuan.": None}


def test_several_api_keys():
    """Key 1 at its limit on every model -> key 2; key 1 has room again -> back to key 1; a refused key is skipped."""
    import llm

    class Quota(Exception):
        code = 429

        def __str__(self):
            return "429 RESOURCE_EXHAUSTED {'retryDelay': '30s'}"

    class Refused(Exception):
        code = 400

        def __str__(self):
            return "API_KEY_INVALID API key not valid"

    saved = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = "kunci-satu, kunci-dua\nkunci-tiga"
    try:
        llm.reset_keys()
        assert llm.configured_keys() == ["kunci-satu", "kunci-dua", "kunci-tiga"] and llm.active_key() == 1
        assert llm.candidates() and llm.active_key() == 1
        for model in config.FALLBACK_MODELS:                       # every model of key 1 hits its quota
            llm.report_error(model, Quota())
        assert llm.candidates() and llm.active_key() == 2           # requests move to key 2
        llm.report_error(config.FALLBACK_MODELS[0], Refused())       # key 2 is refused by Google
        assert llm.candidates() and llm.active_key() == 3           # ...and skipped
        for model in config.FALLBACK_MODELS:                       # key 1's rest is over
            llm._states[0]["resting_until"][model] = 0
        assert llm.candidates() and llm.active_key() == 1           # back to the first key
    finally:
        config.GEMINI_API_KEY = saved
        llm.reset_keys()


def test_systemd_unit_name():
    sys.argv = ["main.py", "--no-hud", "--text", "--background"]
    os.environ.pop("DIGIDAW_NO_CONSOLE", None)
    import main
    saved = os.environ.get("INVOCATION_ID")
    os.environ.pop("INVOCATION_ID", None)
    assert main._systemd_unit() == ""                                          # started by hand: no systemd
    base = "0::/user.slice/user-1000.slice/user@1000.service/"
    assert main._systemd_unit(base + "app.slice/app-graphical.slice/app-digidaw@autostart.service") == \
        "app-digidaw@autostart.service"
    # started from the app menu: inside Hyprland's own unit, which must never be restarted (= logout)
    assert main._systemd_unit(base + "session.slice/wayland-wm@hyprland.desktop.service") == ""
    if saved:
        os.environ["INVOCATION_ID"] = saved


if __name__ == "__main__":
    ok = True
    for test in (test_confirmation_answers, test_timed_out_command_stays_quiet, test_reply_belongs_to_its_command,
                 test_several_api_keys, test_systemd_unit_name):
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            ok = False
            print(f"✗ {test.__name__}\n{e}")
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
