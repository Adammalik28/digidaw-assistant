"""
Hears the name itself, in the sound: a small network (scripts/train_wake.py) on openWakeWord's speech features, a few milliseconds on the
CPU every 80 ms. The other recognizers read words and in a noisy room they read something else ("Dido", "gigi dong", or nothing at all).

It never acts alone: a phrase it hears the name in goes on to Google instead of being dropped by the laptop's recognizer, and Google's words still
decide what it was. Until the user's own voice is learned ("latih panggilan") it only watches and keeps score: on synthetic voices alone it missed a
real call at this microphone, where the voice is only twice as loud as the room.
"""
import collections
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

import config

MODEL_DIR = Path(config.BASE_DIR) / "models" / "wake"
GENERAL_MODEL = MODEL_DIR / "digidaw.npz"
PERSONAL_MODEL = MODEL_DIR / "digidaw_personal.npz"
TAKES_FILE = MODEL_DIR / "my_voice.npz"
FRAMES = 16
KEEP_SECONDS = 30
TRAIN_SECONDS = 900
CALL_TAKES = 12
CALL_SECONDS = 2.0
ROOM_SECONDS = 6.0
OTHER_SENTENCES = ["buka firefox", "putar musik", "pindah ke workspace dua", "selamat pagi semuanya"]
OTHER_SECONDS = 2.5


class _Network:
    def __init__(self, path: Path):
        data = np.load(path)
        count = sum(1 for k in data.files if k.startswith("w"))
        self.weights = [data[f"w{i}"].astype(np.float32) for i in range(count)]
        self.biases = [data[f"b{i}"].astype(np.float32) for i in range(count)]
        self.threshold = float(data["threshold"]) if "threshold" in data.files else 0.9

    def score(self, features: np.ndarray) -> float:
        x = features.reshape(-1)
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            x = x @ w + b
            if i < len(self.weights) - 1:
                x = np.maximum(x, 0)
        return float(1 / (1 + np.exp(-np.clip(x[0], -30, 30))))


class Spotter:
    """Frames go in from the microphone's thread and never hold it up; the scoring runs on its own thread."""

    def __init__(self):
        self.features = None
        self.network = None
        self.personal = False
        self.scores: collections.deque = collections.deque(maxlen=int(KEEP_SECONDS / 0.08))
        self.frames: collections.deque = collections.deque(maxlen=200)
        self.wake = threading.Event()
        self.started = False
        self.lock = threading.Lock()

    def load(self) -> bool:
        """The learned model if there is one, otherwise the general one. False: not available, Digidaw works as before."""
        path = PERSONAL_MODEL if PERSONAL_MODEL.exists() else GENERAL_MODEL
        try:
            network = _Network(path)
            if self.features is None:
                from openwakeword.utils import AudioFeatures
                self.features = AudioFeatures(inference_framework="onnx", ncpu=1)
        except Exception as e:                    # no model, or the library isn't there: only this extra is missing
            print(f"[nama] pendeteksi nama tidak aktif: {type(e).__name__}: {str(e)[:100]}")
            return False
        with self.lock:
            self.network, self.personal = network, path == PERSONAL_MODEL
        return True

    def start(self) -> bool:
        if self.started:
            return True
        if not self.load():
            return False
        self.started = True
        threading.Thread(target=self._run, daemon=True, name="name-spotter").start()
        print(f"[nama] pendeteksi nama aktif ({'suara Anda sudah dipelajari' if self.personal else 'hanya mengamati; belum belajar suara Anda'})")
        return True

    def feed(self, frame: bytes, at: float):
        if self.started:
            self.frames.append((frame, at))
            self.wake.set()

    def _run(self):
        pending = []
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            while self.frames:
                frame, at = self.frames.popleft()
                pending.append(frame)
                if sum(map(len, pending)) >= 2560:             # 80 ms
                    audio = np.frombuffer(b"".join(pending), dtype=np.int16)
                    pending = []
                    try:
                        new = self.features(audio)
                        if new:
                            with self.lock:
                                network = self.network
                            self.scores.append((at, network.score(self.features.get_features(FRAMES))))
                    except Exception as e:
                        print(f"[nama] berhenti: {type(e).__name__}: {str(e)[:100]}")
                        self.started = False
                        return

    def score_during(self, started: float, ended: float) -> float:
        """The surest moment in this stretch: two scores in a row have to agree, one alone is often a click."""
        points = [s for at, s in list(self.scores) if started - 0.1 <= at <= ended + 0.3]
        return max((min(a, b) for a, b in zip(points, points[1:])), default=0.0)

    def sure(self, score: float) -> bool:
        return self.personal and self.network is not None and score >= self.network.threshold


spotter = Spotter()


# ───────────────────────────── learning the user's voice ─────────────────────────────

_training = {"busy": False}


def _record(seconds: float) -> np.ndarray:
    """What the microphone hears for this long (the filtered sound Digidaw itself listens to)."""
    import stt
    return np.frombuffer(stt.stt_engine.record_for(seconds), dtype=np.int16)


def _say_and_wait(text: str):
    import tts
    tts.speak(text)
    time.sleep(0.3)
    until = time.time() + 20
    while tts.is_speaking() and time.time() < until:
        time.sleep(0.1)
    time.sleep(0.2)


def _lesson():
    import alarms
    import core
    import stt
    t = config.USER_TITLE
    try:
        time.sleep(0.5)
        _say_and_wait(f"Baik, {t}. Mohon diam sebentar, saya dengarkan ruangannya.")
        takes = {"room": _record(ROOM_SECONDS)}
        if len(takes["room"]) < 16000:
            raise RuntimeError("mikrofon tidak memberi suara")
        _say_and_wait(f"Setelah bunyi ting, ucapkan Digidaw dengan suara seperti biasa memanggil saya. Ada {CALL_TAKES} kali.")
        for i in range(CALL_TAKES):
            core.chime()
            time.sleep(0.25)
            takes[f"call{i:02d}"] = _record(CALL_SECONDS)
            time.sleep(0.15)
        for i, sentence in enumerate(OTHER_SENTENCES):
            _say_and_wait(f"Sekarang ucapkan: {sentence}.")
            core.chime()
            time.sleep(0.25)
            takes[f"other{i}"] = _record(OTHER_SECONDS)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        part = MODEL_DIR / "my_voice.part.npz"
        np.savez(part, **takes)
        os.chmod(part, 0o600)
        os.replace(part, TAKES_FILE)
        stt.stt_engine.stop_recording()
        _say_and_wait(f"Terima kasih, {t}. Saya pelajari dulu, sekitar satu menit. Sambil menunggu, Digidaw tetap bisa dipakai.")
        done = subprocess.run(["nice", "-n", "10", sys.executable, "scripts/train_wake.py", "--personal", str(TAKES_FILE)], cwd=config.BASE_DIR,
                              capture_output=True, text=True, timeout=TRAIN_SECONDS)
        last = (done.stdout.strip().splitlines() or ["{}"])[-1]
        result = json.loads(last) if last.startswith("{") else {}
        if not result or not spotter.load():
            print(f"[nama] pelatihan gagal: {done.stderr[-300:]}")
            alarms.ANNOUNCE and alarms.ANNOUNCE(f"Pelatihan panggilan gagal, {t}. Digidaw tetap seperti sebelumnya.", "warning")
            return
        print(f"[nama] hasil pelatihan: {result}")
        alarms.ANNOUNCE and alarms.ANNOUNCE(f"Suara panggilan Anda sudah saya pelajari, {t}. Dari {result['of']} rekaman yang tidak ikut dilatih, "
                                            f"{result['held']} saya kenali.", "reminder")
    except Exception as e:
        print(f"[nama] latihan terhenti: {type(e).__name__}: {str(e)[:150]}")
        alarms.ANNOUNCE and alarms.ANNOUNCE(f"Latihan panggilan terhenti, {t}. Digidaw tetap seperti sebelumnya.", "warning")
    finally:
        stt.stt_engine.stop_recording()
        _training["busy"] = False


def train_my_call() -> str:
    """Learn how the user sounds saying "Digidaw" ('latih panggilan'): about a minute of saying it after a chime, then Digidaw hears its name far better.
    The takes stay on this laptop."""
    t = config.USER_TITLE
    if _training["busy"]:
        return f"Latihannya sedang berjalan, {t}."
    if not GENERAL_MODEL.exists() or not (MODEL_DIR / "train_set.npz").exists():
        return f"Bahan latihannya belum ada di laptop ini, {t}."
    if not spotter.started:
        return f"Pendeteksi nama sedang tidak aktif, {t}."
    _training["busy"] = True
    threading.Thread(target=_lesson, daemon=True, name="call-lesson").start()
    return f"Baik, {t}, kita mulai."


def forget_my_call() -> str:
    """Delete the recorded calls and what was learned from them ('hapus latihan panggilan')."""
    removed = False
    for path in (TAKES_FILE, PERSONAL_MODEL):
        if path.exists():
            path.unlink()
            removed = True
    spotter.load()
    return f"Rekaman dan hasil latihan panggilan saya hapus, {config.USER_TITLE}." if removed else f"Belum ada latihan panggilan, {config.USER_TITLE}."


AI_TOOLS = [train_my_call, forget_my_call]
