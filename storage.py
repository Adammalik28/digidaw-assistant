"""
Saving Digidaw's data so it survives a crash or a power cut half-way through a write.

A file is never overwritten in place: the new content goes to a temporary file next to it, is flushed to the disk,
and then renamed over the old one (a rename is all-or-nothing). Important files (alarms, agenda, memory, settings,
protocols, habits) also keep the previous good copy as <name>.bak, and load_json() falls back to it when the file
itself is damaged. Personal data is written readable for this user only (0600).

Deliberately small and dependency-free: config.py uses it too.
"""
import json
import os
import shutil
from pathlib import Path

PRIVATE = 0o600


def write_atomic(path, text: str, backup: bool = False, mode: int | None = None):
    """Write text to path all-or-nothing. backup: keep the previous version as <name>.bak. mode: file permissions
    (None = the old file's, else the usual default)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    if mode is None and path.exists():
        mode = path.stat().st_mode & 0o777
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    if backup and path.exists():
        try:
            if _readable_json(path):                   # never replace a good .bak with a damaged file
                shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)


def save_json(path, data, backup: bool = False, mode: int | None = None, **dumps):
    """json.dumps(data, **dumps) written with write_atomic()."""
    dumps.setdefault("ensure_ascii", False)
    write_atomic(path, json.dumps(data, **dumps), backup=backup, mode=mode)


def load_json(path, default=None):
    """The file's JSON; a damaged or missing file falls back to its .bak copy, then to `default`."""
    path = Path(path)
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return default


def _readable_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False
