"""
Files, for voice and for the AI: look into a folder, read a text file, move, copy, rename. Nothing is ever overwritten,
Digidaw's own data and the system's folders stay out of reach, and secret files (keys, passwords, .env) are never read:
what is read here can go to the online AI.
"""
import os
import re
import shutil
from pathlib import Path

import config
import linux_desktop as ld

T = config.USER_TITLE
MAX_LISTED = 25
MAX_READ = 6000                    # characters of a file given back
SECRET_RE = re.compile(r"(?:^|/)(?:\.env[^/]*|\.ssh|\.gnupg|\.password-store|\.mozilla|\.config/google-chrome|"
                       r"[^/]*(?:secret|password|kata.?sandi|credential|private.?key|\.pem|\.key|id_rsa|id_ed25519)[^/]*)(?:/|$)",
                       re.IGNORECASE)
SYSTEM_DIRS = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/var", "/sys", "/proc", "/dev", "/opt", "/root")
FOLDER_WORDS = {"unduhan": "downloads", "download": "downloads", "downloads": "downloads", "dokumen": "documents",
                "documents": "documents", "gambar": "pictures", "foto": "pictures", "pictures": "pictures",
                "musik": "music", "music": "music", "video": "videos", "videos": "videos", "desktop": "desktop",
                "desktop saya": "desktop"}


def _path(said: str) -> Path:
    """What the user said as a path: full, ~/…, a known folder ("unduhan", "folder dokumen"), or inside one."""
    said = said.strip().strip('"\'')
    key = re.sub(r"^folder\s+", "", said.lower())
    if key in FOLDER_WORDS:
        return ld.known_folder(FOLDER_WORDS[key])
    first, _, rest = said.partition("/")
    if first.lower() in FOLDER_WORDS and rest:
        return ld.known_folder(FOLDER_WORDS[first.lower()]) / rest
    path = Path(os.path.expandvars(os.path.expanduser(said)))
    return path if path.is_absolute() else Path.home() / path


def _refused(path: Path) -> str:
    """Why this path must not be touched, or ""."""
    real = os.path.realpath(path)
    base = os.path.realpath(config.BASE_DIR)
    if real == str(Path.home()) or real == "/" or real == base or real.startswith(base + "/"):
        return f"{path} milik sistem atau Digidaw sendiri"
    if any(real == d or real.startswith(d + "/") for d in SYSTEM_DIRS):
        return f"{path} folder sistem"
    return ""


def list_folder(path: str = "unduhan") -> str:
    """What is in a folder ("isi folder unduhan apa saja"): the newest files first."""
    folder = _path(path)
    if not folder.is_dir():
        return f"Folder {path} tidak ditemukan, {T}."
    if SECRET_RE.search(str(folder)):
        return f"Isi folder itu rahasia, jadi tidak saya buka, {T}."
    items = sorted((p for p in folder.iterdir() if not p.name.startswith(".")), key=lambda p: -p.stat().st_mtime)
    if not items:
        return f"Folder {folder.name} kosong, {T}."
    names = ", ".join(p.name + ("/" if p.is_dir() else "") for p in items[:MAX_LISTED])
    more = f", dan {len(items) - MAX_LISTED} lainnya" if len(items) > MAX_LISTED else ""
    return f"Di folder {folder.name} ada {len(items)} item, {T}: {names}{more}."


def read_text_file(path: str) -> str:
    """The text inside a file ("baca file catatan.txt di unduhan"). Not for secret files (keys, passwords, .env)."""
    file = _path(path)
    if SECRET_RE.search(str(file)):
        return f"File {file.name} berisi rahasia, jadi tidak saya baca, {T}."
    if not file.is_file():
        return f"File {path} tidak ditemukan, {T}. Pakai find_files dulu kalau lokasinya belum pasti."
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"{file.name} bukan file teks yang bisa saya baca, {T}."
    return text[:MAX_READ] + ("\n… (dipotong)" if len(text) > MAX_READ else "")


def _transfer(source: str, destination: str, keep: bool) -> str:
    src, dst = _path(source), _path(destination)
    verb = "disalin" if keep else "dipindahkan"
    if not src.exists():
        return f"{source} tidak ditemukan, {T}. Pakai find_files dulu kalau lokasinya belum pasti."
    for path in (src, dst):
        why = _refused(path)
        if why:
            return f"Demi keamanan tidak saya ubah: {why}, {T}."
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return f"Di tujuan sudah ada {dst.name}, jadi tidak saya timpa, {T}."
    dst.parent.mkdir(parents=True, exist_ok=True)
    if keep:
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    else:
        shutil.move(str(src), str(dst))
    return f"{src.name} sudah {verb} ke {dst.parent}, {T}."


def move_file(source: str, destination: str) -> str:
    """Move a file or folder ("pindahkan laporan.pdf dari unduhan ke dokumen"). Never overwrites."""
    return _transfer(source, destination, keep=False)


def copy_file(source: str, destination: str) -> str:
    """Copy a file or folder ("salin foto.jpg ke dokumen"). Never overwrites."""
    return _transfer(source, destination, keep=True)


def rename_file(path: str, new_name: str) -> str:
    """Rename a file or folder, in the same place ("ganti nama laporan.pdf jadi laporan akhir.pdf")."""
    src = _path(path)
    if not src.exists():
        return f"{path} tidak ditemukan, {T}."
    why = _refused(src)
    if why:
        return f"Demi keamanan tidak saya ubah: {why}, {T}."
    if "/" in new_name:
        return f"Nama baru tidak boleh memuat folder, {T}. Untuk memindahkan, bilang pindahkan."
    dst = src.with_name(new_name.strip())
    if dst.exists():
        return f"Sudah ada {dst.name} di situ, jadi tidak saya timpa, {T}."
    src.rename(dst)
    return f"{src.name} sekarang bernama {dst.name}, {T}."


AI_TOOLS = [list_folder, read_text_file, move_file, copy_file, rename_file]
