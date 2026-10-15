"""
Voice output.

Engine chain: Microsoft Edge neural voices (British "Ryan" for English, "Ardi" for Indonesian)
-> Google gTTS -> offline Windows SAPI (espeak-ng on Linux).

Speed matters here:
- The Edge voice is *streamed*: playback starts as soon as ~0.25 s of audio has arrived instead of
  after the whole sentence is downloaded.
- Connections to the Edge service are sometimes reset instantly by the network; such failures take
  ~0.1 s, so they are retried a few times before falling back to another engine.
- Long replies are split into sentences; later sentences are downloaded while the first one plays.
- Short phrases are cached, so frequent replies play instantly.

Speech can be interrupted with stop(), and before/after hooks let the media engine "duck" other
apps' audio while DIGIDAW talks.
"""
import io
import os
import re
import time
import uuid
import queue
import asyncio
import contextlib
import hashlib
import threading
import unicodedata
import subprocess
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

import config
import events

AUDIO_DIR = config.AUDIO_DIR
CACHE_DIR = AUDIO_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SPEAK_SCRIPT = config.BASE_DIR / "speak.ps1"
CACHE_MAX_CHARS = 90
CACHE_MAX_FILES = 1500        # ~10 KB each; frequent replies then always play instantly
EDGE_MAX_ATTEMPTS = 5       # instant connection resets are retried this many times (IPv6 path)
EDGE_FIRST_AUDIO_DEADLINE = 4.0   # no Edge audio after this long -> use the fallback voice
EDGE_BREAK_SECONDS = 90           # after an Edge failure, sentences skip Edge for this long...
EDGE_PROBE_EVERY = 30             # ...while a background check retries it every 30 s
PIPER_MODEL = config.BASE_DIR / "models" / "piper" / "id_ID-news_tts-medium.onnx"   # local fallback voice

ID_WORDS = {
    "tuan", "saya", "selamat", "apakah", "bisa", "adalah", "ini", "itu", "dan", "di", "ke",
    "dari", "yang", "tidak", "ya", "sudah", "lagi", "buka", "cari", "kunci", "mati", "suara",
    "jam", "hari", "sekarang", "ada", "apa", "halo", "terima", "kasih", "bagaimana", "sistem",
    "sedang", "akan", "untuk", "dengan", "baik", "sudah", "memutar", "membuka", "menit", "pukul",
    "baterai", "anda", "sampai", "jumpa", "siap", "maaf", "dibatalkan", "tersimpan", "catatan",
    "sebentar", "satu", "momen", "proses",
}


ID_STARTS = {"oke", "siap", "baik", "beres", "cuaca", "alarm", "timer", "volume", "tab", "jendela", "lagu", "musik",
             "hai", "hmm", "wah", "aduh", "waduh", "iya", "tidak", "belum", "ini", "itu", "sekarang", "semua", "kecerahan"}
EN_WORDS = {"the", "is", "are", "was", "were", "you", "your", "i", "i'm", "my", "me", "we", "it", "it's", "this",
            "that", "with", "for", "of", "to", "and", "a", "an", "in", "on", "what", "how", "why", "have", "has", "be",
            "will", "can", "do", "does", "not", "please", "here", "there", "sir", "good", "morning", "evening",
            "playing", "opening", "closing", "done", "sure", "okay", "thanks", "thank", "hello"}
ID_AFFIX = re.compile(r"^(?:me|mem|men|meng|meny|ber|ter|di|ke|pe|pem|pen|peng)\w{3,}|\w{3,}(?:kan|nya|lah|an)$")


def is_indonesian(text: str) -> bool:
    """Indonesian unless the text is clearly English: Digidaw mostly speaks Indonesian, and an Indonesian sentence
    read by the English voice ("Beres, Firefox ditutup.") sounds far worse than the reverse. Names and titles
    ("Arctic Monkeys - Do I Wanna Know?") inside an Indonesian sentence don't make it English."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return True
    if words[0] in ID_WORDS or ID_AFFIX.match(words[0]) or words[0] in ID_STARTS:
        return True             # "Memutar Arctic Monkeys - Do I Wanna Know?": an Indonesian sentence with a title in it
    indonesian = sum(1 for w in words if w in ID_WORDS or ID_AFFIX.match(w))
    english = sum(1 for w in words if w in EN_WORDS)
    return not (english >= 2 and english > indonesian * 1.5)


def clean_for_speech(text: str) -> str:
    """Remove things that sound awful when read aloud (markdown, URLs, full file paths, emoji)."""
    text = re.sub(r"(?<=\d)\s*[*×]\s*(?=\d)", " kali " if is_indonesian(text) else " times ", text)
    text = text.replace("*", "").replace("#", "").replace("`", "")
    text = re.sub(r"https?://\S+", "tautan", text)
    text = re.sub(r"[A-Za-z]:\\[^\s,;]*", lambda m: Path(m.group(0).rstrip(".")).name or "folder", text)
    text = re.sub(r"(?<![\w/])(?:~|/(?:home|mnt|media|tmp|usr|opt|etc|var|run))/[^\s,;]*",
                  lambda m: Path(m.group(0).rstrip(".")).name or "folder", text)
    if is_indonesian(text):
        text = speakable(text)          # before the symbol clean-up below: it needs "°"
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Cs", "Co"))
    return re.sub(r"\s+", " ", text).strip()


# ───────────────── how Indonesian text is said out loud ─────────────────
# The voice reads "0:02" as "pukul nol dua", "81/100" as "delapan ratus satu seratus" and "01.43" as "satu koma empat
# tiga"; these rewrite such text into what a person would actually say.
_UNITS = [(r"(\d)\s?GB\b", r"\1 giga"), (r"(\d)\s?MB\b", r"\1 mega"), (r"(\d)\s?TB\b", r"\1 tera"),
          (r"(\d)\s?GHz\b", r"\1 gigahertz"), (r"(\d)\s?km/jam\b", r"\1 kilometer per jam"), (r"(\d)\s?km/h\b", r"\1 kilometer per jam"),
          (r"(\d)\s?km\b", r"\1 kilometer"), (r"(\d)\s?kg\b", r"\1 kilogram"), (r"(\d)\s?cm\b", r"\1 senti")]
_ABBREV = [(r"\bdll\b\.?", "dan lain-lain"), (r"\bdsb\b\.?", "dan sebagainya"), (r"\byg\b", "yang"),
           (r"\btdk\b", "tidak"), (r"\bdgn\b", "dengan"), (r"\butk\b", "untuk"), (r"\bvs\.?\b", "lawan"),
           (r"\bkm/jam\b", "kilometer per jam"), (r"&", " dan ")]
_LAUGH = re.compile(r"[,.]?\s*\b(?:he){2,}\b|\b(?:ha){2,}\b|\bwk(?:wk)+\b", re.IGNORECASE)


def _clock(m: re.Match) -> str:
    """'pukul 05.00' -> 'pukul 5'; '16.45' -> '16 lewat 45'."""
    word, hour, minute = m[1] or "", int(m[2]), int(m[3])
    said = f"{hour}" if minute == 0 else f"{hour} lewat {minute}"
    return f"{word + 'jam ' if word.strip().lower() == 'alarm' else word}{said}"


def _duration(m: re.Match) -> str:
    """A media position '4:44' -> '4 menit 44 detik', '0:02' -> '2 detik'."""
    minutes, seconds = int(m[1]), int(m[2])
    parts = ([f"{minutes} menit"] if minutes else []) + ([f"{seconds} detik"] if seconds or not minutes else [])
    return " ".join(parts)


def speakable(text: str) -> str:
    for pattern, word in _ABBREV + _UNITS:
        text = re.sub(pattern, word, text)
    # clock times: after pukul/jam/alarm, or HH.MM / HH:MM with a two-digit minute
    text = re.sub(r"((?:pukul|jam|alarm|sampai|hingga|sejak|mulai) )?\b([01]?\d|2[0-3])[.:]([0-5]\d)\b(?![.,]?\d)",
                  lambda m: _clock(m) if (m[1] or len(m[0]) >= 4) and not re.match(r"\d:\d\d$", m[0]) else m[0], text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bmenit (?=\d{1,3}:[0-5]\d\b)", "", text)                       # "menit 0:02 dari 4:44"
    text = re.sub(r"\b(\d{1,3}):([0-5]\d)\b", _duration, text)                       # media positions
    text = re.sub(r"\b(\d+)\s?/\s?(10|100)\b", r"\1 dari \2", text)                 # scores: 81/100
    text = re.sub(r"\b(\d+)\s?/\s?(\d+)\b", r"\1 per \2", text)
    if re.search(r"\d\s*=\s*-?\d", text):                                               # a calculation
        text = re.sub(r"(?<=\d)\s+-\s+(?=\d)", " kurang ", text)
        text = re.sub(r"(?<=\d)\s*=\s*(?=-?\d)", " sama dengan ", text)
    text = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", " tambah ", text)                            # "10 + 300"
    text = re.sub(r"(?<![\w])\+\s?(\d)", r"\1", text)                                  # "+21°C" -> "21"
    text = re.sub(r"(?<![\w])-\s?(\d+(?:[.,]\d+)?)\s?°", r"minus \1°", text)
    text = re.sub(r"\s?°\s?C\b", " derajat", text).replace("°", " derajat")
    text = re.sub(r"\s+[—–]\s+|[—–]", ", ", text)                                        # dashes = a short pause
    text = re.sub(r"\s+-\s+", ", ", text)                                                 # "Dewa 19 - Kangen"
    text = re.sub(r"[\u2190-\u21ff\u2b00-\u2bff]", "", text)                             # arrows (wind direction)
    text = _LAUGH.sub("", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([,.!?])\1+", r"\1", text)
    text = re.sub(r"([?!])\.", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


_edge_warned = {"at": 0.0}
EDGE_WARN_EVERY = 600        # seconds between two warnings about the Edge voice


def note_edge_failure(error: Exception):
    """The Edge voice failed and the robotic fallback voice will speak: say why (once in a while), on the console
    and the HUD. A 403 means Microsoft changed its service and edge-tts must be updated."""
    if time.time() - _edge_warned["at"] < EDGE_WARN_EVERY:
        return
    _edge_warned["at"] = time.time()
    text = str(error)
    if "403" in text:
        hint = ("Suara Edge ditolak server (403): Microsoft mengubah layanannya. Perbarui edge-tts: "
                "uv pip install --python ~/.local/share/digidaw/venv/bin/python -U edge-tts")
    else:
        hint = f"Suara Edge gagal ({type(error).__name__}: {text[:80]}); sementara memakai suara cadangan."
    print(f"[TTS] {hint}")
    events.emit("alert", text=hint, level="warning")


class EdgeLink:
    """One Edge voice connection kept open and warm.

    On a fresh connection the service needs ~1.6-2.5 s before the first audio; on a connection that has already
    spoken it needs ~0.6 s. So the connection is opened and warmed up while the user is still talking (prewarm(),
    called when Digidaw is woken), and reused for every sentence after that. Closed again after a quiet minute."""

    IDLE_CLOSE = 75          # seconds without speech before the connection is dropped (the service drops it anyway)

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="edge-link").start()
        self.session = None
        self.ws = None
        self.warm = False
        self.last_used = 0.0
        self.warming = None
        self._lock = asyncio.Lock()
        asyncio.run_coroutine_threadsafe(self._idle_closer(), self.loop)

    def is_warm(self) -> bool:
        return self.warm and self.ws is not None and not self.ws.closed and time.time() - self.last_used < self.IDLE_CLOSE

    async def _close(self):
        ws, session = self.ws, self.session
        self.ws = self.session = None
        self.warm = False
        for thing in (ws, session):
            if thing is not None:
                with contextlib.suppress(Exception):
                    await thing.close()

    async def _connect(self):
        if self.ws is not None and not self.ws.closed:
            return
        await self._close()
        import aiohttp
        from edge_tts import communicate as C
        from edge_tts.drm import DRM
        from edge_tts.constants import WSS_URL, SEC_MS_GEC_VERSION, WSS_HEADERS
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=None))
        self.ws = await asyncio.wait_for(self.session.ws_connect(
            f"{WSS_URL}&ConnectionId={C.connect_id()}&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
            compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=C._SSL_CTX), 6)
        await self.ws.send_str(
            f"X-Timestamp:{C.date_to_string()}\r\nContent-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false",'
            '"wordBoundaryEnabled":"false"},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')

    async def _synth(self, text: str, voice: str, on_chunk, first_timeout: float, should_stop=None):
        import aiohttp
        from edge_tts import communicate as C
        tc = C.TTSConfig(voice, config.EDGE_RATE, "+0%", config.EDGE_PITCH, "SentenceBoundary")
        await self.ws.send_str(C.ssml_headers_plus_data(C.connect_id(), C.date_to_string(),
                                                        C.mkssml(tc, C.escape(C.remove_incompatible_characters(text)))))
        got_audio = False
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), first_timeout if not got_audio else 8)
            if msg.type == aiohttp.WSMsgType.BINARY:
                header_length = int.from_bytes(msg.data[:2], "big")
                _, data = C.get_headers_and_data(msg.data, header_length)
                if data:
                    got_audio = True
                    on_chunk(data)
                if should_stop and should_stop():
                    raise InterruptedError     # the rest of this answer would arrive on the socket: drop it
            elif msg.type == aiohttp.WSMsgType.TEXT:
                if "Path:turn.end" in msg.data:
                    break
            else:
                raise ConnectionError(f"koneksi suara tertutup ({msg.type.name})")
        if not got_audio:
            raise ConnectionError("tidak ada audio dari Edge")

    async def _run(self, text, voice, on_chunk, first_timeout, should_stop, warm_up=False):
        async with self._lock:
            if warm_up and self.is_warm():
                return
            try:
                await self._connect()
                await self._synth(text, voice, on_chunk, first_timeout, should_stop)
                self.warm = True
                self.last_used = time.time()
            except BaseException:
                await self._close()
                raise

    def stream(self, text: str, voice: str, on_chunk, first_timeout: float = 3.0, should_stop=None, timeout=20):
        """Speak over the kept connection (blocking). Raises if it doesn't work: the caller falls back."""
        future = asyncio.run_coroutine_threadsafe(
            self._run(text, voice, on_chunk, first_timeout, should_stop), self.loop)
        return future.result(timeout)

    def prewarm(self):
        """Open + warm the connection in the background (no sound). Cheap when it is already warm."""
        if self.is_warm() or (self.warming and not self.warming.done()):
            return
        self.warming = asyncio.run_coroutine_threadsafe(
            self._run("Ya.", config.EDGE_VOICE_ID, lambda data: None, 6.0, None, warm_up=True), self.loop)

    async def _idle_closer(self):
        while True:
            await asyncio.sleep(10)
            if self.ws is not None and not self._lock.locked() and time.time() - self.last_used > self.IDLE_CLOSE:
                await self._close()


class NeuralVoice:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._channel = None
        self._speak_count = 0
        self.speaking = False
        self.last_activity = 0.0          # last moment audio was coming out of the speakers
        self.before_speak = None          # hooks set by main (media ducking)
        self.after_speak = None
        self.first_audio_delay = None
        self._spoken_at = 0.0
        self.link = EdgeLink()
        self.edge_down_until = 0.0        # circuit breaker: Edge failed, don't make every sentence wait for it
        self._piper = None
        self._probing = False
        self._init_mixer()

    def _init_mixer(self):
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as e:
            print(f"[Pygame Init Warning] {e}")

    # ── state helpers used by the voice loop ──
    def is_speaking(self) -> bool:
        return self.speaking

    def spoke_since(self, t: float) -> bool:
        return self.speaking or self.last_activity >= t

    def is_audible(self, tail: float = 0.3) -> bool:
        """True while DIGIDAW's voice (or its room echo) can reach the microphone."""
        return self.speaking or time.time() - self.last_activity < tail

    def stop(self):
        """Interrupt current speech immediately."""
        self._stop.set()
        try:
            pygame.mixer.music.stop()
            if self._channel:
                self._channel.stop()
        except Exception:
            pass
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    # ── synthesis ──
    def _voice_for(self, text: str) -> str:
        return config.EDGE_VOICE_ID if is_indonesian(text) else config.EDGE_VOICE_EN

    def _cache_path(self, text: str, engine: str) -> Path:
        key = f"{engine}|{self._voice_for(text)}|{config.EDGE_RATE}|{config.EDGE_PITCH}|{text}"
        return CACHE_DIR / f"{hashlib.md5(key.encode('utf-8')).hexdigest()}.mp3"

    def _edge(self, text: str, connector=None):
        import edge_tts
        return edge_tts.Communicate(text, self._voice_for(text), rate=config.EDGE_RATE, pitch=config.EDGE_PITCH,
                                    connector=connector, connect_timeout=5, receive_timeout=15)

    async def _race_stream(self, text: str, on_chunk):
        """Fetch Edge audio over IPv6 and IPv4 at the same time and keep whichever delivers audio
        first. (On some networks IPv6 answers much faster but its connections are often reset
        instantly, while IPv4 always connects but responds slowly.)"""
        import socket
        import aiohttp
        state = {"winner": None}
        tasks: list[asyncio.Task] = []

        async def attempt(family, tries: int) -> bool:
            for _ in range(tries):
                try:
                    stream = self._edge(text, aiohttp.TCPConnector(family=family)).stream()
                    async with contextlib.aclosing(stream):
                        async for chunk in stream:
                            if chunk["type"] != "audio":
                                continue
                            if state["winner"] is None:
                                state["winner"] = family
                                for t in tasks:
                                    if t is not asyncio.current_task():
                                        t.cancel()   # the other path lost the race
                            if self._stop.is_set():
                                return True
                            on_chunk(chunk["data"])
                    if state["winner"] == family:
                        return True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if state["winner"] == family:
                        raise   # broke in the middle of the sentence: can't restart cleanly
                if state["winner"] is not None:
                    return False
                await asyncio.sleep(0.05)
            return False

        tasks.extend([asyncio.create_task(attempt(socket.AF_INET6, EDGE_MAX_ATTEMPTS)),
                      asyncio.create_task(attempt(socket.AF_INET, 2))])
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                raise result
        if not any(r is True for r in results):
            raise ConnectionError("layanan suara Edge tidak terjangkau")

    def _synth_edge(self, text: str, path: Path, timeout: float = 20, use_link: bool = True):
        chunks: list[bytes] = []
        try:
            if not use_link:
                raise LookupError
            self.link.stream(text, self._voice_for(text), chunks.append, timeout=timeout)
        except Exception:
            chunks.clear()
            asyncio.run(asyncio.wait_for(self._race_stream(text, chunks.append), timeout))
        if not chunks:
            raise ConnectionError("tidak ada audio dari Edge")
        path.write_bytes(b"".join(chunks))

    def _synth_gtts(self, text: str, path: Path):
        from gtts import gTTS
        if is_indonesian(text):
            gTTS(text=text, lang="id", slow=False, timeout=6).save(str(path))
        else:
            gTTS(text=text, lang="en", tld="co.uk", slow=False, timeout=6).save(str(path))

    def _synthesize(self, text: str, engines: list[str] | None = None,
                    edge_timeout: float = 20, use_link: bool = True) -> tuple[Path | None, bool]:
        """Download the whole phrase to an mp3. Returns (path, is_cached_file)."""
        if engines is None:
            engines = {"edge": ["edge", "piper", "gtts"], "gtts": ["gtts", "edge"]}.get(config.TTS_ENGINE, [])
        if not self.edge_ok():
            engines = [e for e in engines if e != "edge"] or ["piper", "gtts"]
        cacheable = len(text) <= CACHE_MAX_CHARS
        for engine in engines:
            keep = cacheable and engine == "edge"      # a fallback recording is never kept: Edge gets the next try
            if keep:
                path = self._cache_path(text, engine)
                if path.exists() and path.stat().st_size > 0:
                    return path, True
            else:
                path = AUDIO_DIR / f"speech_{uuid.uuid4().hex[:12]}.{'wav' if engine == 'piper' else 'mp3'}"
            try:
                if engine == "edge":
                    self._synth_edge(text, path, timeout=edge_timeout, use_link=use_link)
                elif engine == "piper":
                    self._synth_piper(text, path)
                else:
                    self._synth_gtts(text, path)
                if path.exists() and path.stat().st_size > 0:
                    if engine != config.TTS_ENGINE:
                        print(f"[TTS] suara cadangan ({engine}) dipakai untuk: {text[:60]}")
                    return path, keep
            except Exception as e:
                if engine == "edge":
                    note_edge_failure(e)
                    self.edge_failed()
                elif engine == "piper":
                    print(f"[TTS] suara lokal Piper gagal: {str(e)[:100]}")
            path.unlink(missing_ok=True)
        return None, False

    # ── circuit breaker for the Edge voice ──
    def edge_ok(self) -> bool:
        return time.time() >= self.edge_down_until

    def edge_failed(self):
        """Edge didn't answer: for a while every sentence goes straight to the fallback voice (no 13 s waits), and
        a background check brings Edge back as soon as it answers again."""
        self.edge_down_until = time.time() + EDGE_BREAK_SECONDS
        if not self._probing:
            self._probing = True
            threading.Thread(target=self._probe_edge, daemon=True, name="tts-edge-probe").start()

    def _probe_edge(self):
        try:
            while True:
                time.sleep(EDGE_PROBE_EVERY)
                test = AUDIO_DIR / f"probe_{uuid.uuid4().hex[:8]}.mp3"
                try:
                    self._synth_edge("Ya.", test, timeout=6, use_link=False)
                    ok = test.exists() and test.stat().st_size > 0
                except Exception:
                    ok = False
                finally:
                    test.unlink(missing_ok=True)
                if ok:
                    self.edge_down_until = 0.0
                    print("[TTS] suara Edge pulih kembali")
                    return
                self.edge_down_until = time.time() + EDGE_BREAK_SECONDS
        finally:
            self._probing = False

    def _piper_voice(self):
        if self._piper is None:
            from piper import PiperVoice
            self._piper = PiperVoice.load(str(PIPER_MODEL))
        return self._piper

    def _synth_piper(self, text: str, path: Path):
        """The fallback voice when Edge is down: Piper, on this laptop (~0.1 s, no internet, no quota). Only for
        Indonesian; English text goes on to the next engine."""
        import wave
        if not is_indonesian(text) or not PIPER_MODEL.exists():
            raise RuntimeError("tidak ada suara Piper untuk teks ini")
        with wave.open(str(path), "wb") as out:
            self._piper_voice().synthesize_wav(text, out)

    def preload_fallback(self):
        """Load the Piper voice in the background, so the first fallback sentence isn't slower."""
        if PIPER_MODEL.exists():
            threading.Thread(target=lambda: self._piper_voice(), daemon=True, name="piper-load").start()

    def preload(self, phrases: list[str]):
        """Warm the cache in the background so these phrases play instantly later."""
        def run():
            for p in phrases:
                p = clean_for_speech(p)
                if len(p) <= CACHE_MAX_CHARS and not self._cache_path(p, "edge").exists():
                    try:
                        self._synth_edge(p, self._cache_path(p, "edge"))
                    except Exception:
                        continue
        threading.Thread(target=run, daemon=True, name="tts-preload").start()

    # ── streaming Edge playback ──
    def _stream_edge(self, text: str) -> bool:
        """Speak `text` with the Edge voice, starting as soon as audio arrives.
        Returns False if no audio could be fetched (the caller then tries another engine)."""
        incoming: queue.Queue = queue.Queue()
        cacheable = len(text) <= CACHE_MAX_CHARS

        async def fetch(on_chunk):
            await asyncio.wait_for(self._race_stream(text, on_chunk), 20)
            incoming.put(None)   # signal the end before asyncio's (slow) teardown

        def producer():
            chunks: list[bytes] = []

            def on_chunk(data: bytes):
                chunks.append(data)
                incoming.put(data)
            try:
                try:
                    self.link.stream(text, self._voice_for(text), on_chunk, should_stop=self._stop.is_set,
                                     first_timeout=2.5 if self.link.is_warm() else 3.5)
                    incoming.put(None)
                except InterruptedError:
                    incoming.put(None)
                    return
                except Exception:
                    if chunks:
                        raise          # broke in the middle of the sentence: can't restart it cleanly
                    try:
                        asyncio.run(fetch(on_chunk))   # the kept connection failed: a fresh one (IPv6/IPv4 race)
                    except Exception as e:
                        note_edge_failure(e)
                        raise
                if cacheable and chunks and not self._stop.is_set():   # the full, finished audio
                    self._cache_path(text, "edge").write_bytes(b"".join(chunks))
            except Exception as e:
                incoming.put(e)
            incoming.put(None)

        threading.Thread(target=producer, daemon=True, name="tts-stream").start()

        freq, _, channels = pygame.mixer.get_init()
        bytes_per_sec = freq * channels * 2
        hold_back = int(0.12 * freq) * channels * 2   # last ~2 mp3 frames change as more data arrives
        mp3, pcm_done, segments, sounds = bytearray(), 0, [], []
        finished = tail_done = False
        decoded_len, last_data = 0, time.time()
        started = time.time()
        warm_at_start = self.link.is_warm()
        self._channel = None

        while True:
            try:
                item = incoming.get(timeout=0.03)
            except queue.Empty:
                item = False
            if not mp3 and not isinstance(item, bytes) and time.time() - started > EDGE_FIRST_AUDIO_DEADLINE:
                return False   # Edge is too slow right now: let the caller use the fallback voice
            if item is None or isinstance(item, Exception):
                finished = True
            elif item:
                mp3 += item
                last_data = time.time()
                tail_done = False

            if self._stop.is_set():
                if self._channel:
                    self._channel.stop()
                return True

            # Edge delivers audio faster than real time, so a short lull means the sentence is
            # complete: play the held-back tail now instead of waiting for the "done" message.
            idle = time.time() - last_data
            flush_tail = finished or idle > 0.25
            if mp3 and (len(mp3) != decoded_len or (flush_tail and not tail_done)):
                decoded_len = len(mp3)
                tail_done = flush_tail
                try:
                    pcm = pygame.mixer.Sound(file=io.BytesIO(bytes(mp3))).get_raw()
                except Exception:
                    pcm = b""   # not enough data to decode yet
                limit = len(pcm) if flush_tail else len(pcm) - hold_back
                pending = limit - pcm_done
                needed = int(0.15 * bytes_per_sec) if self._channel is None else int(0.15 * bytes_per_sec)
                if pending > 0 and (pending >= needed or flush_tail):
                    segments.append(pcm[pcm_done:limit])
                    pcm_done = limit

            if segments:
                if self._channel is None:
                    self.first_audio_delay = time.time() - started
                    if self._spoken_at:
                        print(f"   🔊 suara mulai {time.time() - self._spoken_at:.1f} dtk"
                              f"{' (koneksi hangat)' if warm_at_start else ''}")
                        self._spoken_at = 0.0
                    sound = pygame.mixer.Sound(buffer=segments.pop(0))
                    sounds.append(sound)
                    self._channel = pygame.mixer.find_channel(True)
                    self._channel.play(sound)
                elif self._channel.get_queue() is None:
                    sound = pygame.mixer.Sound(buffer=segments.pop(0))
                    sounds.append(sound)
                    if self._channel.get_busy():
                        self._channel.queue(sound)
                    else:
                        self._channel.play(sound)

            playing = self._channel is not None and self._channel.get_busy()
            if playing:
                self.last_activity = time.time()
            # the "done" message sometimes lags seconds behind the audio: don't keep the mic closed for it
            done = finished or (sounds and idle > 0.6)
            if done and not segments and not playing:
                break
        return bool(sounds)

    # ── playback ──
    def _play(self, path: Path):
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if self._stop.is_set():
                pygame.mixer.music.stop()
                break
            self.last_activity = time.time()
            time.sleep(0.03)
        pygame.mixer.music.unload()

    def _speak_sapi(self, text: str):
        """Offline voice: Windows SAPI, or espeak-ng on Linux."""
        if os.name == "nt":
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SPEAK_SCRIPT),
                    "-Text", text, "-Voice", config.DEFAULT_VOICE]
        else:
            import shutil
            engine = shutil.which("espeak-ng") or shutil.which("espeak")
            if not engine:
                print("[TTS] tidak ada suara offline: install espeak-ng")
                return
            args = [engine, "-v", "id" if is_indonesian(text) else "en-gb", "-s", "165", text]
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while self._proc.poll() is None:
            self.last_activity = time.time()
            time.sleep(0.05)

    def _prune_cache(self):
        self._speak_count += 1
        if self._speak_count % 25:
            return
        files = sorted(CACHE_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for f in files[:-CACHE_MAX_FILES]:
            f.unlink(missing_ok=True)
        for f in AUDIO_DIR.glob("speech_*.mp3"):   # leftovers from interrupted speech
            if time.time() - f.stat().st_mtime > 600:
                f.unlink(missing_ok=True)

    @staticmethod
    def _chunks(text: str) -> list[str]:
        """Split long replies into sentence-sized pieces (tiny ones stay with their neighbour)."""
        if len(text) <= 110:
            return [text]
        chunks: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if chunks and (len(chunks[-1]) < 40 or len(sentence) < 25):
                chunks[-1] += " " + sentence
            elif sentence:
                chunks.append(sentence)
        return chunks

    def _speak_one(self, chunk: str, prefetched=None):
        """Speak one sentence: cached file, prefetched file, streamed Edge, then fallbacks."""
        path, cached = prefetched or (None, False)
        if path is None and len(chunk) <= CACHE_MAX_CHARS:
            cache = self._cache_path(chunk, "edge" if config.TTS_ENGINE == "edge" else "gtts")
            if cache.exists() and cache.stat().st_size > 0:
                path, cached = cache, True
        edge = config.TTS_ENGINE == "edge"
        if path is None and edge and self.edge_ok():
            if self._stream_edge(chunk):
                return
            self.edge_failed()          # no audio in time: the next sentences don't wait for Edge again
        if path is None and not self._stop.is_set():
            # Edge is down: the local Piper voice (instant), the Google voice only as the last resort
            path, cached = self._synthesize(chunk, ["piper", "gtts"] if edge else None)
        try:
            if self._stop.is_set():
                return
            if path:
                if self._spoken_at:
                    print(f"   🔊 suara mulai {time.time() - self._spoken_at:.1f} dtk ({'cache' if cached else 'unduhan'})")
                    self._spoken_at = 0.0
                self._play(path)
            else:
                self._speak_sapi(chunk)
        finally:
            if path and not cached:
                path.unlink(missing_ok=True)
            elif path:
                path.touch()   # keep frequently used phrases in the cache

    def _speak_chunks(self, chunks: list[str]):
        """Stream the first sentence right away while the next sentences download in the background."""
        prefetched: list = [None] * len(chunks)
        ready = [threading.Event() for _ in chunks]

        def prefetch():
            for i in range(1, len(chunks)):
                if self._stop.is_set():
                    break
                prefetched[i] = self._synthesize(chunks[i], use_link=False)   # the kept link is for sentence 1
                ready[i].set()
            for event in ready:
                event.set()

        if len(chunks) > 1:
            threading.Thread(target=prefetch, daemon=True, name="tts-prefetch").start()
        for i, chunk in enumerate(chunks):
            if i:
                ready[i].wait(timeout=30)
            if self._stop.is_set():
                break
            self._speak_one(chunk, prefetched[i])
        for item in prefetched:   # files prefetched but never played (speech was interrupted)
            if item and item[0] and not item[1]:
                item[0].unlink(missing_ok=True)

    def speak(self, text: str):
        text = clean_for_speech(text or "")
        if not text:
            return
        # never wait forever behind a previous sentence that got stuck (network, audio device)
        if not self._lock.acquire(timeout=20):
            print("[TTS] suara sebelumnya macet — dihentikan paksa")
            self.stop()
            if not self._lock.acquire(timeout=5):
                return
        try:
            self._stop.clear()
            self._spoken_at = time.time()
            self.speaking = True
            self.last_activity = time.time()
            events.set_status("speaking")
            events.emit("speaking", text=text)
            if self.before_speak:
                try:
                    self.before_speak()
                except Exception:
                    pass
            try:
                if config.TTS_ENGINE == "sapi":
                    self._speak_sapi(text)
                else:
                    self._speak_chunks(self._chunks(text))
            except Exception as e:
                print(f"[TTS Error] {e}")
            finally:
                if self.after_speak:
                    try:
                        self.after_speak()
                    except Exception:
                        pass
                self.speaking = False
                self.last_activity = time.time()
                events.set_status("idle")
                self._prune_cache()
        finally:
            self._lock.release()


# Global instance
tts_engine = NeuralVoice()


def speak(text: str):
    tts_engine.speak(text)


def stop():
    tts_engine.stop()


def is_cached(text: str) -> bool:
    """Is the voice for this whole reply already in the cache (so it plays instantly)?"""
    try:
        engine = tts_engine
        chunks = engine._chunks(clean_for_speech(text or ""))
        engine_name = "edge" if config.TTS_ENGINE == "edge" else "gtts"
        return bool(chunks) and all(len(c) <= CACHE_MAX_CHARS and engine._cache_path(c, engine_name).exists()
                                    for c in chunks)
    except Exception:
        return False


WARM_PER_RUN, WARM_PER_DAY = 25, 60      # new recordings per start / per day: bursts got the voice service throttled
WARM_STATE = CACHE_DIR / "warm_state.json"


def warm_cache(phrases: list[str], pause: float = 2.5):
    """Fill the voice cache with these replies, slowly, in the background. Capped (WARM_PER_RUN / WARM_PER_DAY) and
    paused while Edge is failing: many requests in a row made Microsoft throttle the voice for minutes."""
    def run():
        import datetime
        import json
        today = datetime.date.today().isoformat()
        try:
            state = json.loads(WARM_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        done_today = state.get("count", 0) if state.get("day") == today else 0
        made = 0
        for phrase in phrases:
            if made >= WARM_PER_RUN or done_today + made >= WARM_PER_DAY:
                break
            while tts_engine.speaking or not tts_engine.edge_ok():
                time.sleep(5)
            for chunk in tts_engine._chunks(clean_for_speech(phrase)):
                path = tts_engine._cache_path(chunk, "edge")
                if len(chunk) > CACHE_MAX_CHARS or path.exists():
                    continue
                try:
                    tts_engine._synth_edge(chunk, path, timeout=10, use_link=False)
                    made += 1
                except Exception:
                    path.unlink(missing_ok=True)
                    tts_engine.edge_failed()
                time.sleep(pause)
        try:
            WARM_STATE.write_text(json.dumps({"day": today, "count": done_today + made}), encoding="utf-8")
        except OSError:
            pass
    threading.Thread(target=run, daemon=True, name="tts-warm-cache").start()


def prewarm():
    """Get the voice connection ready while the user is still talking (called when Digidaw is woken)."""
    if config.TTS_ENGINE == "edge":
        try:
            tts_engine.link.prewarm()
        except Exception:
            pass


def is_speaking() -> bool:
    return tts_engine.is_speaking()


def spoke_since(t: float) -> bool:
    return tts_engine.spoke_since(t)


def is_audible() -> bool:
    return tts_engine.is_audible()


def preload_fallback():
    tts_engine.preload_fallback()


def preload(phrases: list[str]):
    tts_engine.preload(phrases)


def set_hooks(before=None, after=None):
    tts_engine.before_speak = before
    tts_engine.after_speak = after


if __name__ == "__main__":
    speak("Selamat datang kembali, Tuan. Semua sistem sudah online.")
    speak("Good evening, Sir. All systems are online and at your service.")
