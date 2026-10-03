"""
Small cross-platform helpers so the rest of Digidaw can stay the same on Windows and Linux.

Windows keeps using exactly what it used before (os.startfile, explorer, winsound, ImageGrab, taskkill);
Linux uses the freedesktop / Wayland tools wrapped in linux_desktop.py.
"""
import os
import sys
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# creationflags=0 is accepted on every OS, so `creationflags=NO_WINDOW` works everywhere
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def detached() -> dict:
    """Popen keyword arguments for a program that must outlive Digidaw and never block it."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, "close_fds": True}
    return {"start_new_session": True, "close_fds": True,
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def open_path(path) -> None:
    """Open a file, folder or URL with its default application."""
    if IS_WINDOWS:
        os.startfile(str(path))
        return
    subprocess.Popen(["xdg-open", str(path)], **detached())


def open_folder(path) -> None:
    """Show a folder in the file manager (File Explorer on Windows)."""
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", str(path)])
        return
    import linux_desktop
    linux_desktop.open_in_file_manager(str(path))


def reveal(path) -> None:
    """Show a file selected in its folder."""
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return
    import linux_desktop
    linux_desktop.reveal_in_file_manager(str(path))


def open_text_file(path) -> None:
    """Open a text file in a simple editor (Notepad on Windows)."""
    if IS_WINDOWS:
        subprocess.Popen(["notepad.exe", str(path)])
        return
    import linux_desktop
    linux_desktop.open_text_editor(str(path))


TEXT_EDITOR_NAME = "Notepad" if IS_WINDOWS else "editor teks"
FILE_MANAGER_NAME = "File Explorer" if IS_WINDOWS else "file manager"
TRASH_NAME = "Recycle Bin" if IS_WINDOWS else "Tempat Sampah"


def kill_tree(pid: int) -> None:
    """Stop a process and everything it started (a coding agent, a project's terminal)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
        return
    import psutil
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass


def beep(tones: list[tuple[int, int]]) -> None:
    """Play short tones [(frequency Hz, duration ms), ...] (blocking)."""
    if IS_WINDOWS:
        import winsound
        for freq, ms in tones:
            winsound.Beep(freq, ms)
        return
    import linux_desktop
    linux_desktop.play_tones(tones)


def grab_screen():
    """Screenshot of all monitors as a PIL image."""
    if IS_WINDOWS:
        from PIL import ImageGrab
        return ImageGrab.grab(all_screens=True)
    import linux_desktop
    return linux_desktop.grab_screen()


def system_disk() -> str:
    """Root of the system drive (for free-space checks)."""
    return os.environ.get("SYSTEMDRIVE", "C:") + "\\" if IS_WINDOWS else "/"


SYSTEM_DISK_NAME = "disk C" if IS_WINDOWS else "disk sistem"


def notify_error(title: str, text: str) -> None:
    """Show an error to the user when Digidaw runs without a console."""
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
        return
    try:
        subprocess.run(["notify-send", "-u", "critical", "-a", title, title, text], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def venv_pip_hint(package: str) -> str:
    if IS_WINDOWS:
        return f".venv\\Scripts\\pip install {package}"
    return f"./setup_digidaw.sh (atau: uv pip install {package})"


def home_path(*parts) -> Path:
    return Path.home().joinpath(*parts)
