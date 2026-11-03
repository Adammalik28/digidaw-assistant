"""
Digidaw's log file (logs/digidaw.log, when it runs without a console): every line starts with the time, and the text
of messages read out loud (notifications.PRIVATE_TEXTS) is replaced by "‹isi pesan›" — the HUD still shows it.
"""
import sys
import time


class TimestampedLog:
    """The log file: every line starts with the time (most lines had none, so nothing could be traced back), and
    the text of messages read out loud is left out (notifications.PRIVATE_TEXTS)."""

    def __init__(self, f):
        self._f, self._line_start = f, True

    def write(self, text: str) -> int:
        if not text:
            return 0
        notes = sys.modules.get("notifications")
        for private in (list(getattr(notes, "PRIVATE_TEXTS", ())) if notes else ()):
            if private in text:
                text = text.replace(private, "‹isi pesan›")
        pieces = []
        for chunk in text.splitlines(keepends=True):
            if self._line_start and chunk.strip():
                pieces.append(time.strftime("%H:%M:%S "))
            pieces.append(chunk)
            self._line_start = chunk.endswith("\n")
        self._f.write("".join(pieces))
        return len(text)

    def __getattr__(self, name):
        return getattr(self._f, name)
