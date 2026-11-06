"""
Voice input.

A capture thread keeps the microphone open and cuts the audio into phrases; a second thread sends
each finished phrase to Google Speech Recognition. Capturing never pauses while a phrase is being
recognized, so a command spoken right after "Digidaw ..." is still recorded.

Phrases are detected with WebRTC voice-activity detection (VAD): it recognises the shape of human
speech instead of just loudness, so soft words are caught, fan/room noise is ignored, and the end
of a sentence is detected after only ~0.5 s of silence. If you pause briefly and keep talking, the
pieces are merged back into one sentence before DIGIDAW acts on it. While DIGIDAW is
speaking the microphone is gated (fed silence) so he never hears himself.

Without webrtcvad (or if the mic can't run at 16 kHz) an energy-threshold detector is used instead.
"""
import array
import math
import re
import sys
import time
import queue
import warnings
import threading
import collections
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import DefaultCookiePolicy

import pyaudio
import requests
import speech_recognition as sr

import config
import diag
import metrics
import name_spotter

# Google's speech service, over one kept-alive connection instead of a new one per sentence (the library's way: a
# DNS lookup and a TCP handshake every time; on a lossy network the lookup alone sometimes took 5 s). HTTPS: what the
# user says is not sent in clear text (the library uses plain http).
GOOGLE_STT_URL = "https://www.google.com/speech-api/v2/recognize"
WAKE_LENGTH = (0.3, 1.8)  # seconds: "Digidaw" alone; the laptop's recognizer "hears" the name in longer music / TV


def sounds_like_name(text: str) -> bool:
    """Does Google's text confirm the name the laptop heard: the name (or an alias) itself, or a short phrase that
    sounds like it ("pigi chord" 0.53, "gigi kau" 0.57)? Chatter scores below 0.3 ("tulang lunak", "kok bisa")."""
    import difflib
    import local_stt
    if local_stt.mentions_wake(text):
        return True
    words = text.lower().split()
    if not 1 <= len(words) <= 3:
        return False
    key = "".join(words)
    return max(difflib.SequenceMatcher(None, "digidaw", key[i:i + 8]).ratio()
               for i in range(max(1, len(key) - 6))) >= 0.5
def confirm_alias(local: str | None, text: str) -> str:
    """The laptop's own recognizer heard "Digidaw" itself, and Google wrote one of the words it mishears the name as
    ("digital, ganti lampu ke hijau"): two recognizers agree it's a call, so the name is written as such and the
    phrase counts as a sure call (an alias alone only lets commands that start like one through)."""
    if not local or not text:
        return text
    import core
    if not core.WAKE_RE.search(local) or core.WAKE_RE.search(text):
        return text
    start_re, end_re = core._alias_patterns()
    if start_re and (m := start_re.match(text)):
        return ("Digidaw " + text[m.end():].lstrip(" ,.!?")).strip()
    if end_re and (m := end_re.search(text)) and m.start() > 0:
        return text[:m.start()].rstrip(" ,.!?") + ", Digidaw"
    return text


# a phrase ending like this isn't finished ("lebih…", "pindah ke…"): wait a little longer for the rest of it
# a verb alone too: "buka" (17 times in the log), the user then said it all over again ("buka" … "buka Firefox")
DANGLING_RE = re.compile(r"\b(?:lebih|agak|paling|kurang|ke|di|dari|yang|dan|atau|tapi|untuk|buat|dengan|kalau|"
                         r"jadi|biar|supaya|mau|ingin|pengen|tolong|coba|warna|mode|sama)[\s,.]*$|"
                         r"^\s*(?:buka|tutup|pindah|matikan|nyalakan|hidupkan|putar|jalankan|ubah|ganti|atur)[\s,.]*$", re.IGNORECASE)
DANGLING_HOLD = 1.6        # seconds after such a phrase ends (the usual mid-sentence gap is config.STT_MERGE_GAP)


def dangling(text: str) -> bool:
    return bool(DANGLING_RE.search(text or ""))


_google_session = requests.Session()
_google_session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))   # no cookies, as before: not traceable

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop               # Python 3.13+: provided by the audioop-lts package

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

if sys.platform.startswith("linux"):
    import linux_desktop
    _device_scan_quiet = linux_desktop.quiet_stderr     # PortAudio's device scan otherwise floods the console
else:
    import contextlib
    _device_scan_quiet = contextlib.nullcontext



class _SharedPortAudio:
    """PortAudio started once for the whole run. speech_recognition starts and stops ("terminate") PortAudio every
    time the microphone is (re)opened; after an audio buffer overrun the microphone is reopened, and stopping
    PortAudio then hits a failing assertion in its JACK backend (PipeWire) that aborts the WHOLE process
    ("Fatal Python error: Aborted"). Never stopping it avoids that path; reopening the stream still works."""
    _instance = None

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)

    def PyAudio(self):
        if _SharedPortAudio._instance is None:
            _SharedPortAudio._instance = self._module.PyAudio()
        return _KeptPyAudio(_SharedPortAudio._instance)


class _KeptPyAudio:
    def __init__(self, audio):
        self._audio = audio

    def __getattr__(self, name):
        return getattr(self._audio, name)

    def terminate(self):
        pass             # see _SharedPortAudio


def _keep_portaudio(microphone):
    microphone.pyaudio_module = _SharedPortAudio(microphone.pyaudio_module)
    return microphone

GATE_TAIL_SECONDS = 8.0


class _HighPass:
    """Takes away what's below the voice. A low rumble in the room (22-42 Hz, 3000+ on the meter, 5 Nov) passes for speech: phrases
    never ended, so a call came out 3.4 s late inside 5 s of noise. Two Butterworth biquads, 24 dB per octave."""

    def __init__(self, cutoff: float, rate: int):
        w = 2 * math.pi * cutoff / rate
        cos, alpha = math.cos(w), math.sin(w) / math.sqrt(2)
        norm = 1 + alpha
        self.b0, self.b1 = (1 + cos) / 2 / norm, -(1 + cos) / norm
        self.a1, self.a2 = -2 * cos / norm, (1 - alpha) / norm
        self.state = [0.0, 0.0, 0.0, 0.0]

    def __call__(self, frame: bytes) -> bytes:
        samples = array.array("h", frame)
        b0, b1, a1, a2 = self.b0, self.b1, self.a1, self.a2
        s1, s2, s3, s4 = self.state
        for i, x in enumerate(samples):
            y = b0 * x + s1                  # (b2 == b0)
            s1 = b1 * x - a1 * y + s2
            s2 = b0 * x - a2 * y
            z = b0 * y + s3
            s3 = b1 * y - a1 * z + s4
            s4 = b0 * y - a2 * z
            samples[i] = -32768 if z < -32768 else 32767 if z > 32767 else int(z)
        self.state = [s1, s2, s3, s4]
        return samples.tobytes()


class _VadSegmenter:
    """Turns a stream of 30 ms frames into phrases using WebRTC VAD + a relative energy gate."""

    FRAME_MS = 30
    START_WINDOW, START_VOICED = 5, 3      # 3 voiced frames out of the last 5 (~90 ms) starts a phrase
    PREROLL_MS = 300                       # audio kept from just before speech started
    TRAIL_KEEP_MS = 500                    # what the detector calls silence after the last word is kept: a soft ending ("Fire-fox", "kuli-ah")
                                           # is often in it. (All of it: costs nothing, the phrase ended anyway.)
    MIN_VOICED_MS = 240                    # shorter blips (clicks, coughs) are dropped
    ENERGY_FACTOR = 2.5                    # a speech frame must also be this much louder than the room (1.8: phrases never ended in
                                           # a noisy room; 3.5: a soft call was lost. Measured on recordings, 5 Nov)

    def __init__(self, sample_rate: int):
        self.rate = sample_rate
        self.frame_samples = sample_rate * self.FRAME_MS // 1000
        self.vad = webrtcvad.Vad(config.STT_VAD_AGGRESSIVENESS)
        self.end_frames = max(1, config.STT_END_SILENCE_MS // self.FRAME_MS)
        self.max_frames = int(config.PHRASE_TIME_LIMIT * 1000 / self.FRAME_MS)
        self.energies = collections.deque(maxlen=int(5000 / self.FRAME_MS))   # ~5 s for the noise floor
        self.reset()

    def reset(self):
        self.triggered = False
        self.frames: list[bytes] = []
        self.voiced = 0
        self.silence_run = 0
        self.recent = collections.deque(maxlen=self.START_WINDOW)
        self.preroll = collections.deque(maxlen=self.PREROLL_MS // self.FRAME_MS)
        self.started_at = None

    def _is_speech(self, frame: bytes) -> bool:
        energy = audioop.rms(frame, 2)
        self.energies.append(energy)
        floor = sorted(self.energies)[len(self.energies) // 5]   # 20th percentile = room noise
        if energy <= max(config.STT_MIN_ENERGY, floor * self.ENERGY_FACTOR):
            return False
        return self.vad.is_speech(frame, self.rate)

    def feed(self, frame: bytes, now: float) -> bytes | None:
        """Add one frame; returns the phrase audio when a phrase has just ended."""
        speech = self._is_speech(frame)
        if not self.triggered:
            self.preroll.append(frame)
            self.recent.append(speech)
            if sum(self.recent) >= self.START_VOICED:
                self.triggered = True
                self.frames = list(self.preroll)
                self.voiced = sum(self.recent)
                self.silence_run = 0
                self.started_at = now - len(self.frames) * self.FRAME_MS / 1000
            return None

        self.frames.append(frame)
        if speech:
            self.voiced += 1
            self.silence_run = 0
        else:
            self.silence_run += 1
        if self.silence_run >= self.end_frames or len(self.frames) >= self.max_frames:
            return self.finish()
        return None

    def finish(self) -> bytes | None:
        """End the current phrase now (returns None if it was too short to be speech)."""
        if not self.triggered:
            return None
        extra_silence = max(0, self.silence_run - self.TRAIL_KEEP_MS // self.FRAME_MS)
        audio = b"".join(self.frames[:len(self.frames) - extra_silence])
        long_enough = self.voiced * self.FRAME_MS >= self.MIN_VOICED_MS
        self.reset()
        return audio if long_enough else None


class _MicGate:
    """Energy-threshold fallback: wraps the microphone stream for SpeechRecognition's listen().

    - While is_muted() (DIGIDAW speaking) it returns silence.
    - The threshold only learns from quiet audio (SpeechRecognition 3.17 also adapts during speech,
      which cuts off the quieter end of sentences), except when sound stays loud for several seconds
      without a pause (music, TV), where it may rise so phrases can still end.
    """

    SUSTAINED_NOISE_SECONDS = 6.0

    def __init__(self, stream, recognizer, is_muted, sample_rate, chunk):
        self._stream = stream
        self._rec = recognizer
        self._is_muted = is_muted
        recognizer.dynamic_energy_threshold = False
        self._seconds_per_chunk = chunk / float(sample_rate)
        self._damping = recognizer.dynamic_energy_adjustment_damping ** self._seconds_per_chunk
        self._ratio = recognizer.dynamic_energy_ratio
        self._loud_for = 0.0

    def read(self, size):
        data = self._stream.read(size)
        if self._is_muted():
            self._loud_for = 0.0
            return bytes(len(data))
        energy = audioop.rms(data, 2)
        rec = self._rec
        if energy > rec.energy_threshold:
            self._loud_for += self._seconds_per_chunk
            if self._loud_for < self.SUSTAINED_NOISE_SECONDS:
                return data   # someone is talking: leave the threshold alone
        else:
            self._loud_for = 0.0
        target = energy * self._ratio
        rec.energy_threshold = max(config.STT_MIN_ENERGY,
                                   rec.energy_threshold * self._damping + target * (1 - self._damping))
        return data

    def close(self):
        self._stream.close()


def _preferred_input(p) -> dict | None:
    """Linux: use PipeWire's own ALSA device. Through ALSA's "default" device, 16 kHz capture with the
    480-frame VAD buffer corrupts memory in PortAudio and crashes Digidaw; "pipewire" resamples safely."""
    if not sys.platform.startswith("linux"):
        return None
    devices = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
    for name in ("pipewire", "pulse"):
        for d in devices:
            if d.get("name") == name and d.get("maxInputChannels", 0) > 0:
                return d
    return None


class SpeechToText:
    def __init__(self, language=None):
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = config.STT_PAUSE_THRESHOLD
        self.recognizer.operation_timeout = 7    # never hang forever on a slow network
        self.language = language or config.STT_LANGUAGE
        self.microphone = None
        self.device_index = None
        self.is_available = False
        self.device_name = "None"
        self.mode = "energy"
        self.vad_microphone = None
        self.phrase_in_progress_since = None     # set while the user is mid-phrase (VAD mode)
        self.last_frame_at = 0.0                 # the microphone last delivered sound (VAD mode)
        self.phrases_heard = 0
        self._capture_gen = 0                    # a stalled capture thread is replaced; the old one then stops
        self._segmenter = None
        self._running = threading.Event()
        self._phrases: queue.Queue = queue.Queue()
        with _device_scan_quiet():
            self._init_microphone()

    def _init_microphone(self):
        """Safely find and initialize a working microphone."""
        # First test PyAudio device availability
        p = None
        working_index = None

        try:
            p = pyaudio.PyAudio()
            # Try to test default first
            try:
                def_info = _preferred_input(p) or p.get_default_input_device_info()
                def_idx = def_info.get("index")
                stream = p.open(format=pyaudio.paInt16, channels=1, rate=int(def_info.get('defaultSampleRate', 16000)), input=True, input_device_index=def_idx, frames_per_buffer=1024)
                stream.close()
                working_index = def_idx
                self.device_name = def_info.get("name", "Default Microphone")
            except Exception:
                pass

            # If default failed, scan other input devices
            if working_index is None:
                for i in range(p.get_device_count()):
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        try:
                            stream = p.open(format=pyaudio.paInt16, channels=1, rate=int(info.get('defaultSampleRate', 16000)), input=True, input_device_index=i, frames_per_buffer=1024)
                            stream.close()
                            working_index = i
                            self.device_name = info.get("name", f"Microphone {i}")
                            break
                        except Exception:
                            continue

            # VAD needs 16 kHz audio; most Windows drivers resample for us
            if working_index is not None and webrtcvad and config.STT_USE_VAD:
                try:
                    stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                                    input_device_index=working_index, frames_per_buffer=480)
                    stream.close()
                    self.mode = "vad"
                except Exception:
                    self.mode = "energy"
        except Exception as e:
            print(f"[Microphone Init Error] {e}")
        finally:
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass

        # If a working device was found, initialize SpeechRecognition Microphone
        if working_index is not None:
            try:
                mic = _keep_portaudio(sr.Microphone(device_index=working_index))
                with mic as source:
                    self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                self.microphone = mic
                self.device_index = working_index
                self.is_available = True
                if self.mode == "vad":
                    self.vad_microphone = _keep_portaudio(sr.Microphone(device_index=working_index, sample_rate=16000,
                                                                        chunk_size=480))
            except Exception:
                self.microphone = None
                self.is_available = False
        else:
            self.microphone = None
            self.is_available = False

    # ───────────────────────── continuous listening ─────────────────────────

    def start_background(self, on_text, is_muted=lambda: False) -> bool:
        """Listen continuously. on_text(text, started, ended) runs on the recognition thread for
        every recognized utterance; started/ended are wall-clock times of the captured audio."""
        if not self.is_available or self._running.is_set():
            return False
        self._running.set()
        capture = self._capture_loop_vad if self.mode == "vad" else self._capture_loop_energy
        threading.Thread(target=capture, args=(is_muted,), daemon=True, name="stt-capture").start()
        threading.Thread(target=self._recognize_loop, args=(on_text,), daemon=True, name="stt-recognize").start()
        if self.mode == "vad":
            threading.Thread(target=self._watch_mic, args=(is_muted,), daemon=True, name="stt-mic-watch").start()
        return True

    MIC_STALL_SECONDS = 8          # no sound at all from the microphone this long: its stream is stuck
    MIC_LOUD_LEVEL = 26000         # the limit is 32767: at 142% Google missed the name in 38% of calls instead of 11%
    MIC_SILENT_LEVEL = 3           # a real room is never this quiet: the microphone only sends zeros (muted?)

    def mic_report(self) -> str:
        """'level ruangan 40–2100, 3 frasa' for the log: is the microphone really hearing anything?"""
        seg = self._segmenter
        levels = sorted(seg.energies) if seg and seg.energies else []
        if not levels:
            return "belum ada suara dari mikrofon"
        loud = " (mikrofon terlalu keras: suara bisa pecah, cek volumenya)" if levels[-1] >= self.MIC_LOUD_LEVEL else ""
        return f"level ruangan {levels[len(levels) // 5]}–{levels[-1]}, {self.phrases_heard} frasa tertangkap{loud}"

    def _watch_mic(self, is_muted):
        """A microphone that stops delivering sound (its stream stuck after a device change) or delivers only
        silence used to leave Digidaw deaf without a word. Now it says so, and a stuck stream is opened again."""
        import events
        started, told_level, told_stall, reported = time.time(), False, False, False
        last_tick = started
        while self._running.is_set():
            time.sleep(2)
            now = time.time()
            if now - last_tick > 30:             # the laptop slept (suspend): every stream starts over quietly
                last_tick = self.last_frame_at = now
                print("[STT] laptop bangun dari tidur: mikrofon diperiksa lagi")
                continue
            last_tick = now
            if not reported and now - started > 20:
                reported = True
                print(f"[STT] mikrofon: {self.mic_report()}")
            stalled_for = now - (self.last_frame_at or started)
            if stalled_for > (self.MIC_STALL_SECONDS if self.last_frame_at else 20):   # (opening takes a moment)
                if not told_stall:
                    told_stall = True
                    print(f"[STT] mikrofon tidak mengirim suara sejak {stalled_for:.0f} dtk: dibuka ulang")
                    import metrics
                    metrics.count("mikrofon_macet")
                    events.emit("alert", text="Mikrofon tidak mengirim suara; sedang dibuka ulang.", level="warning",
                                push=False)
                self.last_frame_at = now             # (one new try per MIC_STALL_SECONDS)
                self._capture_gen += 1
                try:                                 # a new microphone object: the stuck one is still "in use"
                    self.vad_microphone = _keep_portaudio(sr.Microphone(device_index=self.device_index,
                                                                        sample_rate=16000, chunk_size=480))
                except Exception as e:
                    print(f"[STT] mikrofon tidak bisa dibuka ulang: {e}")
                    continue
                threading.Thread(target=self._capture_loop_vad, args=(is_muted,), daemon=True,
                                 name="stt-capture").start()
                continue
            if told_stall and stalled_for < 2:
                told_stall = False
                print("[STT] mikrofon mengirim suara lagi")
            seg = self._segmenter
            if seg and len(seg.energies) == seg.energies.maxlen and not is_muted():
                loudest = max(seg.energies)
                if loudest <= self.MIC_SILENT_LEVEL and not told_level:
                    told_level = True
                    print("[STT] mikrofon hanya mengirim keheningan (mati / di-mute?): Digidaw tidak bisa mendengar")
                    events.emit("alert", text="Mikrofon hanya mengirim keheningan. Cek apakah mic di-mute.",
                                level="warning", push=False)
                elif loudest > self.MIC_SILENT_LEVEL * 4:
                    told_level = False

    def stop_background(self):
        self._running.clear()

    _recording = None

    def record_for(self, seconds: float) -> bytes:
        """The microphone for this long; while a lesson runs (until stop_recording) no phrase is formed from what is said."""
        self._recording = []
        time.sleep(seconds)
        heard, self._recording = self._recording, []
        return b"".join(heard)

    def stop_recording(self):
        self._recording = None

    def _capture_loop_vad(self, is_muted):
        segmenter = _VadSegmenter(16000)
        self._segmenter = segmenter
        frame_samples = segmenter.frame_samples
        highpass = _HighPass(config.STT_HIGHPASS_HZ, 16000) if config.STT_HIGHPASS_HZ else None
        gen = self._capture_gen
        while self._running.is_set() and gen == self._capture_gen:
            try:
                with self.vad_microphone as source:
                    if source.stream is None:
                        raise RuntimeError("mikrofon tidak bisa dibuka")
                    while self._running.is_set():
                        frame = source.stream.read(frame_samples)
                        now = time.time()
                        if gen != self._capture_gen:
                            return                        # this stream got stuck and was replaced meanwhile
                        self.last_frame_at = now
                        if highpass:
                            frame = highpass(frame)
                        if is_muted():
                            segmenter.reset()             # DIGIDAW is talking: hear nothing
                            self.phrase_in_progress_since = None
                            continue
                        if self._recording is not None:   # a lesson in saying the name: nothing else is listened to meanwhile
                            self._recording.append(frame)
                            segmenter.reset()
                            self.phrase_in_progress_since = None
                            continue
                        name_spotter.spotter.feed(frame, now)
                        audio = segmenter.feed(frame, now)
                        self.phrase_in_progress_since = segmenter.started_at if segmenter.triggered else None
                        if audio:
                            duration = len(audio) / 32000
                            self.phrases_heard += 1
                            self._phrases.put((sr.AudioData(audio, 16000, 2), now - duration, now))
            except Exception as e:
                self.phrase_in_progress_since = None
                if self._running.is_set():
                    print(f"[Microphone Error] {e} — mencoba lagi...")
                    time.sleep(2)

    def _capture_loop_energy(self, is_muted):
        while self._running.is_set():
            try:
                with self.microphone as source:
                    if source.stream is None:
                        raise RuntimeError("mikrofon tidak bisa dibuka")
                    source.stream = _MicGate(source.stream, self.recognizer, is_muted,
                                             source.SAMPLE_RATE, source.CHUNK)
                    while self._running.is_set():
                        try:
                            audio = self.recognizer.listen(source, timeout=1,
                                                           phrase_time_limit=config.PHRASE_TIME_LIMIT)
                        except sr.WaitTimeoutError:
                            continue
                        ended = time.time()
                        duration = len(audio.frame_data) / float(audio.sample_rate * audio.sample_width)
                        self._phrases.put((audio, ended - duration, ended))
            except Exception as e:
                if self._running.is_set():
                    print(f"[Microphone Error] {e} — mencoba lagi...")
                    time.sleep(2)

    def _google(self, audio):
        """Google's raw answer for one phrase, like recognize_google(show_all=True) but over the kept connection.
        Raises sr.UnknownValueError (nothing understood) / sr.RequestError (no connection) like the library."""
        from speech_recognition.recognizers import google
        builder = google.create_request_builder(endpoint=GOOGLE_STT_URL, language=self.language)
        url, data, headers = builder.build_url(), builder.build_data(audio), builder.build_headers(audio)
        for attempt in (1, 2):
            try:
                response = _google_session.post(url, data=data, headers=headers,
                                                timeout=self.recognizer.operation_timeout)
                break
            except requests.ConnectionError as e:
                # Google closes a connection that sat idle; the first request after that finds it dead
                # ("Remote end closed connection"). That's no reason to think the internet is gone: once more, fresh.
                if attempt == 2:
                    raise sr.RequestError(f"recognition connection failed: {e}")
            except requests.RequestException as e:
                raise sr.RequestError(f"recognition connection failed: {e}")
        if response.status_code >= 400:
            raise sr.RequestError(f"recognition request failed: {response.status_code} {response.reason}")
        return google.OutputParser(show_all=True, with_confidence=False).parse(response.text)

    def guesses(self, audio) -> list[str]:
        """All of Google's readings of a phrase, best first; the laptop's own reading when it can't be reached."""
        try:
            result = self._google(audio)
            self._set_online(True)
            return [a.get("transcript", "").strip() for a in result.get("alternative", [])] if isinstance(result, dict) else []
        except sr.UnknownValueError:
            return []
        except (sr.RequestError, OSError) as e:
            self._set_online(False, e)
        import local_stt
        return [text for text in [local_stt.speech_in(audio)] if text]    # (not the prompted reading: that one "hears" the name in an echo)

    def _recognize(self, audio) -> str:
        """Best transcript — but if any of Google's alternatives contains the assistant's name, use
        that one (Google's top guess usually "corrects" an unusual name like Digidaw to "digital")."""
        try:
            result = self._google(audio)
            self._set_online(True)          # Google answered (even "nothing recognised" means we're online)
            alternatives = [a.get("transcript", "").strip() for a in result.get("alternative", [])] \
                if isinstance(result, dict) else []
            self._guesses.items = alternatives
            if not alternatives:
                return ""
            name = config.WAKE_WORDS[0].lower()
            named = next((a for a in alternatives if name in a.lower()), None)
            return named or self._understood_guess(alternatives)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            self._set_online(False, e)
            return ""
        except Exception:
            return ""

    def _understood_guess(self, guesses: list[str]) -> str:
        """Which of Google's guesses to use: see hearing.py. Whatever goes wrong there, Google's first choice stands."""
        chosen = guesses[0]
        if self.choose_guess and len(guesses) > 1:
            try:
                chosen = self.choose_guess(guesses) or chosen
            except Exception as e:
                diag.warn_once("stt.guess", "tebakan Google gagal dipilih", e)
        if chosen != guesses[0]:
            print(f"[STT] tebakan lain dipakai: \"{chosen}\" (pilihan pertama Google: \"{guesses[0]}\")")
            try:
                import metrics
                metrics.count("tebakan_lain")
            except Exception:
                pass
        return chosen

    def _user_kept_talking(self, ended: float, next_started: float | None = None, gap: float | None = None) -> bool:
        """Did a new phrase start within STT_MERGE_GAP (or `gap`) of this one ending? (mid-sentence pause)"""
        gap = config.STT_MERGE_GAP if gap is None else gap
        if next_started is not None:
            return next_started - ended <= gap
        if not self._phrases.empty():
            return self._phrases.queue[0][1] - ended <= gap
        started = self.phrase_in_progress_since
        return started is not None and started - ended <= gap

    online = True
    on_connectivity = None     # set by core: called with True/False when internet comes back / drops
    needs_full_text = staticmethod(lambda: False)   # set by core: Digidaw is listening (armed / follow-up / brief)
    media_playing = staticmethod(lambda: False)     # set by core: short phrases may be "pause" / "next"
    # set by core, so Digidaw reacts before Google has answered (~0.5-1 s, at times much more):
    on_wake_hint = None        # fn(local_text) -> bool: the laptop heard the name; True = it already chimed
    on_wake_unconfirmed = None  # fn(): ...but Google heard something else: undo the chime
    on_phrase_for_us = None    # fn(): a phrase Digidaw is waiting for goes to Google (warm the voice up)
    choose_guess = None        # fn(guesses) -> the one to use (hearing.best_guess)
    _guesses = threading.local()    # all of Google's guesses for the phrase this thread just sent
    gated = 0                  # phrases that stayed on this PC (privacy mode)

    @staticmethod
    def _call(fn, *args):
        try:
            return fn(*args) if fn else None
        except Exception as e:
            print(f"[STT callback error] {e}")
            return None

    @staticmethod
    def _tail(audio, duration: float):
        """Of a long phrase nobody is waiting for, the laptop reads the end: reading all of it held up the next one (a call came out
        8 s late), and a call within a long sound is mostly what ends it."""
        if duration <= GATE_TAIL_SECONDS:
            return audio
        keep = int(GATE_TAIL_SECONDS * audio.sample_rate) * audio.sample_width
        return sr.AudioData(audio.get_raw_data()[-keep:], audio.sample_rate, audio.sample_width)

    @staticmethod
    def _with_spot(text: str, spot: float, duration: float) -> str:
        """The laptop heard the name in the sound, and Google's words don't have it. Alone: the call. Before something that starts like a command:
        the name was misheard. Otherwise the words stand. An alias, not the name: what follows still has to look like a command."""
        import core
        if not name_spotter.spotter.sure(spot) or core.detect_wake(text)[0] or not config.WAKE_ALIASES:
            return text
        metrics.count("nama_tertolong")
        print(f"[nama] nama terdengar (skor {spot:.2f}, {duration:.1f} dtk) walau Google menulis lain")
        alias = config.WAKE_ALIASES[0]
        if not text.strip() or len(text.split()) <= 2 and WAKE_LENGTH[0] <= duration <= WAKE_LENGTH[1] + 0.4 and not core.COMMAND_START_RE.match(text):
            return alias
        return f"{alias} {text}" if core.COMMAND_START_RE.match(text) and len(text.split()) <= 8 else text

    def _recognize_smart(self, audio, duration: float, spot: float = 0.0) -> str:
        """Local-first recognition: see local_stt. Falls back to Google-only if no local model."""
        import local_stt
        if not self.online and config.OFFLINE_STT and local_stt.available(local_stt.OFFLINE_MODEL):
            text = local_stt.transcribe(audio, local_stt.OFFLINE_MODEL)
            if text:
                print(f"[STT] (offline, dikenali di laptop) {text}")
            return text
        chimed = False
        local = None
        expected = self.needs_full_text() or name_spotter.spotter.sure(spot)
        self._guesses.items = []
        if config.STT_PRIVACY_MODE and local_stt.available(local_stt.GATE_MODEL) and not expected:
            try:
                local = local_stt.transcribe(self._tail(audio, duration), local_stt.GATE_MODEL)
            except Exception as e:
                print(f"[STT lokal] {e}")
                local = None
            if local is not None:
                short_media = duration <= 2.5 and self.media_playing()
                wake = local_stt.mentions_wake(local)
                if not wake and not short_media:
                    self.gated += 1
                    return local          # not for Digidaw: stays on this PC, Digidaw ignores it
                if wake and WAKE_LENGTH[0] <= duration <= WAKE_LENGTH[1]:
                    chimed = bool(self._call(self.on_wake_hint, local))
        elif expected:
            self._call(self.on_phrase_for_us)
        text = self._with_spot(confirm_alias(local, self._recognize(audio)), spot, duration)
        if WAKE_LENGTH[0] <= duration <= WAKE_LENGTH[1] and sounds_like_name(text):
            metrics.timing("skor_nama_panggilan", spot)      # how the detector does on real calls (it only watches until it has learned)
        # the early chime stands when Google heard the name or something like it ("pigi chord"); nothing at all or
        # other words means the laptop's recognizer imagined it in noise / a song (it chimed for "tulang lunak")
        if chimed and not any(sounds_like_name(g) for g in [text, *getattr(self._guesses, "items", [])]):
            self._call(self.on_wake_unconfirmed)
        if not text and not self.online and config.OFFLINE_STT and local_stt.available(local_stt.OFFLINE_MODEL):
            text = local_stt.transcribe(audio, local_stt.OFFLINE_MODEL)   # Google just failed: use the local one
            if text:
                print(f"[STT] (offline, dikenali di laptop) {text}")
        return text

    def _probe_until_online(self):
        import local_stt
        while not self.online:
            time.sleep(10)
            if local_stt.internet_ok():
                self._set_online(True)

    def _set_online(self, online: bool, error=None):
        if online == self.online:
            return
        self.online = online
        if online:
            print("[STT] Internet tersambung lagi; pengenalan suara normal.")
            import local_stt
            local_stt.unload(local_stt.OFFLINE_MODEL)   # ~0.5 GB that was only needed while offline
        else:
            print(f"[STT] Tidak ada internet untuk pengenalan suara ({error}). Memakai pengenalan di laptop…")
            threading.Thread(target=self._probe_until_online, daemon=True, name="stt-probe").start()
        if self.on_connectivity:
            try:
                self.on_connectivity(online)
            except Exception as e:
                diag.warn_once("stt.connectivity", "kabar internet putus/tersambung gagal diteruskan", e)

    max_phrase_seconds = 0.0   # >0 while Digidaw is paused: longer phrases are dropped without being sent anywhere

    def _recognize_loop(self, on_text):
        """Phrases are sent to Google in parallel (one slow request no longer holds up the next
        phrase), and delivered strictly in the order they were spoken."""
        pending = None                  # (text, started, ended) held back while the user is still talking
        inflight = collections.deque()  # (future, started, ended), oldest first
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="stt-google")
        while self._running.is_set():
            try:
                audio, started, ended = self._phrases.get(timeout=0.03)
                stale = time.time() - ended > 20 and not pending and not inflight
                too_long = self.max_phrase_seconds and ended - started > self.max_phrase_seconds
                if not stale and not too_long:
                    spot = name_spotter.spotter.score_during(started, ended)
                    inflight.append((pool.submit(self._recognize_smart, audio, ended - started, spot), started, ended))
            except queue.Empty:
                pass
            while inflight and inflight[0][0].done():
                future, started, ended = inflight.popleft()
                text = future.result()
                if not text and config.LOG_OVERHEARD:
                    print(f"· frasa {ended - started:.1f} dtk tidak terbaca")
                if text:
                    try:
                        import presence
                        presence.note_voice(ended)    # someone talks in the room (for the lamp, not for "here")
                    except Exception:
                        pass
                if pending:
                    pending = (f"{pending[0]} {text}".strip(), pending[1], ended)
                elif text:
                    pending = (text, started, ended)
                gap = DANGLING_HOLD if pending and dangling(pending[0]) else None
                if pending and inflight and not self._user_kept_talking(pending[2], inflight[0][1], gap):
                    self._deliver(on_text, pending)   # the next phrase is a new sentence
                    pending = None
            if pending and not inflight:
                unfinished = dangling(pending[0])
                if not self._user_kept_talking(pending[2], gap=DANGLING_HOLD if unfinished else None) and not (
                        unfinished and time.time() - pending[2] < DANGLING_HOLD):
                    self._deliver(on_text, pending)   # ("lebih" … "terang": one sentence, said with a pause)
                    pending = None
        pool.shutdown(wait=False)

    @staticmethod
    def _deliver(on_text, item):
        try:
            on_text(*item)
        except Exception as e:
            print(f"[STT callback error] {e}")

    # ───────────────────────── single phrase (legacy) ─────────────────────────

    def listen(self, timeout=6, phrase_time_limit=8) -> str:
        """
        Listen to microphone and return transcribed text.
        Returns empty string if timed out or microphone unavailable.
        """
        if not self.is_available or not self.microphone:
            return ""

        try:
            with self.microphone as source:
                audio = self.recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit
                )
            text = self.recognizer.recognize_google(audio, language=self.language)
            return text.strip()
        except sr.WaitTimeoutError:
            return ""
        except sr.UnknownValueError:
            return ""
        except Exception:
            return ""


# Global instance
stt_engine = SpeechToText()


def listen_command(timeout=6, phrase_time_limit=8) -> str:
    return stt_engine.listen(timeout=timeout, phrase_time_limit=phrase_time_limit)


def start_background(on_text, is_muted=lambda: False) -> bool:
    return stt_engine.start_background(on_text, is_muted)


def stop_background():
    stt_engine.stop_background()


def is_mic_working() -> bool:
    return stt_engine.is_available
