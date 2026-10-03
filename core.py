"""
Runtime orchestrator.

Every input source (microphone, console, HUD, hotkey) feeds one queue that a single worker
thread processes in order, so DIGIDAW never talks over himself.

Voice flow:
- The microphone listens continuously (see stt.py); phrases arrive here with their timestamps.
- "Digidaw, <command>" in one breath -> the command runs.
- A bare "Digidaw" (people naturally pause after a name) -> a chime, and the NEXT phrase is taken
  as the command, even if it was already being spoken while "Digidaw" was being recognized.
- After DIGIDAW answers there is a short follow-up window where "Digidaw" isn't needed,
  but only while no music is playing (otherwise song lyrics would become commands).
"""
import re
import sys
import time
import queue
import threading

from rich.console import Console

import oscompat

import config
import events
import brain
import tts
import stt
import safety
import media

console = Console()
T = config.USER_TITLE

# "Digidaw" plus the ways Google STT tends to mishear it, with an optional greeting in front
WAKE_RE = re.compile(
    r"\b(?:(?:hey|hei|hai|halo|oke|ok|oi|oy|yo)\s+)?"
    r"(?:digi[\s-]?(?:daw|dau|dauw|dao|dow)|dikidaw|digidor|digidaw\w*)\b[,.!?]?",
    re.IGNORECASE,
)
# said during a conversation: "that's all" -> close the listening window
CLOSING_RE = re.compile(r"(?:oke |ok |baik )?(?:terima ?kasih|makasih|thanks?(?: you)?|sudah|udah|cukup|itu saja|"
                        r"sudah cukup|tidak ada lagi|nggak ada lagi|gak ada lagi)(?: ya| dulu| digidaw)?", re.IGNORECASE)
# while describing a project (brief mode)
BRIEF_DONE_RE = re.compile(r"(?:oke |ok |baik |ya )?(?:selesai|sudah|sudah selesai|sudah itu saja|itu saja|itu aja|cukup|"
                           r"sudah cukup|mulai|mulai sekarang|mulai buat|mulai bikin|buat sekarang|bikin sekarang|gas|"
                           r"lanjut|lanjut saja|lanjut aja|langsung saja|langsung aja|terserah|terserah kamu|terserah kau|"
                           r"terserah (?:digidaw|anda)|bebas|bebas saja|lewati|skip)"
                           r"(?: ya| digidaw| dong)?", re.IGNORECASE)
# "buat project" alone carries no information about the project itself
BRIEF_TRIGGER_ONLY_RE = re.compile(r"(?:tolong )?(?:buat|bikin|buatkan|bikinkan)(?:kan)?(?: saya| aku)?(?: sebuah| satu)?"
                                   r"(?: project| proyek| aplikasi| app| website| web| situs| game| program)?(?: baru)?",
                                   re.IGNORECASE)
BRIEF_FIRST_WAIT_SECONDS = 30   # time to start describing after Digidaw asks
BRIEF_CANCEL_RE = re.compile(r"(?:batal|batalkan|tidak jadi|gak jadi|nggak jadi|ga jadi)(?: saja| aja| deh)?"
                             r"(?: digidaw)?", re.IGNORECASE)
# a command verb said on its own: Digidaw asks what, and the answer completes the command
BARE_VERB_RE = re.compile(r"(buka|bukain|bukakan|tutup|tutupin|pindah ke|beralih ke|fokus ke|putar|putarkan|setel|mainkan|"
                          r"cari|carikan|matikan|nyalakan|hidupkan|kecilkan|besarkan|hapus|ketik|tulis|catat)", re.IGNORECASE)
BARE_QUESTIONS = {"pindah ke": "Pindah ke mana", "beralih ke": "Beralih ke mana", "fokus ke": "Fokus ke mana",
                  "putar": "Putar lagu apa", "putarkan": "Putar lagu apa", "setel": "Setel lagu apa",
                  "mainkan": "Mainkan lagu apa", "cari": "Cari apa", "carikan": "Cari apa",
                  "ketik": "Ketik apa", "tulis": "Tulis apa", "catat": "Catat apa"}
# pausing Digidaw itself ("stop listening") vs. waking it up again
_DURATION = r"(?: (?:selama |untuk )?(\d+|satu|dua|tiga|lima|sepuluh|lima belas|tiga puluh|setengah) (menit|jam))?"
PAUSE_RE = re.compile(r"(?:tolong )?(?:(?:berhenti|stop|jangan) (?:mendengar(?:kan)?|dengar(?:kan)?|nguping|dengerin)"
                      r"(?: dulu| sebentar| saya| kami)?|istirahat(?: dulu)?|mode (?:senyap|privasi|diam|jeda)|diam dulu|"
                      r"(?:matikan|nonaktifkan) (?:mic|mik|mikrofon|pendengaran(?:nya)?)|(?:pause|jeda) (?:digidaw|pendengaran|mic|mikrofon)|"
                      r"tidur(?: dulu)?)" + _DURATION, re.IGNORECASE)
RESUME_RE = re.compile(r"(?:tolong )?(?:aktif|aktifkan|bangun|dengar(?:kan)? lagi|dengerin lagi|mulai dengar(?:kan)?(?: lagi)?|"
                       r"lanjut(?:kan)? dengar(?:kan)?|kembali|resume|nyalakan (?:mic|mik|mikrofon|pendengaran)|on|hidup|"
                       r"aktif(?:kan)? (?:lagi|kembali)|selesai istirahat)(?: lagi| kembali| dong| ya)?", re.IGNORECASE)
_WORD_NUMBERS = {"satu": 1, "dua": 2, "tiga": 3, "lima": 5, "sepuluh": 10, "lima belas": 15, "tiga puluh": 30}
# music control that works without "Digidaw" while something plays (Google writes "pause" as "paus")
MEDIA_PAUSE_RE = re.compile(r"(?:pause|paus|pos|jeda|stop|setop|stop musik|stop lagu|pause (?:musik|lagu|video)|"
                            r"paus (?:musik|lagu|video)|jeda (?:musik|lagu|video)|hentikan musik|matikan musik)", re.IGNORECASE)
MEDIA_NEXT_RE = re.compile(r"(?:next|skip|lagu selanjutnya|lagu berikutnya|next lagu|ganti lagu)", re.IGNORECASE)
MEDIA_PLAY_RE = re.compile(r"(?:play|lanjutkan musik|lanjutkan lagu|putar lagi|play lagi|lanjut musik)", re.IGNORECASE)
# after a doubtful wake (an alias like "digital" instead of "Digidaw") the phrase must look like a command
COMMAND_START_RE = re.compile(
    r"^(?:tolong |coba |bisa |bisakah |dong )?(?:buka|tutup|putar|putarkan|mainkan|setel|cari|carikan|search|atur|set|naikkan|"
    r"turunkan|kecilkan|besarkan|keraskan|pelankan|matikan|nyalakan|hidupkan|hentikan|stop|pause|jeda|lanjut|lanjutkan|next|skip|"
    r"ingatkan|catat|tulis|ambil|screenshot|kunci|ringkas|rangkum|simpan|salin|scan|cek|periksa|analisis|kembalikan|tambahkan|"
    r"ubah|perbaiki|buat|bikin|pindah|beralih|minimize|maximize|tampilkan|lihat|baca|bacakan|terjemahkan|hitung|jelaskan|"
    r"berapa|apa|apakah|siapa|kapan|dimana|di mana|bagaimana|gimana|kenapa|mengapa|jam|tanggal|hari|cuaca|berita|volume|mute|"
    r"unmute|batalkan|status|restart|shutdown|sleep|scroll|gulir|kembali|refresh|muat|ya|tidak|enggak|nggak|oke|ok|terima kasih|"
    r"makasih|istirahat|aktif|bangun|diam|what|who|how|when|open|close|play)\b", re.IGNORECASE)
DICTATION_STOP = ("selesai dikte", "stop dikte", "berhenti dikte", "akhiri dikte",
                  "selesai mendikte", "stop dictation", "matikan dikte")


FILLERS = [f"Sebentar, {T}.", f"Baik, {T}, sedang saya proses.", f"Satu momen, {T}."]


def chime(up: bool = True):
    """Short two-tone beep: rising = 'I'm listening', falling = 'back to standby'."""
    tones = [(880, 55), (1320, 75)] if up else [(1320, 55), (880, 75)]
    threading.Thread(target=oscompat.beep, args=(tones,), daemon=True).start()


def ack():
    """A short 'tik': the command was heard and is being handled."""
    threading.Thread(target=oscompat.beep, args=([(1568, 40)],), daemon=True).start()


def com_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


# Google often writes "Digidaw" as "digital", "digi", "lagi dong"... (config.WAKE_ALIASES). These are
# trusted only at the very start (optionally after a greeting) or the very end of what was said, so
# the same words in the middle of a sentence stay normal words.
_GREETING = r"(?:(?:hey|hei|hai|halo+|hallo+|oke+|ok|oki|oi|oy|yo)\s+)?"
_alias_cache: dict = {}


def _alias_patterns():
    key = tuple(config.WAKE_ALIASES)
    if key not in _alias_cache:
        words = "|".join(re.escape(a.strip()) for a in sorted(key, key=len, reverse=True) if a.strip())
        _alias_cache.clear()
        _alias_cache[key] = (re.compile(rf"^\s*{_GREETING}(?:{words})\b[,.!?]?", re.IGNORECASE),
                             re.compile(rf"[,\s]+(?:{words})[.!?]?\s*$", re.IGNORECASE)) if words else (None, None)
    return _alias_cache[key]


def split_wake_word(text: str) -> tuple[bool, str]:
    """Return (addressed, command). The command is what follows the last "Digidaw"; if nothing
    follows ("putar musik, Digidaw"), it is what came before."""
    addressed, command, _ = detect_wake(text)
    return addressed, command


def detect_wake(text: str) -> tuple[bool, str, bool]:
    """(addressed, command, loose). loose = only an alias ("digital", "didi"...) was heard, not "Digidaw"
    itself — then the command has to look like one, so chatter ending in "...lagi dong" isn't obeyed."""
    loose = not WAKE_RE.search(text)
    start_re, end_re = _alias_patterns()
    if start_re and (m := start_re.match(text)):
        text = "Digidaw " + text[m.end():]
    elif end_re and (m := end_re.search(text)) and m.start() > 0:
        text = text[:m.start()] + " Digidaw"
    elif start_re and start_re.fullmatch(text.strip()):
        text = "Digidaw"
    matches = list(WAKE_RE.finditer(text))
    if not matches:
        return False, text.strip(" ,.!?"), False
    last = matches[-1]
    after = text[last.end():].strip(" ,.!?")
    before = WAKE_RE.sub(" ", text[:last.start()]).strip(" ,.!?")
    return True, re.sub(r"\s+", " ", after or before), loose


class DigidawCore:
    def __init__(self, voice_enabled: bool):
        self.voice_enabled = voice_enabled
        events.state["voice_enabled"] = voice_enabled
        self.queue: queue.Queue = queue.Queue()
        self.shutdown = threading.Event()
        self.busy = threading.Event()
        self.armed_until = 0.0        # after "Digidaw"/hotkey: the next phrase is a command
        self.followup_until = 0.0     # after an answer: natural follow-up without "Digidaw"
        self._awaiting_command = False
        self._armed_loose = False     # armed by an alias ("digital"), not by "Digidaw" itself
        self._pending_verb = None     # ("buka", asked_at): "Buka apa?" was asked
        self.paused_until = 0.0       # Digidaw told to stop listening (inf = until woken up)
        self._ducked = False
        self._last_command = ("", 0.0)   # (text, finished_at) — drops impatient repeats
        self.restart_requested = False
        self.brief = None                # {"parts": [...], "last": t} while the user describes a project
        self._worker = threading.Thread(target=self._work, daemon=True, name="digidaw-worker")

    def start(self):
        self._worker.start()

    # ───────────────────────── inputs ─────────────────────────

    def submit(self, text: str, source: str = "text") -> threading.Event:
        done = threading.Event()
        self.queue.put((text, source, done))
        return done

    def handle_action(self, action: str):
        """Buttons from the HUD."""
        com_init()
        if action == "talk":
            self.push_to_talk()
        elif action == "stop_speaking":
            tts.stop()
        elif action in ("play", "pause", "toggle", "next", "previous"):
            media.media_control(action)
        elif action == "ai_check":
            def check():
                result = brain.self_check()
                if result["ok"]:
                    text = f"AI normal — {result['model'].replace('gemini-', '')}, {result['ms'] / 1000:.1f} dtk."
                else:
                    text = f"AI tidak bisa dipakai: {result['reason']}"
                events.emit("toast", text=text, level="info" if result["ok"] else "warning")
            threading.Thread(target=check, daemon=True, name="ai-check").start()
        elif action == "alarm_stop":
            import alarms
            alarms.stop_alarm()
        elif action == "alarm_snooze":
            import alarms
            alarms.snooze_alarm(config.ALARM_SNOOZE_MINUTES)
        elif action.startswith("cancel_timer:"):
            import alarms
            with alarms._lock:
                alarms._items[:] = [i for i in alarms._items if i["id"] != action.split(":", 1)[1]]
            alarms._changed()
        elif action == "escape":
            self.escape_key()
        elif action == "pause_listening":
            self.pause()
        elif action == "resume_listening":
            self.resume()
        elif action == "reset":
            brain.reset_conversation()
        elif action == "test_voice":
            tts.speak(f"Halo {T}, beginilah suara saya sekarang. Semua sistem siap.")
        elif action in ("restart", "shutdown"):
            self.restart_requested = action == "restart"
            events.emit("shutdown", restart=self.restart_requested)
            self.shutdown.set()

    def _connectivity(self, online: bool):
        """Speech recognition lost / regained the internet (e.g. right after the laptop starts)."""
        events.emit("toast", text="Internet tersambung lagi. Perintah suara aktif." if online else
                    "Tidak ada internet: perintah suara belum bisa dikenali. Perintah ketik untuk aplikasi tetap jalan.",
                    level="info" if online else "warning")
        events.state["online"] = online
        if online:
            import llm
            if llm.health()["state"] in ("network", "unknown"):
                threading.Thread(target=brain.self_check, daemon=True, name="ai-recheck").start()

    def escape_key(self):
        """Esc anywhere: if Digidaw is listening / talking / thinking, cancel that; otherwise start
        listening right away (no need to say "Digidaw")."""
        if not self.voice_enabled:
            return
        now = time.time()
        if now - getattr(self, "_last_escape", 0) < 0.3:
            return
        self._last_escape = now
        import alarms
        if alarms.is_ringing():
            alarms.stop_alarm()
            return
        listening = now < self.armed_until or (config.CONVERSATION_MODE and self.in_followup())
        if tts.is_speaking() or listening:
            tts.stop()
            self.close_conversation()
            if self._ducked:
                media.unduck()
                self._ducked = False
            chime(up=False)
            events.set_status("paused" if self.is_paused() else "idle")
            events.emit("listening", seconds=0)
            console.print("[dim]   (Esc: batal mendengarkan)[/dim]")
            return
        self.push_to_talk()

    def push_to_talk(self):
        """Hotkey / HUD mic: interrupt DIGIDAW and take the next phrase as a command."""
        import alarms
        if alarms.is_ringing():          # the hotkey stops a ringing alarm first
            alarms.stop_alarm()
            return
        if not self.voice_enabled:
            return
        if tts.is_speaking():
            tts.stop()
        if self.is_paused():
            self.resume(speak=False)
        self.arm()

    # ───────────────────── pause (stop listening) ─────────────────────

    def is_paused(self) -> bool:
        return self.paused_until > time.time()

    def pause(self, minutes: float = 0) -> str:
        """Stop listening: speech is ignored (and long phrases aren't even sent for recognition)
        until 'Digidaw, aktif', the hotkey / HUD button, or the timer."""
        self.paused_until = time.time() + minutes * 60 if minutes else float("inf")
        self.close_conversation()
        stt.stt_engine.max_phrase_seconds = 3.0
        if self._ducked:
            media.unduck()
            self._ducked = False
        events.state["paused"] = True
        events.emit("paused", paused=True, until=self.paused_until if minutes else 0)
        events.set_status("paused")
        span = f" selama {int(minutes)} menit" if minutes >= 1 else ""
        return (f"Baik, {T}, saya berhenti mendengarkan{span}. Ucapkan 'Digidaw, aktif', tekan "
                f"{config.PUSH_TO_TALK_HOTKEY.upper()}, atau tombol di HUD untuk mengaktifkan saya lagi.")

    def resume(self, speak: bool = True) -> str:
        was_paused = self.paused_until > 0
        self.paused_until = 0.0
        stt.stt_engine.max_phrase_seconds = 0.0
        events.state["paused"] = False
        events.emit("paused", paused=False, until=0)
        events.set_status("idle")
        reply = f"Saya mendengarkan lagi, {T}." if was_paused else f"Saya sudah mendengarkan, {T}."
        if speak and was_paused:
            events.emit("digidaw", text=reply)
            tts.speak(reply)
        return reply

    @staticmethod
    def _pause_minutes(match) -> float:
        amount, unit = match.group(1), match.group(2)
        if not amount:
            return 0
        n = 0.5 if amount == "setengah" else float(_WORD_NUMBERS.get(amount, amount) if not amount.isdigit() else amount)
        return n * (60 if unit == "jam" else 1)

    def _media_word(self, text: str) -> bool:
        """'pause' / 'next' / 'play' said alone while music plays: no "Digidaw" needed."""
        if not config.MEDIA_WORDS_WITHOUT_WAKE or len(text.split()) > 3:
            return False
        clean = text.strip(" .,!?").lower()
        if MEDIA_PAUSE_RE.fullmatch(clean) and media.is_playing():
            action = "pause"
        elif MEDIA_NEXT_RE.fullmatch(clean) and media.is_playing():
            action = "next"
        elif MEDIA_PLAY_RE.fullmatch(clean) and media._last_paused:
            action = "play"
        else:
            return False
        console.print(f"[bold yellow]Anda:[/bold yellow] {clean}  [dim](kontrol musik tanpa wake word)[/dim]")
        ack()
        result = media.media_control(action)
        events.emit("user", text=clean, source="voice")
        events.emit("digidaw", text=result)
        return True

    def arm(self):
        """The user addressed DIGIDAW without a command yet: chime, lower the music,
        and treat the next phrase as the command. No spoken reply, so nothing gets missed."""
        self.armed_until = time.time() + config.WAKE_LISTEN_SECONDS
        self._awaiting_command = True
        if config.DUCK_MEDIA and not self._ducked:
            media.duck()
            self._ducked = True
        tts.prewarm()               # the voice connection warms up while the user is still talking
        chime()
        events.set_status("listening")
        events.emit("listening", seconds=config.WAKE_LISTEN_SECONDS)
        console.print(f"[bold green]🎙  Ya, {T}? Silakan bicara...[/bold green]")

    # ───────────────────── project brief mode ─────────────────────

    def start_brief(self, initial: str = "") -> str:
        """Called by the create_project skill: listen to the whole project description."""
        import project_builder  # noqa: F401  (make sure the builder is loaded)
        initial = initial.strip()
        if BRIEF_TRIGGER_ONLY_RE.fullmatch(initial.strip(" .,!?")):
            initial = ""
        if self.brief is not None:        # "buat project ..." said again while describing: keep going
            if initial:
                self.brief["parts"].append(initial)
            self.brief["last"] = time.time()
            events.emit("brief", active=True, text=" ".join(self.brief["parts"]))
            return f"Saya masih mendengarkan, {T}. Lanjutkan, atau ucapkan selesai kalau sudah."
        self.brief = {"parts": [initial] if initial else [], "last": time.time(), "heard": bool(initial)}
        events.emit("brief", active=True, text=initial)
        console.print("[bold magenta]📝 Mode brief project: ceritakan project Anda, ucapkan \"selesai\" kalau sudah.[/bold magenta]")
        if len(initial.split()) >= 12:
            return (f"Siap, {T}. Ada tambahan untuk project ini? Lanjutkan saja, "
                    f"atau ucapkan selesai kalau sudah lengkap.")
        return (f"Siap, {T}. Ceritakan project-nya selengkap mungkin: tujuannya, fiturnya, dan tampilannya. "
                f"Saya mendengarkan. Ucapkan selesai kalau sudah.")

    def _brief_input(self, text: str):
        """One more phrase of the project description (spoken or typed)."""
        _, command = split_wake_word(text)
        clean = (command or text).strip(" .,!?")
        if BRIEF_CANCEL_RE.fullmatch(clean):
            self.brief = None
            events.emit("brief", active=False, text="")
            reply = f"Baik, pembuatan project dibatalkan, {T}."
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            return
        if BRIEF_DONE_RE.fullmatch(clean):
            self.finish_brief()
            return
        self.brief["parts"].append(text.strip())
        self.brief["last"] = time.time()
        self.brief["heard"] = True
        events.emit("brief", active=True, text=" ".join(self.brief["parts"]))
        events.emit("user", text=text, source="brief")
        console.print(f"[magenta]   📝 {text}[/magenta]")

    def finish_brief(self):
        if not self.brief:
            return
        transcript = " ".join(p for p in self.brief["parts"] if p).strip()
        clarify = self.brief.get("clarify")
        self.brief = None
        events.emit("brief", active=False, text=transcript)
        chime(up=False)
        if clarify:
            answers = transcript or "(user menyerahkan pilihannya; pilih yang paling masuk akal)"
            full = (f"{clarify['transcript']}\n\nPertanyaan lanjutan: {' '.join(clarify['questions'])}\n"
                    f"Jawaban user: {answers}")
            self.submit(full, "brief-final")
            return
        if not transcript:
            reply = f"Saya belum mendengar deskripsi project-nya, {T}. Coba lagi dengan bilang: buat project, lalu ceritakan."
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            return
        self.submit(transcript, "brief")

    def _plan_project(self, transcript: str, asked: bool = False):
        """Summarise the brief (asking up to two questions first if it's too thin), then ask for
        confirmation before the agent starts building."""
        import project_builder
        events.set_status("thinking")
        tts.speak(f"Baik, {T}, saya rangkum dulu.")
        plan = project_builder.plan(transcript, ask=not asked and config.BRIEF_ASK_QUESTIONS)
        if plan.get("questions"):
            questions = plan["questions"]
            self.brief = {"parts": [], "last": time.time(), "heard": False,
                          "clarify": {"transcript": transcript, "questions": questions}}
            events.emit("brief", active=True, text="", questions=questions)
            intro = "Satu pertanyaan dulu sebelum mulai" if len(questions) == 1 else "Dua pertanyaan dulu sebelum mulai"
            reply = (f"{intro}, {T}. " + " ".join(questions) +
                     " Jawab saja lalu ucapkan selesai, atau bilang terserah kalau saya yang pilih.")
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            events.set_status("listening")
            return
        agent = project_builder.AGENT_LABELS[plan["agent"]]
        result = safety.guard(f"membuat project {plan['name']} dengan {agent}", project_builder.start_build, p=plan)
        if result.startswith(safety.CONFIRMATION_TOKEN):
            reply = (f"Saya akan membuat {plan['name']}: {plan['summary'].rstrip('.')}. Dibuat dengan {agent}. "
                     f"Mulai sekarang? Jawab ya atau tidak.")
        else:
            reply = result
        console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
        events.emit("digidaw", text=reply)
        tts.speak(reply)
        events.set_status("idle")
        self.open_conversation(config.FOLLOWUP_SECONDS + 20)

    def announce(self, text: str, level: str = "info"):
        """Proactive alerts (monitor, reminders): print, show on HUD, speak."""
        style = "bold yellow" if level in ("warning", "reminder") else "bold cyan"
        console.print(f"[{style}]⚠ DIGIDAW:[/{style}] {text}")
        events.emit("alert", text=text, level=level)
        tts.speak(text)

    # ───────────────────── conversation windows ─────────────────────

    def mic_muted(self) -> bool:
        """Gate for the microphone: DIGIDAW must not hear his own voice."""
        return tts.is_audible()

    def in_followup(self) -> bool:
        return time.time() < self.followup_until

    def _accepts_unaddressed(self) -> bool:
        """May a phrase without "Digidaw" be treated as a command right now?"""
        if not config.REQUIRE_WAKE_WORD or time.time() < self.armed_until or safety.has_pending():
            return True
        import alarms
        if alarms.is_ringing():          # "stop" / "tunda" without saying Digidaw
            return True
        return config.CONVERSATION_MODE and self.in_followup()

    def open_conversation(self, seconds: float):
        """Keep listening after an answer so the user can simply reply. Other apps' audio is
        lowered meanwhile, which also keeps song lyrics from being taken as the reply."""
        self.followup_until = time.time() + seconds
        if self.voice_enabled and config.DUCK_MEDIA and not self._ducked:
            media.duck()
            self._ducked = True
        events.emit("listening", seconds=seconds, kind="conversation")

    def close_conversation(self):
        self.followup_until = 0.0
        self.armed_until = 0.0
        self._awaiting_command = False

    def _update_windows(self):
        now = time.time()
        if self.paused_until and now >= self.paused_until:
            self.resume()
            return
        if self.paused_until:
            events.set_status("paused")
            return
        if self.busy.is_set() or tts.is_speaking():
            if self.brief is not None:
                self.brief["last"] = now          # Digidaw talking is not the user being silent
            return
        if self.brief is not None:
            events.set_status("listening")
            limit = config.BRIEF_SILENCE_SECONDS if self.brief.get("heard") else max(config.BRIEF_SILENCE_SECONDS,
                                                                                    BRIEF_FIRST_WAIT_SECONDS)
            if now - self.brief["last"] > limit:
                self.finish_brief()
            return
        if self._awaiting_command and now >= self.armed_until:
            self._awaiting_command = False
            chime(up=False)
            console.print("[dim]   (kembali siaga)[/dim]")
        conversation = config.CONVERSATION_MODE and self.in_followup()
        if self._ducked and now >= self.armed_until and not conversation and not safety.has_pending():
            media.unduck()
            self._ducked = False
        events.set_status("listening" if now < self.armed_until or conversation else "idle")

    # ───────────────────────── worker ─────────────────────────

    def _work(self):
        com_init()
        while not self.shutdown.is_set():
            try:
                text, source, done = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.busy.set()
            try:
                self._process(text, source)
            except Exception as e:
                console.print(f"[dim red]Error: {e}[/dim red]")
            finally:
                self._last_command = (text.lower(), time.time())
                self.busy.clear()
                done.set()

    def _process(self, text: str, source: str):
        if source in ("brief", "brief-final"):
            return self._plan_project(text, asked=source == "brief-final")
        if self.brief is not None and source in ("hud", "text"):
            return self._brief_input(text)
        clean = text.strip(" .,!?")
        verb = BARE_VERB_RE.fullmatch(clean.lower())
        if verb:
            self._pending_verb = (clean.lower(), time.time())
            reply = BARE_QUESTIONS.get(verb.group(1), "{v} apa").format(v=clean.capitalize()) + f", {T}?"
            events.emit("user", text=text, source=source)
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            self.open_conversation(config.FOLLOWUP_SECONDS + 5)
            return
        if self._pending_verb:
            pending, asked_at = self._pending_verb
            self._pending_verb = None
            if time.time() - asked_at < 25 and not COMMAND_START_RE.match(clean) and not CLOSING_RE.fullmatch(clean):
                text = clean = f"{pending} {clean}"        # "buka" … "firefox" -> "buka firefox"
        pause = PAUSE_RE.fullmatch(clean)
        if pause or (RESUME_RE.fullmatch(clean) and self.paused_until):
            reply = self.pause(self._pause_minutes(pause)) if pause else self.resume(speak=False)
            events.emit("user", text=text, source=source)
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            return
        self.armed_until = 0.0
        self._awaiting_command = False
        if source == "hud":
            console.print(f"\n[bold yellow]You (HUD):[/bold yellow] {text}")
        events.emit("user", text=text, source=source)
        events.set_status("thinking")
        tts.prewarm()
        filler = None
        if source == "voice":
            if config.ACK_SOUND:
                ack()
            if config.THINKING_FILLER_SECONDS > 0:
                filler = threading.Timer(config.THINKING_FILLER_SECONDS, self._say_filler)
                filler.daemon = True
                filler.start()
        started = time.time()
        try:
            response, should_exit = self._run_with_deadline(text)
        finally:
            if filler:
                filler.cancel()
        console.print(f"[dim]   ⏱ diproses dalam {time.time() - started:.1f} dtk[/dim]")
        if response:
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {response}")
            events.emit("digidaw", text=response)
            tts.speak(response)
        events.set_status("idle")
        if should_exit:
            if brain.consume_restart():
                self.restart_requested = True
                events.emit("shutdown", restart=True)
            self.shutdown.set()
            return
        # let the user reply naturally ("ya", "yang kedua", ...) without saying "Digidaw" again
        if safety.has_pending():
            self.open_conversation(config.FOLLOWUP_SECONDS + 15)
        elif source == "voice" and config.CONVERSATION_MODE and not brain.started_background_job():
            self.open_conversation(config.FOLLOWUP_SECONDS)
        if self._pending_verb and source == "voice" and not safety.has_pending():
            self.open_conversation(config.FOLLOWUP_SECONDS + 5)

    def _run_with_deadline(self, text: str) -> tuple[str, bool]:
        """Run the brain on a helper thread so one stuck request (network, a hanging app) can never
        freeze Digidaw: after COMMAND_TIMEOUT_SECONDS it apologises and is ready for the next command."""
        box = {}

        def run():
            com_init()
            try:
                box["result"] = brain.process_command(text)
            except Exception as e:
                box["error"] = e

        worker = threading.Thread(target=run, daemon=True, name="digidaw-command")
        worker.start()
        worker.join(config.COMMAND_TIMEOUT_SECONDS)
        if worker.is_alive():
            console.print(f"[bold red]   ⚠ perintah \"{text}\" melebihi {config.COMMAND_TIMEOUT_SECONDS} dtk, dilewati[/bold red]")
            return f"Maaf {T}, permintaan itu terlalu lama, sepertinya jaringan sedang lambat. Silakan coba lagi.", False
        if "error" in box:
            console.print(f"[dim red]Error: {box['error']}[/dim red]")
            return f"Maaf {T}, terjadi kesalahan: {box['error']}", False
        return box["result"]

    def _say_filler(self):
        """The answer is taking a while (web search, slow network): let the user know."""
        if self.busy.is_set() and not tts.is_speaking():
            tts.speak(FILLERS[int(time.time()) % len(FILLERS)])
            if self.busy.is_set():
                events.set_status("thinking")

    # ───────────────────────── loops ─────────────────────────

    def console_loop(self, prompt: bool = True):
        while not self.shutdown.is_set():
            try:
                text = console.input("\n[bold yellow]Anda:[/bold yellow] ") if prompt else input()
            except (EOFError, KeyboardInterrupt):
                self.shutdown.set()
                break
            text = text.strip()
            if text:
                self.submit(text, "text").wait()

    def voice_loop(self):
        stt.stt_engine.on_connectivity = self._connectivity
        stt.stt_engine.needs_full_text = lambda: (self._accepts_unaddressed() or self.brief is not None
                                                  or bool(events.state.get("dictation")))
        stt.stt_engine.media_playing = lambda: config.MEDIA_WORDS_WITHOUT_WAKE and media.is_playing()
        import local_stt
        local_stt.preload()
        if not stt.start_background(self.on_heard, self.mic_muted):
            console.print("[bold red]Mikrofon tidak bisa dipakai untuk mendengarkan terus-menerus.[/bold red]")
            if sys.stdin is None:          # no console: the HUD is the only input
                return self.shutdown.wait()
            return self.console_loop()
        next_health = time.time() + 600
        try:
            while not self.shutdown.is_set():
                self._update_windows()
                if time.time() > next_health:
                    next_health = time.time() + 600
                    self.log_health()
                time.sleep(0.2)
        finally:
            stt.stop_background()

    def log_health(self):
        """One line every 10 minutes: makes slowdowns after long use easy to diagnose from the log."""
        import psutil
        import llm
        proc = psutil.Process()
        ai = ", ".join(f"{m.replace('gemini-', '')}:{v['used']}" + (f"(rest {v['resting']}s)" if v["resting"] else "")
                       + ("(x)" if v["retired"] else "") for m, v in llm.status().items())
        console.print(f"[dim]♥ health {time.strftime('%H:%M')} | threads {threading.active_count()} | "
                      f"RAM {proc.memory_info().rss / 2**20:.0f} MB | antrean suara {stt.stt_engine._phrases.qsize()} | "
                      f"AI/menit {ai}[/dim]")

    def on_heard(self, text: str, started: float, ended: float):
        """Called by the recognition thread for every phrase, in the order it was spoken."""
        if self.shutdown.is_set():
            return
        if events.state["dictation"]:
            self._dictate(text)
            return
        if self.brief is not None:
            self._brief_input(text)
            return

        addressed, command, loose = detect_wake(text)
        if addressed and command.lower().strip(" .,!?") in ("restart", "mulai ulang", "muat ulang", "reboot") and \
                re.search(r"\b(?:restart|mulai ulang|muat ulang|reboot)\W+digidaw\b", text, re.IGNORECASE):
            command = "restart digidaw"      # "restart Digidaw": the name is the object here, not only the call
        if self.is_paused():
            # only "Digidaw, aktif" (the real name, not an alias) wakes it up; everything else is dropped unlogged
            if addressed and not loose and RESUME_RE.fullmatch(command.strip(" .,!?")):
                self.resume()
            return
        import alarms
        if not addressed and not alarms.is_ringing() and self._media_word(text):
            return                     # (while an alarm rings, "stop" is for the alarm)
        if addressed and loose and command and not COMMAND_START_RE.match(command):
            addressed = False          # "…lagi dong" at the end of someone's sentence
        armed_loose = self._armed_loose and time.time() < self.armed_until
        if not addressed and armed_loose and not COMMAND_START_RE.match(text.strip()):
            self.armed_until = 0.0     # woken by an alias, and this isn't a command either: chatter
        if not addressed and not self._accepts_unaddressed():
            detail = f"\"{text}\"" if config.LOG_OVERHEARD else f"{len(text.split())} kata"
            console.print(f"[dim]· terdengar: {detail} (diabaikan — panggil \"Digidaw\" dulu; "
                          f"{time.time() - ended:.1f} dtk setelah selesai bicara)[/dim]")
            events.emit("heard", text=text, ignored=True)
            return
        if not command:            # just "Digidaw" — the command follows in the next phrase
            if loose and len(text.split()) > 3:
                return                 # an alias inside a longer sentence isn't a call
            self.arm()
            self._armed_loose = loose
            return
        self._armed_loose = False
        if CLOSING_RE.fullmatch(command.strip(" .,!?")) and not safety.has_pending():
            self.close_conversation()   # "terima kasih" / "sudah": end the conversation
            reply = f"Sama-sama, {T}."
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("user", text=command, source="voice")
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            return

        last_text, finished_at = self._last_command
        if command.lower() == last_text and started < finished_at:
            console.print(f"[dim]· \"{command}\" sudah diproses, diabaikan[/dim]")
            return

        console.print(f"[bold yellow]Anda:[/bold yellow] {command}  "
                      f"[dim](dikenali {time.time() - ended:.1f} dtk setelah selesai bicara)[/dim]")
        self.submit(command, "voice").wait()

    def _dictate(self, text: str):
        import system_control
        if any(p in text.lower() for p in DICTATION_STOP):
            events.set_dictation(False)
            chime(up=False)
            reply = f"Mode dikte selesai, {T}."
            events.emit("digidaw", text=reply)
            tts.speak(reply)
            return
        system_control.type_text(text + " ")
        events.emit("user", text=text, source="dictation")
