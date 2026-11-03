"""
Interrupting Digidaw by voice while it talks (config.BARGE_IN): "Digidaw, stop" or "Digidaw, <another command>".

Normally the microphone is shut while Digidaw speaks, or it would hear its own voice and answer itself. With
PipeWire's echo cancellation (WebRTC) Digidaw's own voice is taken out of what the microphone hears, so it can keep
listening while it talks. Digidaw loads the echo-cancel module itself (a virtual microphone "Digidaw AEC mic" and a
virtual output "Digidaw AEC voice") and moves only its own voice and its own microphone stream onto them: the
system's default devices and every other app's audio stay exactly as they were. The module is unloaded again when
Digidaw stops (a copy left by a crash is reused at the next start).

Linux with PipeWire (pactl) only; anywhere else barge-in simply isn't available and the microphone is shut while
Digidaw speaks, as before.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import config

SOURCE, SINK = "digidaw_aec_source", "digidaw_aec_sink"
# only the echo is removed: WebRTC's noise suppression and automatic gain also reshape the user's voice, and the
# recognizers (Whisper tiny/base, Google) heard "Digidaw" far less often through them
AEC_ARGS = "webrtc.noise_suppression=false webrtc.gain_control=false webrtc.high_pass_filter=true"
WATCH_EVERY = 30                     # seconds: a reopened microphone stream is moved again, a lost module reloaded

_module = None
_ready = threading.Event()           # Digidaw's microphone and voice run through the echo canceller
_stopping = threading.Event()
_started = False


def active() -> bool:
    """Is barge-in possible right now (echo cancellation running for Digidaw's mic and voice)?"""
    return _ready.is_set() and config.BARGE_IN


def _pactl(*args, as_json: bool = False, timeout: float = 5):
    cmd = ["pactl", *(["-f", "json"] if as_json else []), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"pactl {args[0]} gagal")
    return json.loads(r.stdout or "[]") if as_json else r.stdout.strip()


def _find_module() -> str | None:
    """The index of Digidaw's echo-cancel module, if it is loaded. (PipeWire's JSON list of modules has no index,
    so the short text list is read: "<index>\t<name>\t<arguments>".)"""
    for line in _pactl("list", "short", "modules").splitlines():
        index, _, rest = line.partition("\t")
        name, _, argument = rest.partition("\t")
        if name == "module-echo-cancel" and f"source_name={SOURCE}" in argument:
            return index.strip()
    return None


def _current_module() -> str | None:
    """Digidaw's echo-cancel module with today's settings; one left from an older version (noise suppression on)
    is unloaded so it can be loaded again the gentler way."""
    found = _find_module()
    if found and "noise_suppression=false" not in _module_args(found):
        _pactl("unload-module", found, timeout=3)
        return None
    return found


def _module_args(index: str) -> str:
    for line in _pactl("list", "short", "modules").splitlines():
        if line.split("\t", 1)[0].strip() == index:
            return line
    return ""


def unload_leftover():
    """Barge-in is off: an echo-cancel module left loaded by an earlier run (or a crash) is removed, so Digidaw's
    microphone stream can't end up on it again (PipeWire remembers where a stream was moved to)."""
    if not shutil.which("pactl"):
        return
    try:
        found = _find_module()
        if found:
            _pactl("unload-module", found, timeout=3)
            print("[aec] peredam gema dilepas (menyela dengan suara tidak aktif): mikrofon kembali langsung")
    except Exception:
        pass


def _load() -> str:
    found = _current_module()
    if found:
        return found
    return _pactl("load-module", "module-echo-cancel", "aec_method=webrtc", f'aec_args="{AEC_ARGS}"',
                  f"source_name={SOURCE}", f"sink_name={SINK}", 'source_properties=node.description="Digidaw AEC mic"',
                  'sink_properties=node.description="Digidaw AEC voice"')


def _id(value) -> str:
    """PipeWire writes the same index as a number in one list and as text in another ("client": "7138")."""
    return "" if value is None else str(value).strip()


def _index(kind: str, name: str) -> str:
    for item in _pactl("list", kind, as_json=True):
        if item.get("name") == name:
            return _id(item.get("index"))
    return ""


def _mine() -> set[str]:
    """The PipeWire clients of this process (its microphone and its voice)."""
    pid = str(os.getpid())
    return {_id(c.get("index")) for c in _pactl("list", "clients", as_json=True)
            if _id((c.get("properties") or {}).get("application.process.id")) == pid}


def _move() -> tuple[bool, bool]:
    """Put Digidaw's own microphone and voice streams on the echo canceller. Returns (mic, voice) now there."""
    clients = _mine()
    source, sink = _index("sources", SOURCE), _index("sinks", SINK)
    mic = voice = False
    for o in _pactl("list", "source-outputs", as_json=True):
        if _id(o.get("client")) in clients and source:
            if _id(o.get("source")) != source:
                _pactl("move-source-output", _id(o["index"]), SOURCE)
            mic = True
    for o in _pactl("list", "sink-inputs", as_json=True):
        if _id(o.get("client")) in clients and sink:
            if _id(o.get("sink")) != sink:
                _pactl("move-sink-input", _id(o["index"]), SINK)
            voice = True
    return mic, voice


def _run():
    global _module
    deadline = time.time() + 60          # the microphone stream opens a moment after Digidaw starts
    told = ""                            # each problem is written to the log once, not every few seconds
    while not _stopping.is_set():
        try:
            if not config.BARGE_IN:
                _ready.clear()
            else:
                if _module is None or _module != _find_module():
                    _module = _load()
                mic, voice = _move()
                if mic and voice:
                    if not _ready.is_set():
                        print("[aec] peredam gema aktif: Digidaw bisa disela saat bicara (\"Digidaw, stop\")")
                    _ready.set()
                    told = ""
                else:
                    _ready.clear()
                    if time.time() > deadline and told != "streams":
                        told = "streams"
                        print("[aec] aliran mikrofon/suara Digidaw belum ditemukan; menyela dengan suara belum aktif")
        except Exception as e:
            _ready.clear()
            problem = str(e)[:120]
            if problem != told:
                told = problem
                print(f"[aec] peredam gema belum bisa dipakai: {problem} (dicoba lagi)")
        _stopping.wait(5 if not _ready.is_set() and time.time() < deadline else WATCH_EVERY)


def start():
    """After the microphone has started: echo cancellation for Digidaw's own mic and voice."""
    global _started
    if not config.BARGE_IN:
        threading.Thread(target=unload_leftover, daemon=True, name="aec-off").start()
        return
    if _started or not shutil.which("pactl"):
        return
    _started = True
    threading.Thread(target=_run, daemon=True, name="aec").start()


def stop():
    """Digidaw is stopping: remove the virtual devices again."""
    global _module
    _stopping.set()
    _ready.clear()
    if _module is not None:
        try:
            _pactl("unload-module", _module, timeout=3)
        except Exception:
            pass
        _module = None
