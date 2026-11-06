"""
Protocols: named multi-step routines ("Digidaw, aktifkan protokol kerja", "House Party Protocol").

Each step calls one of DIGIDAW's skills by name. Protocols live in protocols.json (created
with defaults on first run) so they can be edited by hand or created by voice via create_protocol.
"""
import json
import re
import time
import difflib

import config
import storage
import smarthome

T = config.USER_TITLE
PROTOCOLS_FILE = config.BASE_DIR / "protocols.json"

DEFAULT_PROTOCOLS = {
    "kerja": {
        "aliases": ["work", "mode kerja", "work mode", "kantor"],
        "description": "Siapkan lingkungan kerja: VS Code, GitHub, volume pelan",
        "steps": [
            {"tool": "media_control", "args": {"action": "pause"}},
            {"tool": "open_application", "args": {"name": "vs code"}},
            {"tool": "open_website", "args": {"site": "github"}},
            {"tool": "set_volume", "args": {"level": 30}},
            {"tool": "lamp_scene", "args": {"scene": "kerja"}},
        ],
    },
    "santai": {
        "aliases": ["relax", "chill", "mode santai", "rileks"],
        "description": "Musik lofi santai dan layar lebih nyaman",
        "steps": [
            {"tool": "play_youtube", "args": {"query": "lofi hip hop chill beats", "mix": True}},
            {"tool": "set_volume", "args": {"level": 35}},
            {"tool": "set_brightness", "args": {"level": 60}},
            {"tool": "lamp_scene", "args": {"scene": "santai"}},
        ],
    },
    "fokus": {
        "aliases": ["focus", "pomodoro", "belajar", "mode fokus"],
        "description": "Sesi fokus Pomodoro 25 menit dengan musik konsentrasi",
        "steps": [
            {"tool": "window_action", "args": {"action": "minimize_all"}},
            {"tool": "play_youtube", "args": {"query": "deep focus music for studying", "mix": True}},
            {"tool": "set_volume", "args": {"level": 25}},
            {"tool": "lamp_scene", "args": {"scene": "fokus"}},
            {"tool": "set_timer", "args": {"minutes": 25, "message": "Sesi fokus 25 menit selesai. Saatnya istirahat 5 menit."}},
        ],
    },
    "tidur": {
        "aliases": ["sleep", "malam", "good night", "mode tidur", "selamat tidur"],
        "description": "Hentikan musik, redupkan layar, pelankan suara, lalu sleep",
        "steps": [
            {"tool": "media_control", "args": {"action": "pause"}},
            {"tool": "set_brightness", "args": {"level": 10}},
            {"tool": "set_volume", "args": {"level": 10}},
            {"tool": "lamp_power", "args": {"on": False}},
            {"tool": "sleep_pc", "args": {}},
        ],
    },
    "bersih": {
        "aliases": ["clean slate", "cleanup", "bersih-bersih", "pembersihan"],
        "description": "Clean Slate Protocol: hapus file temp dan kosongkan Recycle Bin",
        "steps": [
            {"tool": "system_cleanup", "args": {}},
        ],
    },
    "pesta": {
        "aliases": ["house party", "party", "house party protocol"],
        "description": "House Party Protocol: musik pesta, volume besar",
        "steps": [
            {"tool": "play_youtube", "args": {"query": "party hits mix", "mix": True}},
            {"tool": "set_volume", "args": {"level": 70}},
            {"tool": "lamp_scene", "args": {"scene": "pesta"}},
        ],
    },
}
# the lamp joined the built-in protocols later (1 Nov 2026): protocols.json files made before get the step once
# (where to put it: "end", or before the step with that tool)
LAMP_STEPS = {"kerja": ({"tool": "lamp_scene", "args": {"scene": "kerja"}}, "end"),
              "santai": ({"tool": "lamp_scene", "args": {"scene": "santai"}}, "end"),
              "fokus": ({"tool": "lamp_scene", "args": {"scene": "fokus"}}, "set_timer"),
              "tidur": ({"tool": "lamp_power", "args": {"on": False}}, "sleep_pc"),
              "pesta": ({"tool": "lamp_scene", "args": {"scene": "pesta"}}, "end")}
LAMP_TOOLS = ("lamp_power", "lamp_brightness", "lamp_color", "lamp_scene")


def load() -> dict:
    if not PROTOCOLS_FILE.exists():
        save(DEFAULT_PROTOCOLS)
        return json.loads(json.dumps(DEFAULT_PROTOCOLS))
    protocols = storage.load_json(PROTOCOLS_FILE)
    if not isinstance(protocols, dict):
        return json.loads(json.dumps(DEFAULT_PROTOCOLS))
    if _add_lamp_steps(protocols):
        save(protocols)
    return protocols


def _add_lamp_steps(protocols: dict) -> bool:
    """Give the built-in protocols their lamp step, once (a step the user removes later stays removed)."""
    changed = False
    for key, (step, before) in LAMP_STEPS.items():
        p = protocols.get(key)
        if not isinstance(p, dict) or p.get("lamp_added"):
            continue
        steps = p.setdefault("steps", [])
        if not any(isinstance(s, dict) and s.get("tool") in LAMP_TOOLS for s in steps):
            at = next((i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("tool") == before), len(steps))
            steps.insert(at, json.loads(json.dumps(step)))
        p["lamp_added"] = True
        changed = True
    return changed


def save(protocols: dict):
    storage.save_json(PROTOCOLS_FILE, protocols, backup=True, indent=2)


def _resolve(name: str, protocols: dict) -> str | None:
    n = name.lower().replace("protokol", "").replace("protocol", "").strip()
    for key, p in protocols.items():
        if n == key or n in [a.lower() for a in p.get("aliases", [])]:
            return key
    for key, p in protocols.items():
        if key in n or any(a.lower() in n for a in p.get("aliases", [])):
            return key
    names = {a.lower(): k for k, p in protocols.items() for a in [k, *p.get("aliases", [])]}
    close = difflib.get_close_matches(n, list(names), n=1, cutoff=0.7)
    return names[close[0]] if close else None


# a new window has to appear before the next step ("buka Firefox", then "pindah ke workspace 2")
PAUSE_AFTER = {"open_on_workspace": 0.3, "open_application": 1.5, "open_many": 2.0, "open_website": 1.0, "open_folder": 1.0, "switch_workspace": 0.4, "move_to_workspace": 0.4}
MAX_WAIT = 30


def _quietly():
    """A routine opens its apps where they belong and leaves the screen where the user is."""
    import contextlib
    import oscompat
    if oscompat.IS_WINDOWS:
        return contextlib.nullcontext()
    import linux_desktop
    return linux_desktop.quiet_opening()


def _run_step(step: dict) -> str | None:
    """One step, done. Its result; None when there is nothing to do or say. Steps made by talking are the words that were said."""
    import brain
    said = step.get("say")
    if said and not step.get("tool"):
        wait = re.fullmatch(r"tunggu (\d{1,2})(?: detik)?", said.lower())
        if wait:
            time.sleep(min(int(wait[1]), MAX_WAIT))
            return None
        return brain.run_phrase(said)
    tool, args = step.get("tool"), step.get("args", {}) or {}
    if tool == "wait":
        time.sleep(min(float(args.get("seconds", 1)), MAX_WAIT))
        return None
    fn = brain.TOOL_MAP.get(tool)
    if not fn or tool in ("run_protocol", "create_protocol", "delete_protocol"):
        return f"langkah '{tool}' dilewati, tidak dikenal"
    if tool in LAMP_TOOLS and not smarthome.has_lamps():
        return None                          # no lamp connected: the step is simply not there
    return str(fn(**args))


ROUTINE_SAID_FROM = 3


def run_protocol(name: str) -> str:
    """Run a saved multi-step routine, the user's own or a built-in one: 'kerja', 'santai', 'fokus' (Pomodoro),
    'tidur', 'bersih' (Clean Slate), 'pesta' (House Party). See list_protocols for all."""
    import brain
    import habits
    import safety
    protocols = load()
    key = _resolve(name, protocols)
    if not key:
        return f"Rutinitas '{name}' tidak ditemukan, {T}. Yang ada: {', '.join(protocols)}."
    failed = []
    if len(protocols[key].get("steps", [])) >= ROUTINE_SAID_FROM and brain.speaking_aloud():     # (9 s of silence while "mode gabut" opened its apps)
        import threading
        import tts
        threading.Thread(target=tts.speak_filler, args=(f"Menjalankan rutinitas {key}.",), daemon=True, name="routine-said").start()   # (speaking waits for the sentence: the apps don't)
    with _quietly():
        for step in protocols[key].get("steps", []):
            calls = len(brain._calls_this_turn)
            try:
                result = _run_step(step)
            except Exception as e:
                if type(e).__name__ == "Cancelled":
                    raise
                result = f"gagal: {e}"
            if result and habits.FAILED_RESULT.search(result):
                failed.append(step_words(step))
            ran = brain._calls_this_turn[calls:]
            if ran and ran[-1][0] in PAUSE_AFTER:
                time.sleep(PAUSE_AFTER[ran[-1][0]])
    if safety.has_pending():                 # a step that needs "ya" is asked at the end, when the rest has run
        return safety.question()
    trouble = f" Yang tidak berhasil: {', '.join(failed)}." if failed else ""
    return f"Rutinitas {key} sudah jalan, {T}.{trouble}"


_STEP_WORDS = {"open_application": "buka {name}", "open_website": "buka {site}", "set_volume": "volume {level}", "set_brightness": "kecerahan {level}",
               "play_youtube": "putar {query}", "lamp_scene": "lampu {scene}", "set_timer": "timer {minutes} menit", "sleep_pc": "tidurkan laptop",
               "system_cleanup": "bersihkan file sementara", "wait": "tunggu {seconds} detik"}


def step_words(step: dict) -> str:
    """A step the way one would say it."""
    if step.get("say"):
        return step["say"]
    tool, args = step.get("tool", ""), step.get("args", {}) or {}
    if tool == "lamp_power":
        return "nyalakan lampu" if args.get("on") else "matikan lampu"
    if tool == "media_control":
        return {"pause": "jeda musik", "play": "lanjutkan musik", "next": "lagu berikutnya"}.get(args.get("action"), "kontrol musik")
    if tool == "window_action":
        return {"minimize_all": "sembunyikan semua jendela"}.get(args.get("action"), "atur jendela")
    try:
        return _STEP_WORDS[tool].format(**args)
    except (KeyError, IndexError):
        return tool.replace("_", " ")


def for_hud() -> list[dict]:
    """The routines for the screen: the user's own first."""
    rows = [{"key": key, "custom": bool(p.get("custom")), "steps": [step_words(s) for s in p.get("steps", [])]} for key, p in load().items()]
    return sorted(rows, key=lambda r: not r["custom"])


def for_hud_one(key: str) -> list[str]:
    return [step_words(s) for s in load().get(key, {}).get("steps", [])]


def list_protocols() -> str:
    """List all available protocols and what they do."""
    protocols = load()
    return "Protokol tersedia: " + "; ".join(f"{k} ({p.get('description', '')})" for k, p in protocols.items())


def create_protocol(name: str, steps_json: str, description: str = "") -> str:
    """Create or replace a custom protocol. steps_json = JSON array of steps, each
    {"tool": "<tool name>", "args": {...}} using the same tools you can call, plus
    {"tool": "wait", "args": {"seconds": 2}}. Example:
    [{"tool":"open_application","args":{"name":"spotify"}},{"tool":"set_volume","args":{"level":40}}]"""
    import brain
    try:
        steps = json.loads(steps_json)
        assert isinstance(steps, list) and steps
    except (ValueError, AssertionError):
        return "Format langkah protokol tidak valid (harus JSON array)."
    for step in steps:
        tool = step.get("tool") if isinstance(step, dict) else None
        if tool != "wait" and tool not in brain.TOOL_MAP:
            return f"Tool '{tool}' tidak dikenal, protokol tidak disimpan."
        if not isinstance(step.get("args", {}), dict):
            return f"Argumen untuk '{tool}' harus berupa objek."
    protocols = load()
    key = name.lower().replace("protokol", "").replace("protocol", "").strip()
    protocols[key] = {"aliases": protocols.get(key, {}).get("aliases", []),
                      "description": description, "steps": steps}
    save(protocols)
    return f"Protokol {key} disimpan dengan {len(steps)} langkah, {T}."


def delete_protocol(name: str) -> str:
    """Delete a custom protocol."""
    protocols = load()
    key = _resolve(name, protocols)
    if not key:
        return f"Protokol '{name}' tidak ditemukan, {T}."
    del protocols[key]
    save(protocols)
    return f"Protokol {key} dihapus, {T}."


AI_TOOLS = [run_protocol, list_protocols, create_protocol, delete_protocol]
