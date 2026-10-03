"""
Speech recognition on this laptop (faster-whisper), used two ways:

1. Privacy gate (STT_PRIVACY_MODE): every phrase is first transcribed locally with the small "tiny"
   model (~0.4 s). Only phrases that call Digidaw — or arrive while Digidaw is actually listening —
   are sent to Google for an accurate transcript. Everyone else's conversation never leaves the PC.
2. Offline fallback: when Google can't be reached, commands are transcribed locally with the more
   accurate "base" model, so basic commands keep working without internet.

Models live in <digidaw>/models/whisper-<size> (downloaded once; see ensure_models()).
"""
import re
import socket
import threading
import time
from pathlib import Path

import numpy as np

import config

MODELS_DIR = Path(config.BASE_DIR) / "models"
GATE_MODEL, OFFLINE_MODEL = "tiny", "base"
PROMPT = "Digidaw. Digidaw, buka Firefox. Digidaw, putar lagu. Digidaw, jam berapa sekarang? Naikkan volume."
# what Whisper writes for "Digidaw" (plus the configured aliases, checked separately)
FUZZY_WAKE = re.compile(r"\b(?:d[iey]g[iey]|did[iy]|dik[iy]|dig)[\s-]?(?:daw|dau|dao|dow|do|da|dawn)\b", re.IGNORECASE)

_models: dict = {}
_load_lock = threading.Lock()
_run_lock = threading.Lock()


def model_path(size: str) -> Path:
    return MODELS_DIR / f"whisper-{size}"


def available(size: str = GATE_MODEL) -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return (model_path(size) / "model.bin").exists()


def _model(size: str):
    with _load_lock:
        if size not in _models:
            from faster_whisper import WhisperModel
            _models[size] = WhisperModel(str(model_path(size)), device="cpu", compute_type="int8",
                                         cpu_threads=config.LOCAL_STT_THREADS)
        return _models[size]


def preload():
    """Load the gate model in the background so the first phrase isn't slower."""
    if config.STT_PRIVACY_MODE and available(GATE_MODEL):
        threading.Thread(target=_model, args=(GATE_MODEL,), daemon=True, name="whisper-load").start()


def transcribe(audio, size: str = GATE_MODEL) -> str:
    """audio: speech_recognition.AudioData (any rate) -> text."""
    raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
    samples = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if samples.size < 1600:
        return ""
    model = _model(size)
    with _run_lock:     # one at a time: keeps CPU use modest and predictable
        segments, _ = model.transcribe(samples, language="id", beam_size=1, initial_prompt=PROMPT,
                                       vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
        text = " ".join(s.text for s in segments).strip()
    # Whisper sometimes "hears" the prompt itself in near-silence
    return "" if text.lower().strip(" .") in ("digidaw", "") and samples.size < 8000 else text


def mentions_wake(text: str) -> bool:
    """Does the (locally transcribed) phrase look like it calls Digidaw? Deliberately lenient:
    a false yes only means this one phrase goes to Google."""
    if not text:
        return False
    import core
    if core.WAKE_RE.search(text) or FUZZY_WAKE.search(text):
        return True
    addressed, _, _ = core.detect_wake(text)
    return addressed


def internet_ok(timeout: float = 2.0) -> bool:
    for host in ("www.google.com", "1.1.1.1"):
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def ensure_models(sizes=(GATE_MODEL, OFFLINE_MODEL), log=print) -> bool:
    """Download missing models (IPv4, one file at a time: this network resets parallel/IPv6 downloads)."""
    missing = [s for s in sizes if not (model_path(s) / "model.bin").exists()]
    if not missing:
        return True
    original = socket.getaddrinfo
    socket.getaddrinfo = lambda host, *a, **k: [r for r in original(host, *a, **k) if r[0] == socket.AF_INET] \
        or original(host, *a, **k)
    try:
        from huggingface_hub import snapshot_download
        for size in missing:
            for attempt in range(4):
                try:
                    log(f"[STT lokal] mengunduh model whisper-{size}…")
                    snapshot_download(f"Systran/faster-whisper-{size}", local_dir=str(model_path(size)), max_workers=1)
                    break
                except Exception as e:
                    log(f"[STT lokal] unduhan gagal ({type(e).__name__}), coba lagi…")
                    time.sleep(3)
    finally:
        socket.getaddrinfo = original
    return all((model_path(s) / "model.bin").exists() for s in sizes)
