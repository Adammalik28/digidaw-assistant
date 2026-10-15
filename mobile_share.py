"""
Sharing between the phone and the PC (phone HUD): links, text, the clipboard and files.

- A link from the phone opens on the PC (a YouTube link plays in Digidaw's player).
- Text goes to the PC's clipboard, or is typed into the active window.
- Files / photos from the phone are saved in ~/Downloads/Digidaw HP.
- The phone can take the PC's clipboard, and download recent files from the PC — only from Downloads and
  Pictures/Screenshots (never any other folder), and the PC shows a notification when it happens.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import config
import events

T = config.USER_TITLE
MAX_UPLOAD = 300 * 1024 * 1024           # one file from the phone (a few minutes of video)
CHUNK = 256 * 1024


def _user_dir(kind: str, fallback: str) -> Path:
    try:
        out = subprocess.run(["xdg-user-dir", kind], capture_output=True, text=True, timeout=3).stdout.strip()
        if out and Path(out).is_dir() and Path(out) != Path.home():
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / fallback


def _folders() -> dict[str, Path]:
    """The only folders the phone may list / download from (key -> path)."""
    downloads, pictures = _user_dir("DOWNLOAD", "Downloads"), _user_dir("PICTURES", "Pictures")
    folders = {"unduhan": downloads, "dari-hp": downloads / "Digidaw HP", "screenshot": pictures / "Screenshots"}
    return {k: p for k, p in folders.items() if p.is_dir()}


def inbox() -> Path:
    path = _user_dir("DOWNLOAD", "Downloads") / "Digidaw HP"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tell_pc(text: str):
    """A desktop notification + the HUD, so nothing happens on the PC unseen."""
    try:
        import linux_desktop
        linux_desktop.notify("Digidaw · HP", text)
    except Exception:
        pass
    events.emit("toast", text=text, level="info")
    print(f"[HUD HP] {text}")


# ───────────────────────────── links & text ─────────────────────────────

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def first_url(text: str) -> str:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,);]") if m else ""


def open_link(url: str) -> str:
    import media
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return f"Link tidak valid, {T}."
    if media.youtube_id(url):
        _tell_pc("Memutar video YouTube dari HP.")
        return media.play_url(url)
    import system_control
    _tell_pc(f"Membuka link dari HP: {url[:80]}")
    return system_control.open_website(url)


def set_clipboard(text: str) -> str:
    if not text:
        return "Teksnya kosong."
    if not shutil.which("wl-copy"):
        return "wl-copy belum terpasang (paket wl-clipboard)."
    try:
        # wl-copy forks a helper that keeps serving the clipboard; waiting for the parent means the PC's
        # clipboard really holds the text when this returns (an immediate "ambil clipboard" sees it)
        subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=text.encode("utf-8"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Clipboard PC tidak bisa diisi ({e})."
    _tell_pc(f"Teks dari HP disalin ke clipboard ({len(text)} karakter).")
    return "Tersalin di clipboard PC. Tinggal Ctrl+V."


def get_clipboard() -> str:
    if not shutil.which("wl-paste"):
        return ""
    try:
        out = subprocess.run(["wl-paste", "--no-newline", "--type", "text"], capture_output=True, timeout=3)
        text = out.stdout.decode("utf-8", errors="replace")[:20000] if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    if text:
        _tell_pc("HP mengambil isi clipboard PC.")
    return text


def type_text(text: str) -> str:
    import system_control
    return system_control.type_text(text[:5000])


# ───────────────────────────── files ─────────────────────────────

def safe_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name                # never a path
    name = re.sub(r"[\x00-\x1f/]", "", name).strip(" .") or "file"
    return name[:120]


def _unique(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def save_upload(stream, length: int, name: str) -> Path:
    """Write a file the phone sends (streamed, never all in memory). Raises ValueError on bad size."""
    if not 0 < length <= MAX_UPLOAD:
        raise ValueError(f"ukuran file harus di bawah {MAX_UPLOAD // 2**20} MB")
    target = _unique(inbox(), safe_name(name))
    partial = target.with_name(target.name + ".part")
    left = length
    try:
        with open(partial, "wb") as f:
            while left > 0:
                chunk = stream.read(min(CHUNK, left))
                if not chunk:
                    raise ValueError("kiriman terputus")
                f.write(chunk)
                left -= len(chunk)
        partial.rename(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def received(paths: list[Path]):
    if len(paths) == 1:
        _tell_pc(f"{paths[0].name} dari HP disimpan di {paths[0].parent.name}.")
    elif paths:
        _tell_pc(f"{len(paths)} file dari HP disimpan di {paths[0].parent.name}.")


def open_received(name: str) -> str:
    path = inbox() / safe_name(name)
    if not path.is_file():
        return f"File itu tidak ada, {T}."
    import linux_desktop
    linux_desktop.spawn(["xdg-open", str(path)])
    return f"Membuka {path.name} di PC."


def recent_files(limit: int = 20) -> list[dict]:
    files = []
    for key, folder in _folders().items():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name.endswith((".part", ".crdownload", ".tmp")):
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            files.append({"folder": key, "name": e.name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files[:limit]


def file_path(folder: str, name: str) -> Path | None:
    """A file the phone may download: directly inside one of the allowed folders, no links, no tricks."""
    base = _folders().get(folder)
    if not base or name != safe_name(name):
        return None
    path = base / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != base.resolve():
        return None
    return path


_last_download_note: dict[str, float] = {}


def downloaded(path: Path):
    if time.time() - _last_download_note.get(path.name, 0) > 60:
        _last_download_note[path.name] = time.time()
        _tell_pc(f"HP mengunduh {path.name} dari PC.")
