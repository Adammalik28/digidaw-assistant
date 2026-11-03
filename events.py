"""
Runtime event bus + shared state.

Subsystems (brain, TTS, media, monitor, timers) emit events here and the HUD streams them to
the browser. Keeps modules decoupled: nothing imports the HUD directly.
"""
import time
import queue
import threading
from collections import deque

state = {
    "status": "idle",         # idle | listening | thinking | speaking
    "voice_enabled": False,
    "dictation": False,
    "started": time.time(),
}

# Recent conversation / activity, replayed to a HUD window when it (re)connects
history: deque = deque(maxlen=80)
HISTORY_TYPES = {"user", "digidaw", "tool", "alert"}

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()
_command = threading.local()


def working_on(command_id):
    """The calling thread now handles this command: its replies carry the id (None = not a command any more)."""
    _command.id = command_id


def emit(event_type: str, **data) -> dict:
    event = {"type": event_type, "ts": time.time(), **data}
    command_id = getattr(_command, "id", None)
    if command_id is not None and event_type == "digidaw":
        event.setdefault("cmd", command_id)
    if event_type in HISTORY_TYPES:
        history.append(event)
    with _lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass
    return event


def set_status(status: str):
    if state["status"] != status:
        state["status"] = status
        emit("status", status=status)


def set_dictation(active: bool):
    state["dictation"] = active
    emit("dictation", active=active)


def subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=500)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue):
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def has_subscribers() -> bool:
    return bool(_subscribers)
