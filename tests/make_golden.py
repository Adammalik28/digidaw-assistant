"""
Builds tests/golden_commands.json: real commands from logs/digidaw.log with the decision the router makes for them
now (on the fake desktop of test_router), so test_golden.py notices when a change makes Digidaw do something else
with what the user really says. Run again after an INTENDED change in routing, and look at the diff before keeping it.

Only device, app, media and lamp commands are kept: nothing with a person's name, a message, a note, an agenda
entry, a memory or a search (this file goes into git).

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/make_golden.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import golden  # noqa: E402

KEEP_TOOLS = {"wifi_power", "wifi_connect", "bluetooth", "power_mode", "do_not_disturb", "audio_output", "screen_power",
              "click_here", "click_on_screen", "stop_task", "resume_task", "list_folder", "open_application", "close_application", "focus_window", "open_website", "close_tab", "media_control",
              "play_youtube", "set_volume", "volume_up", "volume_down", "volume_mute", "set_brightness", "lamp_power",
              "lamp_brightness", "lamp_color", "lamp_scene", "lamp_status", "get_time", "get_date", "switch_workspace",
              "move_to_workspace", "show_hud", "open_many", "close_many", "screen_time", "daily_recap", "now_playing",
              "take_screenshot", "lock_workstation", "get_system_status", "get_weather", "get_forecast", "list_timers",
              "cancel_timers", "sun_today", "list_watches", "performance_report", "minimize_window", "maximize_window",
              "fullscreen_window", "close_window", "scroll", "zoom", "navigate", "new_tab", "show_desktop", "_ask"}


# place names, a campus, words that may be people's names (tests/private_words.txt): never in git
_WORDS_FILE = os.path.join(os.path.dirname(__file__), "private_words.txt")      # one per line, not in git
_words = [w.strip().lower() for w in open(_WORDS_FILE, encoding="utf-8")] if os.path.exists(_WORDS_FILE) else []
PRIVATE_WORDS = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in _words if w) + r")\b") if any(_words) \
    else re.compile(r"(?!)")


def main():
    folder = os.path.join(os.path.dirname(__file__), "..", "logs")
    seen, cases = set(), []
    lines = []
    for name in ("digidaw.log.old", "digidaw.log"):       # (the log is rotated: reading one file once shrank this list from 337 commands to 27)
        if os.path.exists(os.path.join(folder, name)):
            lines += open(os.path.join(folder, name), encoding="utf-8", errors="replace").read().splitlines()
    try:
        with open(os.path.join(os.path.dirname(__file__), "golden_commands.json"), encoding="utf-8") as f:
            lines += [f"Anda: {case['said']}" for case in json.load(f)]        # what was locked stays locked, also after the log it came from is gone
    except (OSError, ValueError):
        pass
    for line in lines:
        m = re.match(r"^(?:\d\d:\d\d:\d\d )?(?:Anda|You \((?:HUD|HP)\)): (.+?)(?:\s+\(dikenali.*)?$", line.rstrip("\n"))
        if not m:
            continue
        text = m.group(1).strip()
        key = text.lower()
        if key in seen or len(text) > 60 or PRIVATE_WORDS.search(key):
            continue
        seen.add(key)
        decision = golden.decide(text)
        if decision and decision[0] in KEEP_TOOLS:
            cases.append({"said": text, "tool": decision[0], "args": decision[1]})
    cases += [{"said": t, "tool": None, "args": None} for t in golden.STAYS_WITH_AI]
    out = os.path.join(os.path.dirname(__file__), "golden_commands.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=0)
    print(f"{len(cases)} perintah dikunci -> {out}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
