"""
The user's own alarm sounds: audio files added in the Control Panel (Alarm), kept in alarm_sounds/ next to Digidaw. One is
chosen with config.ALARM_SOUND ("" = the built-in sound). A chosen file that went missing or can't be played falls back
to the built-in sound: an alarm that stays silent is the one thing that must not happen.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import config
import oscompat
import storage

FOLDER = Path(config.BASE_DIR) / "alarm_sounds"
EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".webm", ".mp4"}
MAX_BYTES = 40 * 1024 * 1024
PREVIEW_SECONDS = 30
MAX_SONG_SECONDS = 900
DOWNLOAD_SECONDS = 240

_preview = {"proc": None, "timer": None}
_lock = threading.Lock()


def _probe(path: Path) -> tuple[bool, float]:
    """(does it hold audio, how long). Without ffprobe it can't be told: the file is taken as it is."""
    if not shutil.which("ffprobe"):
        return True, 0.0
    try:
        done = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                               str(path)], capture_output=True, text=True, timeout=15)
        info = json.loads(done.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    try:
        return has_audio, float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        return has_audio, 0.0


def _path(sound_id: str) -> Path | None:
    """The file of an id from listing(), and only a file directly in the folder."""
    name = str(sound_id or "")
    path = FOLDER / name
    if not name or Path(name).name != name or path.suffix.lower() not in EXTENSIONS or not path.is_file():
        return None
    return path


def exists(sound_id: str) -> bool:
    return _path(sound_id) is not None


def _name(path: Path) -> str:
    return path.stem


def listing() -> list[dict]:
    if not FOLDER.is_dir():
        return []
    sounds = []
    for path in sorted(FOLDER.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() in EXTENSIONS and not path.name.startswith("."):
            sounds.append({"id": path.name, "name": _name(path), "seconds": round(_probe(path)[1]), "size": path.stat().st_size})
    return sounds


def _clean_filename(filename: str) -> str:
    name = Path(str(filename).replace("\\", "/")).name
    stem = re.sub(r"[^\w .()\-]", "", Path(name).stem, flags=re.UNICODE).strip(" .")[:70]
    return f"{stem or 'alarm'}{Path(name).suffix.lower()}"


def add(filename: str, data: bytes) -> dict:
    """Keep an uploaded file. ValueError (its text is shown to the user) when it can't be used as a sound."""
    name = _clean_filename(filename)
    if Path(name).suffix not in EXTENSIONS:
        raise ValueError(f"Format belum didukung. Pakai salah satu: {', '.join(sorted(e[1:].upper() for e in EXTENSIONS))}.")
    if not data:
        raise ValueError("File audionya kosong.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"File terlalu besar. Batasnya {MAX_BYTES // 1024 // 1024} MB.")
    FOLDER.mkdir(parents=True, exist_ok=True)
    part = FOLDER / f".upload-{time.time_ns()}{Path(name).suffix}"
    try:
        part.write_bytes(data)
        part.chmod(storage.PRIVATE)
        has_audio, _ = _probe(part)
        if not has_audio:
            raise ValueError("File ini tidak berisi audio yang bisa diputar.")
        target = FOLDER / name
        number = 2
        while target.exists():
            if target.read_bytes() == data:      # the same file again: nothing new
                part.unlink()
                return next(s for s in listing() if s["id"] == target.name)
            target = FOLDER / f"{Path(name).stem} ({number}){Path(name).suffix}"
            number += 1
        part.rename(target)
    finally:
        part.unlink(missing_ok=True)
    return next(s for s in listing() if s["id"] == target.name)


def remove(sound_id: str) -> bool:
    path = _path(sound_id)
    if not path:
        return False
    stop_preview()
    path.unlink()
    if config.ALARM_SOUND == sound_id:
        import settings
        settings.save({"ALARM_SOUND": ""})
    return True


def custom_file(kind: str = "alarm") -> str | None:
    """The user's sound for this kind of ring, or None for the built-in one."""
    if kind != "alarm" and not config.ALARM_SOUND_FOR_ALL:
        return None
    path = _path(config.ALARM_SOUND)
    if not path or (oscompat.IS_WINDOWS and path.suffix.lower() != ".wav"):    # (winsound only plays WAV)
        return None
    return str(path)


def sound_name(kind: str = "alarm") -> str:
    custom = custom_file(kind)
    return _name(Path(custom)) if custom else ""


def _clean_title(title: str) -> str:
    """"sound effect absurd🗿 part2 |#NAMA" -> "sound effect absurd part2": a name to say and to see in the list."""
    title = re.sub(r"\s*[|｜]\s*.*$|#\S+", " ", title)
    title = re.sub(r"[^\w\s.,'&()\-]", " ", title, flags=re.UNICODE)
    title = re.sub(r"\s*[(\[][^)\]]*(?:official|video|audio|lyric|lirik|mv|hd)[^)\]]*[)\]]\s*", " ", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip(" -,.")[:70] or "Lagu alarm"


def _song_playing() -> tuple[str, str] | None:
    """(address, title) of what is playing: the page in Digidaw's player; else what the desktop says is playing, found by its title
    (another browser, the player not answering for a moment: "belum ada lagu" twice while Arctic Monkeys played); else what
    Digidaw itself started a little while ago."""
    import media
    target = media._player_target() if media._alive() else None
    url = (target or {}).get("url", "")
    if "youtube.com/watch" in url:
        title = re.sub(r"^\(\d+\)\s*|\s*-\s*YouTube$", "", (target or {}).get("title", "")).strip()
        return re.sub(r"&.*$", "", url), _clean_title(title)
    now = media.snapshot()
    if now.get("title") and now.get("playing") and not media.CHAT_APPS.search(f"{now['title']} {now.get('app', '')}"):
        found = _song_called(f"{now.get('artist', '')} {now['title']}".strip())
        if found:
            return found
    return media.last_video()


def _song_called(title: str) -> tuple[str, str] | None:
    import media
    try:
        results = media._search_youtube(title)
    except Exception as e:
        print(f"[alarm] pencarian lagu gagal: {e}")
        return None
    if not results:
        return None
    video = media._pick_song(title, results)
    return f"https://www.youtube.com/watch?v={video['id']}", _clean_title(video["title"] or title)


def _tell(text: str):
    import alarms
    print(f"[alarm] {text}")
    if alarms.ANNOUNCE:
        alarms.ANNOUNCE(text, "reminder")


def _fetch_song(url: str, title: str):
    """Download the audio and make it the alarm. In the background: it takes a while, and nothing waits for it."""
    import settings
    folder = tempfile.mkdtemp(prefix="digidaw-alarm-")
    T = config.USER_TITLE
    try:
        subprocess.run(["yt-dlp", "--no-playlist", "--no-warnings", "-x", "--audio-format", "mp3", "--audio-quality", "5",
                        "--match-filter", f"duration<={MAX_SONG_SECONDS}", "--max-filesize", str(MAX_BYTES),
                        "-o", os.path.join(folder, "song.%(ext)s"), url], capture_output=True, timeout=DOWNLOAD_SECONDS)
        song = Path(folder) / "song.mp3"
        if not song.is_file():
            _tell(f"{title} tidak bisa dijadikan alarm, {T}. Mungkin videonya terlalu panjang atau internetnya bermasalah.")
            return
        added = add(f"{title[:60]}.mp3", song.read_bytes())
        settings.save({"ALARM_SOUND": added["id"]})
        _tell(f"{added['name']} sudah jadi suara alarm{' dan timer' if config.ALARM_SOUND_FOR_ALL else ''}, {T}.")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"[alarm] lagu gagal diambil: {e}")
        _tell(f"{title} tidak bisa dijadikan alarm, {T}.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def song_as_alarm(title: str = "") -> str:
    """Make a song the alarm sound: the one playing now ("jadikan lagu ini alarm"), or by its title ("jadikan Be My Baby alarm").
    It takes a moment; Digidaw says when it's ready. title: empty = what is playing now."""
    T = config.USER_TITLE
    if not (shutil.which("yt-dlp") and shutil.which("ffmpeg")):
        return f"Untuk mengambil lagu perlu yt-dlp dan ffmpeg, {T}. Atau tambahkan file audionya di Control Panel."
    song = _song_called(title) if title.strip() else _song_playing()
    if not song:
        return (f"Lagu {title} tidak saya temukan, {T}." if title.strip()
                else f"Belum ada lagu yang saya putar, {T}. Putar dulu, atau sebutkan judulnya.")
    threading.Thread(target=_fetch_song, args=song, daemon=True, name="song-as-alarm").start()
    return f"Baik, {T}. {song[1]} saya siapkan jadi suara alarm, saya kabari kalau sudah."


def preview(sound_id: str = "") -> bool:
    """Play a sound once so the user can hear it. "" = the built-in alarm sound."""
    if oscompat.IS_WINDOWS:
        return False
    import linux_desktop as ld
    path = _path(sound_id)
    file = str(path) if path else ld.sound_for("alarm")
    if sound_id and not path:
        return False
    stop_preview()
    proc = ld.play_audio(file) if file else None
    if not proc:
        return False
    with _lock:
        _preview["proc"] = proc
        _preview["timer"] = threading.Timer(PREVIEW_SECONDS, stop_preview)
        _preview["timer"].daemon = True
        _preview["timer"].start()
    return True


def stop_preview():
    with _lock:
        proc, timer = _preview["proc"], _preview["timer"]
        _preview["proc"] = _preview["timer"] = None
    if timer:
        timer.cancel()
    if proc and proc.poll() is None:
        proc.kill()
