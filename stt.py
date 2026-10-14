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
import sys
import time
import queue
import warnings
import threading
import collections
from concurrent.futures import ThreadPoolExecutor

import pyaudio
import speech_recognition as sr

import config

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

class _VadSegmenter:
    """Turns a stream of 30 ms frames into phrases using WebRTC VAD + a relative energy gate."""

    FRAME_MS = 30
    START_WINDOW, START_VOICED = 5, 3      # 3 voiced frames out of the last 5 (~90 ms) starts a phrase
    PREROLL_MS = 300                       # audio kept from just before speech started
    TRAIL_KEEP_MS = 300                    # silence kept after the last word (helps recognition)
    MIN_VOICED_MS = 240                    # shorter blips (clicks, coughs) are dropped
    ENERGY_FACTOR = 1.8                    # a speech frame must also be this much louder than the room

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
        return True

    def stop_background(self):
        self._running.clear()

    def _capture_loop_vad(self, is_muted):
        segmenter = _VadSegmenter(16000)
        frame_samples = segmenter.frame_samples
        while self._running.is_set():
            try:
                with self.vad_microphone as source:
                    if source.stream is None:
                        raise RuntimeError("mikrofon tidak bisa dibuka")
                    while self._running.is_set():
                        frame = source.stream.read(frame_samples)
                        now = time.time()
                        if is_muted():
                            segmenter.reset()             # DIGIDAW is talking: hear nothing
                            self.phrase_in_progress_since = None
                            continue
                        audio = segmenter.feed(frame, now)
                        self.phrase_in_progress_since = segmenter.started_at if segmenter.triggered else None
                        if audio:
                            duration = len(audio) / 32000
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

    def _recognize(self, audio) -> str:
        """Best transcript — but if any of Google's alternatives contains the assistant's name, use
        that one (Google's top guess usually "corrects" an unusual name like Digidaw to "digital")."""
        try:
            result = self.recognizer.recognize_google(audio, language=self.language, show_all=True)
            self._set_online(True)          # Google answered (even "nothing recognised" means we're online)
            alternatives = [a.get("transcript", "") for a in result.get("alternative", [])] \
                if isinstance(result, dict) else []
            if not alternatives:
                return ""
            name = config.WAKE_WORDS[0].lower()
            return next((a for a in alternatives if name in a.lower()), alternatives[0]).strip()
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            self._set_online(False, e)
            return ""
        except Exception:
            return ""

    def _user_kept_talking(self, ended: float, next_started: float | None = None) -> bool:
        """Did a new phrase start within STT_MERGE_GAP of this one ending? (mid-sentence pause)"""
        if next_started is not None:
            return next_started - ended <= config.STT_MERGE_GAP
        if not self._phrases.empty():
            return self._phrases.queue[0][1] - ended <= config.STT_MERGE_GAP
        started = self.phrase_in_progress_since
        return started is not None and started - ended <= config.STT_MERGE_GAP

    online = True
    on_connectivity = None     # set by core: called with True/False when internet comes back / drops
    needs_full_text = staticmethod(lambda: False)   # set by core: Digidaw is listening (armed / follow-up / brief)
    media_playing = staticmethod(lambda: False)     # set by core: short phrases may be "pause" / "next"
    gated = 0                  # phrases that stayed on this PC (privacy mode)

    def _recognize_smart(self, audio, duration: float) -> str:
        """Local-first recognition: see local_stt. Falls back to Google-only if no local model."""
        import local_stt
        if not self.online and config.OFFLINE_STT and local_stt.available(local_stt.OFFLINE_MODEL):
            text = local_stt.transcribe(audio, local_stt.OFFLINE_MODEL)
            if text:
                print(f"[STT] (offline, dikenali di laptop) {text}")
            return text
        if config.STT_PRIVACY_MODE and local_stt.available(local_stt.GATE_MODEL) and not self.needs_full_text():
            try:
                local = local_stt.transcribe(audio, local_stt.GATE_MODEL)
            except Exception as e:
                print(f"[STT lokal] {e}")
                local = None
            if local is not None:
                short_media = duration <= 2.5 and self.media_playing()
                if not local_stt.mentions_wake(local) and not short_media:
                    self.gated += 1
                    return local          # not for Digidaw: stays on this PC, Digidaw ignores it
        text = self._recognize(audio)
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
        else:
            print(f"[STT] Tidak ada internet untuk pengenalan suara ({error}). Memakai pengenalan di laptop…")
            threading.Thread(target=self._probe_until_online, daemon=True, name="stt-probe").start()
        if self.on_connectivity:
            try:
                self.on_connectivity(online)
            except Exception:
                pass

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
                    inflight.append((pool.submit(self._recognize_smart, audio, ended - started), started, ended))
            except queue.Empty:
                pass
            while inflight and inflight[0][0].done():
                future, started, ended = inflight.popleft()
                text = future.result()
                if pending:
                    pending = (f"{pending[0]} {text}".strip(), pending[1], ended)
                elif text:
                    pending = (text, started, ended)
                if pending and inflight and not self._user_kept_talking(pending[2], inflight[0][1]):
                    self._deliver(on_text, pending)   # the next phrase is a new sentence
                    pending = None
            if pending and not inflight and not self._user_kept_talking(pending[2]):
                self._deliver(on_text, pending)
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
