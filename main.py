import os
import sys
import json
import time
import argparse
import importlib.util
import threading
import subprocess

# ── output: a console when there is one, otherwise (pythonw / desktop shortcut) a log file ──
BASE = os.path.dirname(os.path.abspath(__file__))
# Windows: started with pythonw. Linux: start_digidaw.sh (desktop entry / autostart) sets DIGIDAW_NO_CONSOLE=1.
NO_CONSOLE = sys.stdout is None or sys.stderr is None or os.environ.get("DIGIDAW_NO_CONSOLE") == "1"
if NO_CONSOLE:
    log_dir = os.path.join(BASE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "digidaw.log")
    if os.path.exists(log_path) and os.path.getsize(log_path) > 2_000_000:
        os.replace(log_path, log_path + ".old")
    sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)
    print(f"\n===== Digidaw start {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
else:
    # Configure UTF-8 encoding for standard Windows consoles
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

# a native crash (in a DLL) should leave a trace in the log instead of Digidaw silently disappearing
import faulthandler
faulthandler.enable(file=sys.stderr, all_threads=True)

# The winrt package ships an old MSVCP140.dll (14.29). If it loads first, Whisper's ctranslate2 (built
# with a newer compiler) crashes the whole process. Load Windows' own, newer runtime first so every
# module shares it.
if os.name == "nt":
    import ctypes
    for _dll in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        _path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", _dll)
        if os.path.exists(_path):
            try:
                ctypes.WinDLL(_path)
            except OSError:
                pass

import config
import hud_server
import oscompat


def _parse_args():
    parser = argparse.ArgumentParser(description="DIGIDAW Personal AI Assistant")
    parser.add_argument("--text", action="store_true", help="Text mode only (microphone off)")
    parser.add_argument("--voice", action="store_true", help="Voice + text mode (default)")
    parser.add_argument("--no-hud", action="store_true", help="Do not start the holographic HUD")
    parser.add_argument("--background", action="store_true", help="Start quietly: no greeting, HUD stays closed")
    parser.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _single_instance(args) -> bool:
    """If Digidaw already runs, just bring its HUD forward. Returns False when this copy should exit."""
    if args.no_hud:
        return True
    deadline = time.time() + (15 if args.restarted else 0)   # after a restart, wait for the old copy to exit
    while hud_server.running_elsewhere():
        if time.time() > deadline:
            if not args.background:
                hud_server.open_window()
            return False
        time.sleep(0.5)
    return True


ARGS = _parse_args()
if not _single_instance(ARGS):
    sys.exit(0)

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

import tts
if not oscompat.IS_WINDOWS:
    # `import tts` started pygame/SDL, which swallows SIGTERM. Until main() installs the real handler,
    # a stop request during start-up must still end the process, even while it is stuck inside C code
    # (an audio device that doesn't answer): the kernel's default action does that, a Python handler can't.
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
import stt
import brain
import memory
import media
import monitor
import hotkeys
import ai_skills
import system_control
import project_builder
import security_scan
import screen_context
import generator
import alarms
from core import DigidawCore, FILLERS

console = Console()

# Clean ASCII banner that works safely in all Windows codepages
BANNER = r"""
  ____  ___ ____ ___ ____    _    __        __
 |  _ \|_ _/ ___|_ _|  _ \  / \   \ \      / /
 | | | || | |  _ | || | | |/ _ \   \ \ /\ / /
 | |_| || | |_| || || |_| / ___ \   \ V  V /
 |____/|___\____|___|____/_/   \_\   \_/\_/
        SYSTEM INITIALIZATION COMPLETE
"""

# module -> pip package, checked at startup so missing libraries show up immediately
DEPENDENCIES = {
    "PIL": "Pillow", "pyperclip": "pyperclip", "edge_tts": "edge-tts", "gtts": "gTTS", "websockets": "websockets",
    "google.genai": "google-genai", "webrtcvad": "webrtcvad-wheels",
}
if oscompat.IS_WINDOWS:
    DEPENDENCIES.update({"pyautogui": "pyautogui", "pycaw": "pycaw", "win32gui": "pywin32",
                         "winrt.windows.media.control": "winrt-Windows.Media.Control"})
SETUP_SCRIPT = "setup_digidaw.bat" if oscompat.IS_WINDOWS else "setup_digidaw.sh"
# Linux desktop tools Digidaw drives (pacman package names)
LINUX_TOOLS = {"hyprctl": "hyprland", "wpctl": "wireplumber", "pactl": "libpulse", "playerctl": "playerctl",
               "wl-copy": "wl-clipboard", "grim": "grim", "xdg-open": "xdg-utils", "notify-send": "libnotify",
               "brightnessctl": "brightnessctl", "gio": "glib2"}


def missing_tools() -> list[str]:
    if oscompat.IS_WINDOWS:
        return []
    import shutil
    return sorted({package for tool, package in LINUX_TOOLS.items() if not shutil.which(tool)})


def missing_dependencies() -> list[str]:
    missing = []
    for module, package in DEPENDENCIES.items():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)
    return missing


def print_dashboard(voice_enabled: bool, hud_ok: bool, hotkey_ok: bool, missing: list[str]):
    if not NO_CONSOLE:
        console.clear()
    console.print(Panel(Text(BANNER, style="bold cyan"),
                        subtitle="[bold yellow]DIGIDAW SYSTEMS[/bold yellow]", border_style="cyan"))

    table = Table(show_header=True, header_style="bold magenta", border_style="dim")
    table.add_column("Subsystem", style="cyan", width=26)
    table.add_column("Status", width=58)

    voice_desc = {"edge": f"Edge Neural ({config.EDGE_VOICE_ID.split('-')[2].removesuffix('Neural')} / "
                          f"{config.EDGE_VOICE_EN.split('-')[2].removesuffix('Neural')})",
                  "gtts": "Google TTS", "sapi": "Windows SAPI (offline)"}.get(config.TTS_ENGINE, config.TTS_ENGINE)
    table.add_row("Voice Synthesis (TTS)", f"[green]Online — {voice_desc}[/green]")

    if voice_enabled:
        wake = "wake word \"Digidaw\"" if config.REQUIRE_WAKE_WORD else "selalu mendengar"
        detector = "WebRTC VAD" if stt.stt_engine.mode == "vad" else "energy"
        table.add_row("Speech Recognition (STT)",
                      f"[green]Online ({stt.stt_engine.device_name[:22]}) · {detector} · {wake}[/green]")
    elif stt.is_mic_working():
        table.add_row("Speech Recognition (STT)", "[yellow]Dimatikan (mode teks)[/yellow]")
    else:
        table.add_row("Speech Recognition (STT)", "[yellow]Unavailable (Using Text Fallback)[/yellow]")

    if hotkey_ok:
        table.add_row("Hotkeys", f"[green]{config.PUSH_TO_TALK_HOTKEY.upper()} bicara · "
                                 f"{config.SHOW_HUD_HOTKEY.upper()} tampilkan HUD[/green]")
    elif not oscompat.IS_WINDOWS:
        table.add_row("Hotkeys", "[yellow]Tidak aktif (butuh Hyprland)[/yellow]")
    table.add_row("System Automation", f"[green]Full Access ({len(brain.TOOLS)} skills)[/green]")
    exe = "firefox (BiDi)" if media._firefox() else media.browser_exe()
    sessions = "Windows Media Session" if oscompat.IS_WINDOWS else "MPRIS (playerctl)"
    table.add_row("Media Engine", f"[green]YouTube autoplay via {os.path.basename(exe)} + {sessions}[/green]"
                  if exe else "[yellow]Chrome/Edge/Brave tidak ditemukan (YouTube dibuka biasa)[/yellow]")
    gemini_status = ("[green]Connected (Agent Mode · vision · web search)[/green]" if config.GEMINI_API_KEY
                     else "[yellow]Awaiting Key (Local Mode Active)[/yellow]")
    table.add_row("Neural Core (Gemini AI)", gemini_status)
    import local_llm
    if config.BRAIN_MODE != "gemini":
        local_llm.warm_up(brain._local_system_prompt(offline=False))
        table.add_row("Local Brain (Ollama)", f"[green]{config.LOCAL_MODEL} · mode {config.BRAIN_MODE}[/green]"
                      if local_llm.available(refresh=True)
                      else f"[yellow]Tidak aktif: {local_llm.status_reason()} (semua lewat Gemini)[/yellow]")
    if config.HUD_ENABLED:
        table.add_row("Holographic HUD", f"[green]{hud_server.url()}[/green]" if hud_ok
                      else f"[yellow]Port {config.HUD_PORT} sedang dipakai[/yellow]")
    table.add_row("Conversation Mode", f"[green]Mendengar {config.FOLLOWUP_SECONDS} detik setelah menjawab[/green]"
                  if config.CONVERSATION_MODE else "[dim]Nonaktif[/dim]")
    table.add_row("Project Folder", f"[green]{config.PROJECT_DIR}[/green]" if config.PROJECT_DIR
                  else "[dim]Belum diatur (Control Panel)[/dim]")
    table.add_row("Long-term Memory", f"[green]{len(memory.all_facts())} fakta tersimpan[/green]")
    table.add_row("Dependencies", f"[red]Belum terinstall: {', '.join(missing)} — jalankan {SETUP_SCRIPT}[/red]"
                  if missing else "[green]Lengkap[/green]")
    tools = missing_tools()
    if tools:
        table.add_row("Linux tools", f"[yellow]Belum ada: sudo pacman -S {' '.join(tools)}[/yellow]")
    console.print(table)
    console.print()


def _startup_ai_check(core):
    """Right after start: does the AI really work with every tool? Wait for the network first (the PC may
    have just booted). Problems are announced once; a working AI only shows on the HUD indicator."""
    import local_stt
    for _ in range(24):
        if local_stt.internet_ok():
            break
        time.sleep(5)
    else:
        return                       # still offline: the connectivity notice covers it; checked again when online
    result = brain.self_check()
    if result["dropped"]:
        core.announce(f"Perhatian, {config.USER_TITLE}: {len(result['dropped'])} fitur AI saya nonaktifkan karena error "
                      f"({', '.join(result['dropped'][:3])}). Detailnya di log.", "warning")
    if result["ok"]:
        console.print(f"[dim]   AI siap: {result['model']} ({result['ms']} ms)[/dim]")
    else:
        core.announce(f"Peringatan, {config.USER_TITLE}: AI tidak bisa dipakai, {result['reason']}. "
                      f"Perintah dasar tetap jalan.", "warning")


def _first_start_today() -> bool:
    """True once per day (remembered in logs/startup_state.json)."""
    import datetime
    state_file = config.LOG_DIR / "startup_state.json"
    today = datetime.date.today().isoformat()
    try:
        last = json.loads(state_file.read_text(encoding="utf-8")).get("last_briefing")
    except (OSError, ValueError):
        last = None
    if last == today:
        return False
    try:
        config.LOG_DIR.mkdir(exist_ok=True)
        state_file.write_text(json.dumps({"last_briefing": today}), encoding="utf-8")
    except OSError:
        pass
    return True


def _speak_startup_briefing():
    """Right after Windows starts the network may not be up yet: wait for it (up to a minute) so the
    weather is there and the neural voice works."""
    import events
    import local_stt
    for _ in range(30):
        if local_stt.internet_ok():
            break
        time.sleep(2)
    text = ai_skills.startup_briefing()
    console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {text}")
    events.emit("digidaw", text=text)
    tts.speak(text)


def _relaunch():
    """Start a fresh copy of Digidaw (used by "Restart" in the Control Panel)."""
    exe = sys.executable
    if NO_CONSOLE and exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    args = [a for a in sys.argv[1:] if a not in ("--restarted", "--background")] + ["--restarted"]
    if oscompat.IS_WINDOWS:
        subprocess.Popen([exe, os.path.join(BASE, "main.py"), *args], cwd=BASE,
                         creationflags=subprocess.DETACHED_PROCESS if NO_CONSOLE else subprocess.CREATE_NEW_CONSOLE)
        return
    import shlex
    import linux_desktop
    command = shlex.join([exe, os.path.join(BASE, "main.py"), *args])
    argv = None if NO_CONSOLE else linux_desktop.terminal_argv(BASE, command, "DIGIDAW Console")
    if argv:                                  # console mode: the new copy gets its own terminal window
        subprocess.Popen(argv, cwd=BASE, **oscompat.detached())
    else:
        subprocess.Popen([exe, os.path.join(BASE, "main.py"), *args], cwd=BASE,
                         env=dict(os.environ, DIGIDAW_NO_CONSOLE="1"), **oscompat.detached())


def main(args=ARGS):
    voice_enabled = not args.text and stt.is_mic_working()
    media.start_monitor()   # keeps the "what's playing" snapshot fresh without ever blocking
    core = DigidawCore(voice_enabled)
    system_control.ANNOUNCE = core.announce
    project_builder.ANNOUNCE = core.announce
    project_builder.BRIEF_HOOK = core.start_brief
    security_scan.ANNOUNCE = core.announce
    generator.ANNOUNCE = core.announce
    alarms.ANNOUNCE = core.announce
    alarms.start()
    security_scan.start_background()
    screen_context.start()
    if config.STT_PRIVACY_MODE or config.OFFLINE_STT:
        import local_stt
        threading.Thread(target=local_stt.ensure_models, daemon=True, name="whisper-models").start()
    threading.Thread(target=system_control._start_apps, daemon=True, name="start-apps").start()   # warm the app list
    if config.DUCK_MEDIA:
        tts.set_hooks(before=media.duck, after=media.unduck)
    if not oscompat.IS_WINDOWS:
        # pygame/SDL replaces the SIGTERM handler with its own (it only queues a quit event nobody reads), so
        # Digidaw would ignore `kill`, log-out and `uwsm stop`. Stop the same way as Ctrl+C instead.
        import signal

        def _terminate(signum, frame):
            core.shutdown.set()
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _terminate)
        signal.signal(signal.SIGHUP, _terminate)
    core.start()

    hud_ok = False
    if config.HUD_ENABLED and not args.no_hud:
        hud_ok = hud_server.start(core.submit, core.handle_action)
        if hud_ok and config.HUD_AUTO_OPEN and not args.background:
            hud_server.open_window()
    hotkey_ok = False
    if voice_enabled:
        hotkey_ok = hotkeys.start(core.push_to_talk, config.PUSH_TO_TALK_HOTKEY)
    if hud_ok:
        hotkeys.start(hud_server.open_window, config.SHOW_HUD_HOTKEY)
    # Esc-to-talk is handled by the HUD page itself (only while the HUD window is focused)
    monitor.start(core.announce)

    print_dashboard(voice_enabled, hud_ok, hotkey_ok, missing := missing_dependencies())
    if missing:
        import events
        events.emit("alert", level="warning",
                    text=f"Modul belum terinstall: {', '.join(missing)}. Jalankan {SETUP_SCRIPT}.")

    if not args.text and not voice_enabled:
        if oscompat.IS_WINDOWS:
            console.print("[bold yellow][!] Mikrofon tidak terdeteksi atau akses diblokir oleh Windows.[/bold yellow]")
            console.print("[dim]Pastikan izin mikrofon aktif di: Windows Settings > Privacy & security > Microphone[/dim]")
        else:
            console.print("[bold yellow][!] Mikrofon tidak terdeteksi.[/bold yellow]")
            console.print("[dim]Periksa input default di pavucontrol / `wpctl status` (PipeWire), dan pastikan "
                          "mikrofon tidak di-mute.[/dim]")

    # warm caches in the background: Start Menu app list (open apps, media app names)
    threading.Thread(target=system_control._start_apps, daemon=True).start()
    tts.preload(FILLERS + [f"Media di-pause, {config.USER_TITLE}.", f"Melanjutkan pemutaran, {config.USER_TITLE}.",
                           f"Lagu berikutnya, {config.USER_TITLE}.", f"Lagu sebelumnya, {config.USER_TITLE}.",
                           f"Sama-sama, {config.USER_TITLE}.", f"Mengunci komputer sekarang, {config.USER_TITLE}."])

    threading.Thread(target=_startup_ai_check, args=(core,), daemon=True, name="ai-startup-check").start()
    # frequent replies (favourite apps, workspaces, volume...) get their voice cached so they play instantly
    import persona
    persona.is_cached = tts.is_cached
    threading.Timer(20, lambda: tts.warm_cache(persona.warm_phrases())).start()
    first_today = _first_start_today()
    if not args.restarted and config.STARTUP_BRIEFING and first_today:
        threading.Thread(target=_speak_startup_briefing, daemon=True, name="startup-briefing").start()
    elif not args.background and not args.restarted:
        greeting = f"{ai_skills.greeting()}, {config.USER_TITLE}. Digidaw online."
        if voice_enabled:
            greeting += " Panggil saya kapan saja." if config.REQUIRE_WAKE_WORD else " Saya mendengarkan."
        console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {greeting}")
        import events
        events.emit("digidaw", text=greeting)
        tts.speak(greeting)

    try:
        if voice_enabled:
            if not NO_CONSOLE:
                console.print(f"\n[bold green]● Mendengarkan — ucapkan \"Digidaw\" lalu perintah Anda, tekan "
                              f"{config.PUSH_TO_TALK_HOTKEY.upper()}, atau ketik perintah lalu Enter.[/bold green]\n")
                threading.Thread(target=core.console_loop, kwargs={"prompt": False}, daemon=True).start()
            core.voice_loop()
        elif NO_CONSOLE:
            core.shutdown.wait()          # the HUD is the keyboard now
        else:
            core.console_loop()
    except KeyboardInterrupt:
        console.print("\n[bold red]Deactivating DIGIDAW...[/bold red]")
    finally:
        core.shutdown.set()
        media.unduck(force=True)
        hotkeys.stop()
        if core.restart_requested:
            _relaunch()
        time.sleep(0.3)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        if NO_CONSOLE:
            log_hint = "logs\\digidaw.log" if oscompat.IS_WINDOWS else "logs/digidaw.log"
            oscompat.notify_error("Digidaw", f"Digidaw berhenti karena error:\n\n{e}\n\nDetail ada di {log_hint}")
        raise
    os._exit(0)   # don't wait for background threads (mic, HUD) — they are daemons anyway
