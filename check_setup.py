"""
Digidaw setup check: is everything Digidaw needs in place, and if not, how to fix it.

    Linux:   ~/.local/share/digidaw/venv/bin/python check_setup.py      (setup_digidaw.sh runs it at the end)
    Windows: .venv\\Scripts\\python.exe check_setup.py                   (setup_digidaw.bat runs it at the end)

Nothing is changed; the exit code is 1 when something Digidaw can't run without is missing.
"""
import importlib
import os
import shutil
import sys
import time

import oscompat

IS_WINDOWS = oscompat.IS_WINDOWS
OK, WARN, FAIL = "\033[32m✔\033[0m", "\033[33m!\033[0m", "\033[31m✘\033[0m"
if IS_WINDOWS:
    os.system("")                      # enables the colour codes in cmd.exe
problems = {"fail": 0, "warn": 0}


def report(status: str, title: str, detail: str = "", fix: str = ""):
    if status == FAIL:
        problems["fail"] += 1
    elif status == WARN:
        problems["warn"] += 1
    print(f" {status} {title}" + (f" — {detail}" if detail else ""))
    if fix and status != OK:
        print(f"     ↳ {fix}")


def section(name: str):
    print(f"\n\033[1m{name}\033[0m")


def check_python():
    section("Python")
    v = sys.version_info
    report(OK if v[:2] >= (3, 11) else FAIL, f"Python {v.major}.{v.minor}.{v.micro}",
           fix="Digidaw butuh Python 3.11+ (setup memakai 3.12).")
    modules = ["google.genai", "edge_tts", "pygame", "pyaudio", "webrtcvad", "faster_whisper", "speech_recognition",
               "psutil", "requests", "rich", "websockets", "aiohttp", "PIL", "pymupdf", "openpyxl", "pptx"]
    if IS_WINDOWS:
        modules += ["pyautogui", "win32gui", "pycaw"]
    missing = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    report(OK if not missing else FAIL, "Modul Python", f"belum ada: {', '.join(missing)}" if missing else "lengkap",
           fix="Jalankan ulang setup_digidaw" + (".bat" if IS_WINDOWS else ".sh"))


def check_system_tools():
    if IS_WINDOWS:
        return
    section("Program sistem (Linux)")
    required = {"wpctl": "volume (wireplumber)", "pactl": "kecilkan musik saat bicara (libpulse / pulseaudio-utils)",
                "pw-play": "bunyi notifikasi & alarm (pipewire)", "wl-copy": "clipboard (wl-clipboard)",
                "grim": "screenshot", "playerctl": "kontrol musik", "xdg-open": "buka file & link (xdg-utils)",
                "notify-send": "notifikasi (libnotify)"}
    optional = {"brightnessctl": "atur kecerahan layar", "espeak-ng": "suara cadangan saat offline",
                "zenity": "pemilih folder di Control Panel", "gio": "buka aplikasi dari menu (glib2)"}
    for tools, status in ((required, FAIL), (optional, WARN)):
        for tool, purpose in tools.items():
            found = shutil.which(tool)
            report(OK if found else status, tool, purpose, fix="Install lewat setup_digidaw.sh atau package manager Anda.")
    hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl")
    report(OK if hypr else WARN, "Hyprland", "jendela, workspace & hotkey" if hypr else "tidak terdeteksi",
           fix="Fitur jendela/workspace/hotkey butuh Hyprland; perintah suara lainnya tetap jalan.")


def check_microphone():
    section("Mikrofon & suara")
    try:
        import pyaudio
        audio = pyaudio.PyAudio()
        inputs = [audio.get_device_info_by_index(i)["name"] for i in range(audio.get_device_count())
                  if audio.get_device_info_by_index(i).get("maxInputChannels", 0) > 0]
        audio.terminate()
        report(OK if inputs else FAIL, "Mikrofon", f"{len(inputs)} input ditemukan" if inputs else "tidak ada",
               fix="Sambungkan mikrofon / cek pavucontrol, lalu jalankan lagi.")
    except Exception as e:
        report(FAIL, "Mikrofon", f"PyAudio error: {e}", fix="Install portaudio lalu jalankan ulang setup.")
    try:
        import asyncio
        import edge_tts

        async def probe():
            got = False
            async for chunk in edge_tts.Communicate("Tes.", "id-ID-GadisNeural").stream():
                got = got or chunk["type"] == "audio"      # read to the end so the connection closes cleanly
            return got
        started = time.time()
        ok = asyncio.run(asyncio.wait_for(probe(), 12))
        report(OK if ok else WARN, "Suara neural (Microsoft Edge)", f"terjangkau, {time.time() - started:.1f} dtk",
               fix="Butuh internet; tanpa itu Digidaw memakai suara cadangan.")
    except Exception as e:
        report(WARN, "Suara neural (Microsoft Edge)", f"tidak terjangkau ({type(e).__name__})",
               fix="Butuh internet; tanpa itu Digidaw memakai suara cadangan.")


def check_ai():
    section("Otak AI")
    import config
    key = config.GEMINI_API_KEY
    if not key:
        report(WARN, "Gemini API key", "belum diisi",
               fix="Buat gratis di https://aistudio.google.com/apikey lalu isi GEMINI_API_KEY di file .env "
                   "(atau lewat Control Panel → AI).")
    else:
        try:
            import llm
            client = llm.get_client()
            started = time.time()
            client.models.generate_content(model=config.FALLBACK_MODELS[0], contents="Jawab: ok")
            report(OK, "Gemini", f"API key valid, menjawab dalam {time.time() - started:.1f} dtk")
        except Exception as e:
            kind = llm.classify(e)
            hint = {"key": "API key ditolak — periksa kembali isinya di .env.",
                    "network": "Tidak ada koneksi ke Google.",
                    "quota": "Kuota gratis sedang habis; coba lagi nanti."}.get(kind, str(e)[:120])
            report(FAIL if kind == "key" else WARN, "Gemini", kind, fix=hint)
    if config.BRAIN_MODE == "gemini":
        report(OK, "Otak lokal (Ollama)", "tidak dipakai (BRAIN_MODE = gemini)")
        return
    import local_llm
    if local_llm.available(refresh=True):
        report(OK, "Otak lokal (Ollama)", f"{config.LOCAL_MODEL} siap")
    else:
        report(WARN, "Otak lokal (Ollama)", local_llm.status_reason(),
               fix=f"Opsional tapi disarankan (cepat & gratis): install Ollama dari https://ollama.com lalu "
                   f"`ollama pull {config.LOCAL_MODEL}`. Tanpa Ollama semua lewat Gemini.")
    if not key and not local_llm.available():
        report(FAIL, "AI", "tidak ada Gemini key maupun Ollama — hanya perintah dasar yang bisa dipakai",
               fix="Isi Gemini API key dan/atau install Ollama.")


def check_extras():
    section("Lainnya")
    import local_stt
    have = all((local_stt.model_path(s) / "model.bin").exists() for s in (local_stt.GATE_MODEL, local_stt.OFFLINE_MODEL))
    report(OK if have else WARN, "Model pengenal suara lokal (Whisper)", "ada" if have else "belum diunduh",
           fix="Diunduh otomatis saat Digidaw pertama jalan (±220 MB), atau jalankan ulang setup.")
    import media
    browser = "Firefox" if media._firefox() else media.browser_exe()
    report(OK if browser else WARN, "Browser pemutar YouTube", str(browser or "tidak ditemukan"),
           fix="Install Firefox atau Chrome/Chromium agar lagu bisa diputar otomatis.")


def main():
    print("\033[1;36m═══ Pemeriksaan setup DIGIDAW ═══\033[0m")
    for step in (check_python, check_system_tools, check_microphone, check_ai, check_extras):
        try:
            step()
        except Exception as e:
            report(WARN, step.__name__, f"pemeriksaan gagal: {e}")
    print()
    if problems["fail"]:
        print(f"{FAIL} {problems['fail']} masalah harus diperbaiki dulu (lihat ↳ di atas).")
    elif problems["warn"]:
        print(f"{OK} Digidaw siap dipakai. {problems['warn']} hal opsional bisa ditingkatkan (lihat ↳ di atas).")
    else:
        print(f"{OK} Semua siap. Jalankan Digidaw!")
    return 1 if problems["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
