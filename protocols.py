"""
Protocols: named multi-step routines ("Digidaw, aktifkan protokol kerja", "House Party Protocol").

Each step calls one of DIGIDAW's skills by name. Protocols live in protocols.json (created
with defaults on first run) so they can be edited by hand or created by voice via create_protocol.
"""
import json
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


def run_protocol(name: str) -> str:
    """Run a saved multi-step protocol/routine, e.g. 'kerja', 'santai', 'fokus' (Pomodoro),
    'tidur', 'bersih' (Clean Slate), 'pesta' (House Party). See list_protocols for all."""
    import brain
    protocols = load()
    key = _resolve(name, protocols)
    if not key:
        return f"Protokol '{name}' tidak ditemukan. Tersedia: {', '.join(protocols)}."
    results = []
    for step in protocols[key].get("steps", []):
        tool, args = step.get("tool"), step.get("args", {}) or {}
        if tool == "wait":
            time.sleep(min(float(args.get("seconds", 1)), 30))
            continue
        fn = brain.TOOL_MAP.get(tool)
        if not fn or tool in ("run_protocol", "create_protocol", "delete_protocol"):
            results.append(f"langkah '{tool}' dilewati")
            continue
        if tool in LAMP_TOOLS and not smarthome.has_lamps():
            continue                         # no lamp connected: the step is simply not there
        try:
            results.append(str(fn(**args)))
        except Exception as e:
            results.append(f"{tool} gagal: {e}")
    return f"Protokol {key} dijalankan. " + " | ".join(results)


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
