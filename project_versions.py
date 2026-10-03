"""
Local version history for projects Digidaw builds (no git / GitHub needed).

Every time the coding agent finishes a build or a change, the project's files are saved as a version
in <project>/.digidaw/versions. File contents are stored once by their hash, so a version only costs
the files that changed. Dependencies and build output (node_modules, venv, dist, ...) are skipped —
they are re-installed from package.json / requirements.txt when a version is restored.

"kembalikan project kalkulator ke versi sebelumnya" restores the previous version (after saving the
current state, so a restore can itself be undone); "apa saja yang diubah" compares the last two.
"""
import os
import json
import time
import shutil
import hashlib
import datetime
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".digidaw", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next",
             ".nuxt", ".cache", ".parcel-cache", "coverage", "target", ".turbo", ".svelte-kit", ".pytest_cache",
             ".idea", ".vscode", "out"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "BRIEF.md"}   # BRIEF.md is Digidaw's own log of requests
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_VERSIONS = 30
DEP_FILES = {"package.json", "package-lock.json", "requirements.txt", "pyproject.toml", "yarn.lock", "pnpm-lock.yaml"}


def _store(folder: Path) -> Path:
    return Path(folder) / ".digidaw" / "versions"


def _index_path(folder: Path) -> Path:
    return _store(folder) / "index.json"


def load(folder) -> list[dict]:
    try:
        return json.loads(_index_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_index(folder, versions: list[dict]):
    _index_path(folder).parent.mkdir(parents=True, exist_ok=True)
    tmp = _index_path(folder).with_suffix(".tmp")
    tmp.write_text(json.dumps(versions, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _index_path(folder))


def _walk(folder: Path):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    yield path
            except OSError:
                continue


def _hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _object(folder, digest: str) -> Path:
    return _store(folder) / "objects" / digest[:2] / digest[2:]


def current_files(folder) -> dict[str, str]:
    folder = Path(folder)
    return {path.relative_to(folder).as_posix(): _hash(path) for path in _walk(folder)}


def snapshot(folder, label: str, kind: str = "manual", summary: str = "", force: bool = False) -> dict | None:
    """Save the project's current files as a new version. Returns it, or None if nothing changed."""
    folder = Path(folder)
    files = current_files(folder)
    versions = load(folder)
    if versions and versions[-1]["files"] == files and not force:
        return None
    for rel, digest in files.items():
        obj = _object(folder, digest)
        if not obj.exists():
            obj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / rel, obj)
    number = versions[-1]["number"] + 1 if versions else 1
    version = {"number": number, "time": time.time(), "label": label[:200], "kind": kind,
               "summary": summary[:1500], "files": files}
    versions.append(version)
    if len(versions) > MAX_VERSIONS:
        versions = versions[-MAX_VERSIONS:]
        _save_index(folder, versions)
        _collect_garbage(folder, versions)
    else:
        _save_index(folder, versions)
    return version


def _collect_garbage(folder, versions: list[dict]):
    used = {d for v in versions for d in v["files"].values()}
    for obj in (_store(folder) / "objects").glob("*/*"):
        if obj.parent.name + obj.name not in used:
            obj.unlink(missing_ok=True)


def is_modified(folder) -> bool:
    """Do the files differ from the latest saved version (e.g. the user edited them by hand)?"""
    versions = load(folder)
    return bool(versions) and versions[-1]["files"] != current_files(folder)


def diff(old: dict, new: dict) -> dict:
    a, b = old["files"], new["files"]
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


def describe_diff(d: dict, limit: int = 6) -> str:
    parts = []
    for key, word in (("changed", "diubah"), ("added", "ditambah"), ("removed", "dihapus")):
        if d[key]:
            names = ", ".join(Path(p).name for p in d[key][:limit]) + (f", dan {len(d[key]) - limit} lainnya" if len(d[key]) > limit else "")
            parts.append(f"{len(d[key])} file {word} ({names})")
    return "; ".join(parts) or "tidak ada file yang berubah"


def restore(folder, number: int) -> tuple[dict, bool]:
    """Make the project's files exactly like version `number`. Returns (version, dependencies_changed).
    Files that didn't exist in that version are removed; skipped folders (node_modules, ...) are kept."""
    folder = Path(folder)
    versions = load(folder)
    target = next((v for v in versions if v["number"] == number), None)
    if not target:
        raise ValueError(f"versi {number} tidak ada")
    now = current_files(folder)
    for rel in set(now) - set(target["files"]):
        (folder / rel).unlink(missing_ok=True)
    for rel, digest in target["files"].items():
        if now.get(rel) == digest:
            continue
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_object(folder, digest), dest)
    for dirpath, dirnames, _ in os.walk(folder, topdown=False):      # tidy folders left empty
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        path = Path(dirpath)
        if path != folder and not any(path.iterdir()) and not (set(path.relative_to(folder).parts) & SKIP_DIRS):
            path.rmdir()
    deps = any(now.get(f) != target["files"].get(f) for f in DEP_FILES)
    return target, deps


def when(version: dict) -> str:
    t = datetime.datetime.fromtimestamp(version["time"])
    return t.strftime("%H:%M") if t.date() == datetime.date.today() else t.strftime("%d/%m %H:%M")
