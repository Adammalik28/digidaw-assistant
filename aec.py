"""
Interrupting Digidaw by voice while it talks (config.BARGE_IN): "cukup", "stop", or "Digidaw, <another command>".

The microphone Digidaw listens to is shut while it speaks, or it would hear itself and answer itself. This module listens while it speaks, on two streams of its own: the
microphone, and what the speakers are being given (their monitor). With the speakers at 90% Digidaw's voice arrives at this laptop's microphone 2-3 times louder than a person's (13000-20000; at 50% about 3000), and PipeWire's own echo
cancellation left so much of that that it held itself 10 times a minute, so it isn't used. Instead:

1. EchoCanceller, a plain adaptive filter, takes out the part of the voice that can be predicted (about 10 dB here: the path through the speakers isn't linear).
2. What is left is compared with how much echo should be left at that very moment. Between syllables almost none is, so a voice stands out even when it is quieter than Digidaw's.
   Live, words from a second speaker: 11 of 11 at level 6000 and up, 5 of 9 at 4000; with the speakers at 90% no false alarm in 77 s of Digidaw alone.
3. The speech holds its breath at once: over Digidaw's voice not even Google reads a word (0 of 7 clear ones), with the voice held it does. It can't be the only judge, it leaves half of
   even clean lone words blank ("stop", "udah"). So a stop word or the name ends the speech, other words mean it wasn't for Digidaw and the speech goes on, and a short voice nobody could
   read also ends it, if it sounds like a voice at all (a beep, a clink or a key doesn't).

Nothing here touches the sound of Digidaw or any other app, or the everyday microphone. So the worst this module can do is not hear "cukup". What is said over the voice is the only
thing sent to Google without the name in it, and only while Digidaw is talking and only when it is loud enough to hold the speech.

Linux with PipeWire or PulseAudio (pactl, parec) only; anywhere else Digidaw can only be interrupted by the hotkey or the HUD.
"""
import collections
import queue
import re
import shutil
import subprocess
import threading
import time

import numpy as np

import config
import diag

RATE = 16000
BLOCK = 240                          # samples (15 ms) the filter works on
PARTITIONS = 21                      # about 315 ms of echo. The microphone stream starts first, so the speakers' sound always comes to the filter before its echo
STEP = 0.2                           # how fast the filter learns
LEARNED_SECONDS = 4                  # of sound through the speakers at the very least...
TRUST_SECONDS = 2.0                  # ...and over this long what the filter leaves is at most...
TRUST_LEFT = 0.5                     # ...this share of the echo it predicts (working: about 0.25; wrong: 1 or more). Until then Digidaw can't be interrupted by voice
FRAME_BLOCKS = 2                     # levels are looked at per 30 ms
FRAME_SAMPLES = BLOCK * FRAME_BLOCKS
ECHO_SHARE = 0.25                    # what the filter leaves, as a share of the echo it predicted over the last moments (0.18: 23 false alarms in a minute; 0.25: none)
ECHO_FLOOR = 300.0
GATE_LEVEL = 2000                    # with the echo accounted for: over this (and clearly over the room: it can be 3000 with the rumble)...
GATE_OVER_ROOM = 2.5
GATE_FRAMES = 3                      # ...this many frames in a row (2: 3 false alarms in a minute; 3: none)...
PEAK_OVER_ECHO = 2.0                 # ...with one of them this many times what can be left of the echo, is a voice. What the echo itself does stays under 1.5, words reach 2-12
STRONG_OVER_ECHO = 3.0               # this much over it, 2 frames are enough
CLEAR_VOICE_FRAMES = 4               # a phrase with this much voice in it...
VOICE_BAND, VOICE_SHARE = (150, 1500), 0.6  # ...sounding like a voice really was someone: when nobody can read it, it counts as "stop"
ECHO_WINDOW = 7                      # frames: how far back the echo counts (it rings on a little, and the two streams aren't exactly in step)
END_FACTOR, END_LEAST = 1.8, 500     # with the speech held, under this much over the room is silence
END_SILENCE_MS = 330                 # a word has ended
LONGEST_SECONDS = 2.2                # not a stop word any more: decided now. ("Digidaw, putar musik di youtube": the rest is heard the normal way, the microphone is open by then)
SHORT_PHRASE_SECONDS = 1.8           # a voice this short that nobody can read is taken for "stop". Longer: someone talking, or the TV
PREROLL_FRAMES = 8                   # the start of the word, before it was noticed
PAUSE_MOST_SECONDS = 3.0             # never held longer than this, whatever the recognizer does
AGAIN_AFTER_SECONDS = 2.0            # after going on: not held again right away
FALSE_ALARMS_MOST = 2                # that many false alarms in one answer (a TV, someone else talking): it just finishes
WATCH_DEVICES_EVERY = 10             # seconds: another speaker or microphone chosen means another path for the echo

_stopping = threading.Event()
_ready = threading.Event()           # both streams are being read and the filter has learned
_started = False
_recorders: list = []
_held_timer = None
_gain = 1.0


def active() -> bool:
    """Can Digidaw be interrupted by voice right now?"""
    return _ready.is_set() and config.BARGE_IN


def own_stream_pids() -> set[str]:
    """The programs reading the microphone for this module: Digidaw, not someone in a call."""
    return {str(recorder.pid) for recorder in _recorders}


class EchoCanceller:
    """A frequency-domain adaptive filter (partitioned block, overlap-save): given what the speakers play, takes what that predicts out of what the microphone hears."""

    def __init__(self, block: int = BLOCK, partitions: int = PARTITIONS, step: float = STEP):
        self.block, self.step = block, step
        self.filter = np.zeros((partitions, block + 1), dtype=complex)
        self.spectra = np.zeros((partitions, block + 1), dtype=complex)
        self.last = np.zeros(block)
        self.power = np.ones(block + 1)
        self.learned_blocks = 0

    def learned_seconds(self) -> float:
        return self.learned_blocks * self.block / RATE

    def process(self, speaker: np.ndarray, mic: np.ndarray, learn: bool = True) -> tuple[np.ndarray, np.ndarray]:
        """One block of each. Returns (what is left, the echo it predicted)."""
        block = self.block
        self.spectra = np.roll(self.spectra, 1, axis=0)
        self.spectra[0] = np.fft.rfft(np.concatenate([self.last, speaker]))
        self.last = speaker.copy()
        predicted = np.fft.irfft(np.sum(self.filter * self.spectra, axis=0))[block:]
        left = mic - predicted
        if learn and np.any(speaker):
            error = np.fft.rfft(np.concatenate([np.zeros(block), left]))
            now = np.sum(np.abs(self.spectra) ** 2, axis=0)
            self.power = np.maximum(0.9 * self.power + 0.1 * now, now)      # (never behind a sudden start of the voice: that blows the filter up)
            self.filter += np.conj(self.spectra) * (self.step * error / (self.power + 1e6))
            impulse = np.fft.irfft(self.filter, axis=1)
            impulse[:, block:] = 0
            self.filter = np.fft.rfft(impulse, axis=1)
            self.learned_blocks += 1
        return left, predicted


_HANN = np.hanning(FRAME_SAMPLES)
_HANN_SCALE = 2.0 / (FRAME_SAMPLES * np.sum(_HANN ** 2))
_FIRST_BIN = int(np.ceil(config.STT_HIGHPASS_HZ * FRAME_SAMPLES / RATE)) if config.STT_HIGHPASS_HZ else 0


def frame_level(samples: np.ndarray) -> float:
    """How loud 30 ms is, without what is below the voice (config.STT_HIGHPASS_HZ: rumble). The same as RMS for a tone, and on numpy's FFT: the filter stt.py uses runs in Python, per sample."""
    spectrum = np.fft.rfft(samples * _HANN)
    return float(np.sqrt(np.sum(np.abs(spectrum[_FIRST_BIN:]) ** 2) * _HANN_SCALE))


class Trust:
    """Is the filter doing its job right now? If it isn't, what it leaves looks like a voice: a false alarm every second. Also when it has only just begun, or the speakers were moved."""

    def __init__(self):
        self.left = 0.0
        self.echo = 0.0
        self.keep = 1 - (FRAME_BLOCKS * BLOCK / RATE) / TRUST_SECONDS

    def note(self, level: float, predicted: float, room: float):
        self.left = self.keep * self.left + (1 - self.keep) * max(level * level - room * room, 0.0)
        self.echo = self.keep * self.echo + (1 - self.keep) * predicted * predicted

    def good(self) -> bool:
        return self.echo <= (GATE_LEVEL / 2) ** 2 or self.left <= TRUST_LEFT ** 2 * self.echo     # (an echo this small can't pass for a voice anyway)


def voice_share(audio: bytes) -> float:
    """How much of a phrase's sound lies where a voice has it (rumble left out). Words are 70-95% there; a beep, a clink or a key on the keyboard 5-50%."""
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
    if samples.size < 2:
        return 0.0
    power = np.abs(np.fft.rfft(samples * np.hanning(samples.size))) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1 / RATE)
    return float(power[(freqs >= VOICE_BAND[0]) & (freqs < VOICE_BAND[1])].sum() / max(power[freqs >= VOICE_BAND[0]].sum(), 1.0))


class Listener:
    """Frames of what the filter left and the echo it predicted. A voice over that holds the speech (on_voice); the phrase is complete when it has gone quiet."""

    def __init__(self, on_voice):
        self.on_voice = on_voice
        self.echo: collections.deque = collections.deque(maxlen=ECHO_WINDOW)
        self.room: collections.deque = collections.deque(maxlen=100)        # about 3 s of what the filter leaves with nothing playing
        self.not_before = 0.0
        self.false_alarms = 0
        self.preroll: collections.deque = collections.deque(maxlen=PREROLL_FRAMES)
        self.reset()

    def reset(self):
        self.over = 0
        self.peak = 0.0
        self.voiced = 0
        self.frames: list[bytes] | None = None
        self.started = 0.0
        self.quiet = 0

    def new_answer(self):
        self.reset()
        self.false_alarms = 0
        self.not_before = 0.0

    def note_room(self, level: float):
        self.room.append(level)

    def room_level(self) -> float:
        ordered = sorted(self.room)
        return ordered[len(ordered) // 2] if ordered else 0.0

    def end_level(self) -> float:
        return max(END_LEAST, self.room_level() * END_FACTOR)

    def over_echo(self, level: float, predicted: float) -> float:
        """How many times more this frame is than what can be left of the echo right now. 0 when it isn't clearly over the room either."""
        self.echo.append(predicted)
        if level <= max(GATE_LEVEL, self.room_level() * GATE_OVER_ROOM):
            return 0.0
        return level / (ECHO_SHARE * max(self.echo) + ECHO_FLOOR)

    def feed(self, frame: bytes, level: float, predicted: float, now: float, may_listen: bool = True):
        """Returns (audio, started, ended, clear voice?) when a phrase is complete."""
        ratio = self.over_echo(level, predicted)
        if self.frames is None:
            self.over = self.over + 1 if ratio > 1 else 0
            self.peak = max(self.peak, ratio) if ratio > 1 else 0.0
            self.preroll.append(frame)
            noticed = (self.over >= GATE_FRAMES and self.peak >= PEAK_OVER_ECHO) or (self.over >= 2 and self.peak >= STRONG_OVER_ECHO)
            if noticed and may_listen and now >= self.not_before and self.false_alarms < FALSE_ALARMS_MOST:
                self.frames = list(self.preroll)
                self.started = now - len(self.frames) * 0.03
                self.preroll.clear()
                self.voiced = self.over
                self.quiet = 0
                self.on_voice()
            return None
        self.frames.append(frame)
        if ratio > 1:
            self.voiced += 1
            self.peak = max(self.peak, ratio)
        self.quiet = self.quiet + 1 if level <= self.end_level() else 0
        if self.quiet * 30 < END_SILENCE_MS and now - self.started < LONGEST_SECONDS:
            return None
        audio = b"".join(self.frames)
        phrase = (audio, self.started, now, self.voiced >= CLEAR_VOICE_FRAMES and self.peak >= PEAK_OVER_ECHO and voice_share(audio) >= VOICE_SHARE)
        self.reset()
        return phrase

    def judged(self, was_for_digidaw: bool):
        """What the phrase turned out to be."""
        self.not_before = time.time() + AGAIN_AFTER_SECONDS
        self.false_alarms += not was_for_digidaw

    def recording(self) -> bool:
        return self.frames is not None


def _devices() -> tuple[str, str]:
    """(the microphone, the speakers) chosen right now."""
    info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5).stdout
    found = {}
    for line in info.splitlines():
        key, _, value = line.partition(": ")
        found[key] = value.strip()
    return found.get("Default Source", ""), found.get("Default Sink", "")


def _speaker_gain() -> float:
    """The monitor carries the sound as it is before the system volume, the echo comes after it (5 Nov: the same level from the file and the monitor, at 90%). PipeWire's percent is cubic."""
    volume = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=3).stdout
    muted = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=3).stdout
    found = re.search(r"(\d+)%", volume)
    return 0.0 if "yes" in muted or not found else (int(found[1]) / 100) ** 3


def _watch_volume():
    global _gain
    while not _stopping.is_set():
        try:
            _gain = _speaker_gain()
        except Exception:
            pass
        _stopping.wait(1)


def _open_stream(device: str) -> subprocess.Popen:
    """A separate program, not a second stream in this process: PortAudio has crashed on reopened streams before (stt._SharedPortAudio)."""
    return subprocess.Popen(["parec", f"--device={device}", "--format=s16le", f"--rate={RATE}", "--channels=1", "--raw", "--latency-msec=20"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _close_streams():
    for recorder in _recorders:
        if recorder.poll() is None:
            recorder.kill()
        recorder.wait()
    _recorders.clear()


def _listen(heard: queue.Queue):
    import tts
    listener = Listener(_hold)
    _listeners.append(listener)
    bytes_per_block = BLOCK * 2
    while not _stopping.is_set():
        try:
            devices = _devices()
            if not all(devices):
                raise RuntimeError("mikrofon atau speaker tidak ditemukan")
            mic_stream = _open_stream(devices[0])                 # first: see PARTITIONS
            time.sleep(0.02)
            speaker_stream = _open_stream(f"{devices[1]}.monitor")
            _recorders[:] = [mic_stream, speaker_stream]
        except Exception as e:
            diag.warn_once("aec.open", "aliran untuk menyela belum bisa dibuka", e)
            _stopping.wait(5)
            continue
        canceller = EchoCanceller()                                # (never kept for later: opened again, the two streams are in step differently, and an old
        trust = Trust()                                            # filter predicts the echo at the wrong moment: it stopped itself a second into its answer)
        pending: list[np.ndarray] = []
        echo_energy = 0.0
        filled = 0
        was_speaking, checked_at, quiet_blocks = False, time.time(), PARTITIONS + 1
        while not _stopping.is_set():
            mic_bytes, speaker_bytes = mic_stream.stdout.read(bytes_per_block), speaker_stream.stdout.read(bytes_per_block)
            if len(mic_bytes) < bytes_per_block or len(speaker_bytes) < bytes_per_block:
                break
            now = time.time()
            if now - checked_at > WATCH_DEVICES_EVERY:
                checked_at = now
                try:
                    if _devices() != devices:
                        print("[aec] speaker atau mikrofon berganti: gema dipelajari lagi")
                        break
                except Exception:
                    pass
            speaker = np.frombuffer(speaker_bytes, dtype=np.int16).astype(np.float64) * _gain
            mic = np.frombuffer(mic_bytes, dtype=np.int16).astype(np.float64)
            speaking = tts.is_speaking()
            if speaking and not was_speaking:
                listener.new_answer()
            was_speaking = speaking
            quiet_blocks = quiet_blocks + 1 if not np.any(speaker) else 0
            idle = quiet_blocks > PARTITIONS and not speaking          # nothing playing or still ringing: nothing to do but notice how the room sounds
            left, predicted = (mic, None) if idle else canceller.process(speaker, mic, learn=not listener.recording())    # (a voice isn't echo: not learned from)
            pending.append(left)
            if predicted is not None:
                echo_energy += float(np.sum(predicted * predicted))
            filled += 1
            if filled < FRAME_BLOCKS:
                continue
            samples = np.concatenate(pending)
            pending.clear()
            filled = 0
            level = frame_level(samples)
            if idle:
                echo_energy = 0.0
                listener.note_room(level)
                continue
            predicted_level = (echo_energy / FRAME_SAMPLES) ** 0.5
            echo_energy = 0.0
            if not listener.recording():
                trust.note(level, predicted_level, listener.room_level())
            good = canceller.learned_seconds() >= LEARNED_SECONDS and trust.good()
            if good and not _ready.is_set():
                print("[aec] Digidaw bisa disela saat bicara: \"cukup\", \"stop\", \"Digidaw, ...\"")
                _ready.set()
            frame = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
            phrase = listener.feed(frame, level, predicted_level, now, may_listen=speaking and good)
            if phrase:
                heard.put(phrase)
        _ready.clear()
        _close_streams()
        _stopping.wait(1)


_listeners: list = []


def _hold():
    """A voice over Digidaw's: the speech holds its breath, so what is being said can be heard."""
    global _held_timer
    import tts
    tts.pause()
    _held_timer = threading.Timer(PAUSE_MOST_SECONDS, tts.resume)
    _held_timer.daemon = True
    _held_timer.start()


def _go_on():
    import tts
    if _held_timer is not None:
        _held_timer.cancel()
    tts.resume()


def _serve(heard: queue.Queue, on_phrase):
    while not _stopping.is_set():
        try:
            audio, started, ended, clear_voice = heard.get(timeout=0.5)
        except queue.Empty:
            continue
        for_digidaw = False
        try:
            for_digidaw = bool(on_phrase(audio, started, ended, clear_voice))
        except Exception as e:
            diag.warn_once("aec.phrase", "kalimat saat Digidaw bicara gagal diperiksa", e)
        _go_on()                         # (stopped for good if it was for Digidaw: nothing to go on with)
        for listener in _listeners:
            listener.judged(for_digidaw)


def unload_leftover():
    """Removes the PipeWire echo-cancel modules earlier versions of this file loaded."""
    if not shutil.which("pactl"):
        return
    try:
        lines = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True, timeout=5).stdout.splitlines()
        for line in lines:
            index, _, rest = line.partition("\t")
            if rest.startswith("module-echo-cancel") and "source_name=digidaw_" in rest:
                subprocess.run(["pactl", "unload-module", index.strip()], capture_output=True, timeout=5)
                print("[aec] peredam gema lama dilepas")
    except Exception:
        pass


def start(on_phrase=None):
    """After the microphone has started. on_phrase(audio, started, ended, clear_voice) -> was it for Digidaw? Something was said while Digidaw was talking (16 kHz, 16 bit, mono)."""
    global _started
    threading.Thread(target=unload_leftover, daemon=True, name="aec-old").start()
    if not config.BARGE_IN or _started or on_phrase is None or not shutil.which("pactl") or not shutil.which("parec"):
        return
    _started = True
    heard: queue.Queue = queue.Queue()
    threading.Thread(target=_watch_volume, daemon=True, name="aec-volume").start()
    threading.Thread(target=_listen, args=(heard,), daemon=True, name="aec-listen").start()
    threading.Thread(target=_serve, args=(heard, on_phrase), daemon=True, name="aec-hear").start()


def stop():
    """Digidaw is stopping."""
    _stopping.set()
    _ready.clear()
    _close_streams()
