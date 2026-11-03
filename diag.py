"""
Failures that used to pass in silence (a hook that broke, an announcement that never came): written to the log once
per run and kind, so a part that stopped working shows up there instead of just quietly not working, and the log
doesn't fill up when it keeps failing.
"""
import threading

_told: set = set()
_lock = threading.Lock()


def warn_once(key: str, what: str, error: BaseException | None = None):
    """[peringatan] <what>: <error> — the first time this key fails in this run."""
    with _lock:
        if key in _told:
            return
        _told.add(key)
    detail = f": {type(error).__name__}: {str(error)[:160]}" if error is not None else ""
    print(f"[peringatan] {what}{detail} (dicatat sekali)")
