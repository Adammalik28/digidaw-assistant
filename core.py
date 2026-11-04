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
import difflib
import collections
import time
import queue
import threading

from rich.console import Console

import oscompat

import config
import diag
import metrics
import storage
import events
import brain
import tts
import stt
import safety
import media
import notifications
import persona
import presence
import aec
import screen_agent

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
BARE_VERB_RE = re.compile(r"(lebih|agak|buka|bukain|bukakan|tutup|tutupin|pindah ke|beralih ke|fokus ke|putar|putarkan|setel|mainkan|"
                          r"cari|carikan|telusuri|cari di (?:kolom )?pencarian(?: youtube| google)?|matikan|nyalakan|hidupkan|kecilkan|besarkan|hapus|ketik|tulis|catat)", re.IGNORECASE)
BARE_QUESTIONS = {"pindah ke": "Pindah ke mana", "beralih ke": "Beralih ke mana", "fokus ke": "Fokus ke mana",
                  "putar": "Putar lagu apa", "putarkan": "Putar lagu apa", "setel": "Setel lagu apa",
                  "mainkan": "Mainkan lagu apa", "cari": "Cari apa", "carikan": "Cari apa", "telusuri": "Telusuri apa",
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
    r"makasih|istirahat|aktif|bangun|diam|what|who|how|when|open|close|play|"
    r"ganti|gantikan|kirim|kirimkan|kirimin|balas|balaskan|balasin|bales|jawab|jawabin|bilang|bilangin|sampaikan|"
    r"kabari|kabarin|redupkan|terangkan|redupin|terangin|nyalain|matiin|hidupin|idupin|putarin|puterin|mainin|rekap)\b",
    re.IGNORECASE)
# after "digital" / "Didi" said on its own (then the chime), the next phrase is almost surely for Digidaw: requests and
# questions count too ("ada pesan masuk?", "lampu mode santai", "mau dengar musik"), not just a verb
ARMED_COMMAND_RE = re.compile(
    COMMAND_START_RE.pattern + r"|^(?:tolong |coba |bisa |dong )?(?:ada|mau|minta|pengen|ingin|saya|aku|gue|kamu|lampu|"
    r"mode|suasana|pesan|chat|notifikasi|notif|musik|lagu|video|youtube|agenda|jadwal|acara|rangkuman|alarm|timer|"
    r"pengingat|layar|wifi|bluetooth|baterai|suhu|matahari|kasih|beri|berikan|info|lanjut|sekarang|besok|nanti|"
    r"buatkan|bikinin|ceritakan|ceritain|tingkatkan|turunkan|naikkan|situs|halaman|reboot)\b",
    re.IGNORECASE)
# "ubah lampu kamera" ("kamar merah" misheard), "ganti lampu": the lamp, but not what it should become — Digidaw asks
LAMP_BARE_RE = re.compile(r"(?:(?:tolong|coba) )?(?:\w+ )?(?:warna )?lampu(?:nya)?(?: \w+)?")
LAMP_VERB_FIRST_RE = re.compile(r"(?:matikan|padamkan|matiin|nyalakan|hidupkan|nyalain|hidupin|redupkan|terangkan|"
                                r"redupin|terangin)\b")
DICTATION_STOP = ("selesai dikte", "stop dikte", "berhenti dikte", "akhiri dikte",
                  "selesai mendikte", "stop dictation", "matikan dikte")


FILLERS = [f"Sebentar, {T}.", f"Baik, {T}, sedang saya proses.", f"Satu momen, {T}."]
# the answers to Digidaw's own questions after proactive news ("Mau saya bacakan?")
SHORT_REPLY_RE = re.compile(r"(?:ya|iya|iyah|yap|yes|boleh|oke|ok|silakan|silahkan|baik|sip|tidak|nggak|gak|enggak|"
                            r"jangan|no|nanti|nanti saja|nanti aja|sudah|udah|cukup|stop|bacakan|bacain|baca|"
                            r"apa isinya|isinya apa|terima kasih|makasih)(?: (?:dong|saja|aja|deh|ya|dulu|bacakan|"
                            r"baca|tuan|digidaw|pesannya|semuanya))*")
# said while Digidaw is talking (barge-in, aec.py): stop talking, nothing else
BARGE_STOP_RE = re.compile(r"(?:(?:oke|ok|ya|udah|sudah) )?(?:stop|setop|berhenti|diam|cukup|sudah|udah|hentikan|"
                           r"sudah cukup|stop dulu|diam dulu|shut up|stop bicara|berhenti bicara)(?: (?:dulu|digidaw|ya))?")
WELCOME_PLAIN_SECONDS = 30 * 60      # back after this long with no news: still a short "selamat datang kembali"
BRIEFING_AFTER_SECONDS = 3 * 3600    # back after a night away and today's summary not given yet: the summary


def _span(seconds: float) -> str:
    """40 menit / 1 jam 20 menit / 7 jam."""
    hours, minutes = divmod(int(round(seconds / 60)), 60)
    if not hours:
        return f"{max(minutes, 1)} menit"
    return f"{hours} jam" + (f" {minutes} menit" if minutes and hours < 3 else "")


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


def _alias_first(text: str) -> bool:
    """The call is an alias at the very start ("digital, ada pesan masuk?"): much surer than one at the end
    ("…lagi dong"), which is how plenty of ordinary sentences end."""
    start_re, _ = _alias_patterns()
    return bool(start_re and start_re.match(text))


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


def after_misheard_name(text: str) -> str:
    """"gigi buka Fire", "pigi chord Digi Buka terminal": the name misheard, then a command. Only what follows the name;
    the text unchanged when it doesn't look like that. Doesn't make a phrase a call: it's used on a phrase already taken."""
    if COMMAND_START_RE.match(text):
        return text
    words = text.split()
    for n in range(1, min(4, len(words))):
        rest = " ".join(words[n:])
        if COMMAND_START_RE.match(rest):
            import stt
            return rest if stt.sounds_like_name(" ".join(words[:n])) else text
    return text


def sounds_like_call(text: str) -> bool:
    """A short phrase that is most likely "Digidaw" misheard ("Dido", "gigi", "dingin dong") while Digidaw is
    already waiting for a command: the user is calling again, it isn't the command. Real short commands
    ("diam", "lanjut", "stop") start like a command and are never caught by this."""
    text = text.strip(" .,!?")
    if not 1 <= len(text.split()) <= 2 or COMMAND_START_RE.match(text):
        return False
    key = re.sub(r"[^a-z]", "", text.lower())
    return len(key) >= 4 and difflib.SequenceMatcher(None, key, "digidaw").ratio() >= 0.5


ECHO_SECONDS = 8                 # after Digidaw said something (+ the time it takes to say it): its words heard
ECHO_SHARE = 0.8                 # back are its own voice, when this share of the phrase is one run of its words


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def own_echo(text: str, now: float | None = None) -> bool:
    """Is this phrase Digidaw's own answer coming back into the microphone: through the speakers, or from the phone
    reading the same answer aloud a moment later ("Oke lanjut lagi" right after it said "Oke, lanjut lagi.")? Only a
    long run of its exact words counts, so "lampu merah" after "Lampu sekarang merah" is still the user."""
    heard = _words(text)
    if len(heard) < 3:
        return False
    now = time.time() if now is None else now
    for event in reversed(list(events.history)):
        if event.get("type") not in ("digidaw", "alert"):
            continue
        said = _words(event.get("text", ""))
        if now - event.get("ts", 0) > ECHO_SECONDS + len(" ".join(said)) / 12:
            break
        if not said or len(heard) > len(said) + 2:
            continue
        run = difflib.SequenceMatcher(None, heard, said, autojunk=False).find_longest_match(0, len(heard), 0, len(said))
        if run.size >= ECHO_SHARE * len(heard):
            return True
    return False


def wake_echo(text: str) -> bool:
    """"Digidaw, digidaw, digidaw, …" and nothing else: the recogniser hearing the name in music or noise
    (it is told to expect it), not someone calling."""
    return len(WAKE_RE.findall(text)) >= 3 and not WAKE_RE.sub(" ", text).strip(" ,.!?")


def stray_before_call(text: str, command: str) -> bool:
    """"book Digidaw", "Jungan. Digidaw": nothing after the name and a word or two before it that isn't a
    command — the tail of other talk (or noise) before the call, not "putar musik, Digidaw"."""
    matches = list(WAKE_RE.finditer(text))
    return bool(matches) and not text[matches[-1].end():].strip(" ,.!?") \
        and len(command.split()) <= 2 and not COMMAND_START_RE.match(command)


def classify_call(text: str, armed: str = "") -> tuple[str, str]:
    """For a device that listens by itself (the phone's hands-free mode): what to do with one phrase.
    armed: "" = waiting for "Digidaw", "1" = it was just called, "loose" = called by an alias ("digital"),
    so the phrase has to look like a command, "followup" = right after an answer (no "Digidaw" needed, but a
    lone word that isn't a command is a scrap of other talk). Returns ("ignore" | "arm" | "command", command)."""
    text = text.strip()
    if not text or wake_echo(text):
        return "ignore", ""
    addressed, command, loose = detect_wake(text)
    if addressed and loose and command and not COMMAND_START_RE.match(command):
        addressed = False              # "…lagi dong" at the end of someone's sentence
    if addressed and not loose and command and stray_before_call(text, command):
        command = ""
    if addressed and not command:
        return ("ignore", "") if loose and len(text.split()) > 3 else ("arm", "loose" if loose else "")
    if addressed:
        return "command", command
    if armed:
        if sounds_like_call(text):
            return "arm", ""
        if armed == "loose" and not COMMAND_START_RE.match(text):
            return "ignore", ""
        if armed == "followup" and len(text.strip(" ,.!?").split()) < 2 and not COMMAND_START_RE.match(text.strip()):
            return "ignore", ""
        return "command", text.strip(" ,.!?")
    return "ignore", ""


def _device(source: str) -> str:
    return "hp" if source.startswith("hp") else "pc"


BRIEFING_STATE = config.LOG_DIR / "startup_state.json"


def _briefing_due() -> bool:
    import datetime
    import json
    try:
        last = json.loads(BRIEFING_STATE.read_text(encoding="utf-8")).get("last_morning")
    except (OSError, ValueError):
        last = None
    return last != datetime.date.today().isoformat() and datetime.datetime.now().hour >= 4


def _briefing_done():
    import datetime
    import json
    try:
        data = json.loads(BRIEFING_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["last_morning"] = datetime.date.today().isoformat()
    try:
        storage.save_json(BRIEFING_STATE, data)
    except OSError:
        pass


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
        self._pending_lamp = 0.0      # "Lampunya mau diubah jadi apa?" was asked at
        self._ting_undone = False     # Google didn't hear the name in the last chime's phrase (a chime "taken back" if
                                      # no command follows; counted once, not also as "no command")
        self._doubted_at = 0.0        # when that was
        self._loose_rejected = False  # one phrase that isn't a command was heard since a call that wasn't sure
        self._briefing_waiting = False   # today's summary waits for the conversation to end
        self.paused_until = 0.0       # Digidaw told to stop listening (inf = until woken up)
        self._ducked = False
        self._last_command = ("", 0.0)   # (text, finished_at) — drops impatient repeats
        # spoken commands lately: one sentence may be heard by both the PC's microphone and the phone
        self._recent = collections.deque(maxlen=6)
        self._queued_at = 0.0            # when the command now being processed was queued
        self.restart_requested = False
        self.brief = None                # {"parts": [...], "last": t} while the user describes a project
        self._early_armed_at = 0.0       # the chime came from the laptop's recognizer, before Google confirmed
        self._followup_only = None       # while listening after proactive news: the only replies accepted
        self._ids = iter(range(1, 10**12))
        self._worker = threading.Thread(target=self._work, daemon=True, name="digidaw-worker")

    def start(self):
        self._worker.start()

    # ───────────────────────── inputs ─────────────────────────

    def submit(self, text: str, source: str = "text") -> threading.Event:
        done = threading.Event()
        done.queued_at = time.time()
        done.id = next(self._ids)        # the replies to this command carry it (events.working_on)
        done.reply = ""                  # Digidaw's answer, once done is set
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
        elif action.startswith("delete_event:"):
            import agenda
            agenda.delete_event(action.split(":", 1)[1])
        elif action.startswith("cancel_timer:"):
            import alarms
            with alarms._lock:
                alarms._items[:] = [i for i in alarms._items if i["id"] != action.split(":", 1)[1]]
            alarms._changed()
        elif action.startswith("lamp:"):             # lamp:toggle, lamp:brightness:40, lamp:color:merah, lamp:scene:film
            import smarthome
            parts = action.split(":", 2)
            result = smarthome.handle_action(parts[1], parts[2] if len(parts) > 2 else None)
            if not (smarthome.state() or {}).get("online"):
                events.emit("toast", text=result, level="warning")
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
            console.print(f"[dim]· tombol {'restart' if action == 'restart' else 'matikan'} di HUD ditekan[/dim]")
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
        listening right away (no need to say "Digidaw"). A job running on the screen stops first of all."""
        if screen_agent.cancel(say=True):
            console.print("[bold]⏹ tugas di layar dihentikan (Esc)[/bold]")
            return
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

    def _briefing_after_command(self, source: str):
        """Once a day: agenda, weather, battery — after the user's first command, once the conversation is over.
        (It used to answer the first bare "Digidaw" of the day instead of listening, and without the early chime:
        right after a start that felt like Digidaw not responding.)"""
        if source not in ("voice", "hud", "text", "hp") or not config.STARTUP_BRIEFING or not _briefing_due() \
                or self._briefing_waiting:
            return
        self._briefing_waiting = True

        def run():
            try:
                import ai_skills

                def words() -> str:
                    return ai_skills.morning_briefing() if _briefing_due() else ""
                if self.announce(words, "info", wait=120, push=False):
                    _briefing_done()
            except Exception as e:
                console.print(f"[dim red][briefing] {e}[/dim red]")
            finally:
                self._briefing_waiting = False
        threading.Thread(target=run, daemon=True, name="briefing").start()

    def wake_hint(self, heard_here: str) -> bool:
        """The laptop's own recognizer heard the name in a phrase still on its way to Google (~0.5-1 s, at times
        much more): warm the voice up now and, when the phrase is only "Digidaw", chime right away instead of after
        Google. Returns True when it chimed (wake_unconfirmed takes it back if Google heard something else)."""
        tts.prewarm()
        if aec.active() and tts.is_speaking() and WAKE_RE.fullmatch(heard_here.strip()):
            tts.stop()                   # "Digidaw" while it talks: it stops at once (Google's text then decides)
            console.print("[bold]⏹ disela[/bold]")
        called_again = self._armed_loose and time.time() < self.armed_until     # not a sure call yet: answer again
        if not WAKE_RE.fullmatch(heard_here.strip()) or (time.time() < self.armed_until and not called_again) \
                or self.in_followup() or self.is_paused() or self.brief is not None or events.state.get("dictation") \
                or safety.has_pending():
            return False
        self.arm()
        self._early_armed_at = time.time()
        return True

    def wake_unconfirmed(self):
        """Google heard the phrase the early chime answered, and it wasn't the name. It's noise or a song, or the user
        called and Google missed it (about half of them on 3-4 Nov, who then had to call again): the chime stands for
        its few seconds, and only a phrase that sounds like a command gets through."""
        if self._early_armed_at and self._awaiting_command:
            self._early_armed_at = 0.0
            self._doubted_at = time.time()
            self._armed_loose = True
            self._ting_undone = True        # (counted when the wait ends with no command)
            console.print("[dim]· panggilan diragukan: Google tidak mendengar nama Digidaw; hanya kalimat perintah "
                          "yang diterima[/dim]")

    @staticmethod
    def _sounds_like_command(text: str) -> bool:
        """After a call that wasn't quite heard: does this phrase start like a command, or is it one Digidaw knows?"""
        if ARMED_COMMAND_RE.match(text.strip()):
            return True
        try:
            import habits
            return brain._route(habits.apply_aliases(text)) is not None
        except Exception as e:
            diag.warn_once("core.commandlike", "kalimat sesudah panggilan gagal dicek", e)
            return False

    def arm(self):
        """The user addressed DIGIDAW without a command yet: chime, lower the music,
        and treat the next phrase as the command. No spoken reply, so nothing gets missed."""
        self.armed_until = time.time() + config.WAKE_LISTEN_SECONDS
        self._awaiting_command = True
        self._doubted_at = 0.0
        self._ting_undone = False
        self._loose_rejected = False
        metrics.count("ting")
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

    def announce(self, text, level: str = "info", listen: float = 0, wait: float = 0, push: bool = True) -> bool:
        """Proactive alerts (monitor, reminders, messages): print, show on HUD, speak.
        Small news (level "info") isn't said to an empty room: while the user is away from the computer it is kept
        and told when they are back (welcome_back); warnings and reminders are always said. Small news and messages
        also wait while the user watches something fullscreen, is in a call, or has Do Not Disturb on.
        text: the words, or a function giving them at the moment they are said (after the wait: news that came
        meanwhile can change them; "" = nothing to say any more).
        wait: first wait up to this many seconds for a moment nobody is talking with Digidaw; none came = say nothing.
        listen: keep listening this long afterwards, so the user can simply answer ("ya").
        push: also notify the phone. Returns whether it was said."""
        if level == "info" and presence.is_away():
            if callable(text):
                return False             # (worded only when said: nothing to keep for later)
            presence.hold(text)
            console.print(f"[dim]· disimpan sampai Anda kembali: {text}[/dim]")
            events.emit("alert", text=text, level=level, push=push)
            return False
        if level in ("info", "message"):
            busy = presence.busy_reason()
            if busy:
                console.print(f"[dim]· tidak diucapkan ({busy})[/dim]")
                if not callable(text):
                    events.emit("alert", text=text, level=level, push=False)
                return False
        if wait:
            deadline = time.time() + wait
            while not self._quiet():
                if time.time() > deadline or self.shutdown.is_set():
                    return False
                time.sleep(0.5)
        if callable(text):
            text = text()
            if not text:
                return False
        style = "bold yellow" if level in ("warning", "reminder") else "bold cyan"
        console.print(f"[{style}]⚠ DIGIDAW:[/{style}] {text}")
        events.emit("alert", text=text, level=level, push=push)
        tts.speak(text)
        if listen and self.voice_enabled:
            self.open_conversation(listen, only=SHORT_REPLY_RE)   # proactive news: listen for an answer only
        return True

    # ───────────────────── away and back ─────────────────────

    def _quiet(self) -> bool:
        """Nobody is talking with Digidaw right now: a good moment for news that can wait a little."""
        return not (self.busy.is_set() or tts.is_speaking() or self.in_followup() or time.time() < self.armed_until
                    or self.brief is not None or events.state.get("dictation") or self.is_paused()
                    or safety.has_pending())

    def welcome_back(self, seconds: float, how: str, media_played: bool, left_at: float):
        """The user is back after config.AWAY_MINUTES or more (presence.py): what happened meanwhile, in one breath.
        Back by talking to Digidaw ("command"): after that answer, and only if there is news. Back in the morning
        before today's summary was given: the summary itself."""
        deadline, patience = time.time() + 60, time.time() + 15 * 60
        while True:
            quiet = self._quiet()
            if quiet and not presence.busy_reason():
                break
            # someone talks with Digidaw: a minute at most; a film / call / Do Not Disturb right after coming back:
            # the news waits for it to end (a while)
            if time.time() > (patience if quiet else deadline) or self.shutdown.is_set():
                return                       # (the news stays kept for the next time)
            time.sleep(5 if quiet else 0.5)
        held = [persona.TITLE_ANY.sub("", t).strip() for t in presence.take_held()]
        if not config.WELCOME_BACK:
            return
        messages = notifications.unread_summary(since=left_at)
        span = _span(seconds)
        news = ([f"Selama {span} tadi:", *held] + ([f"Juga {messages}"] if messages else []) if held else
                [f"Selama {span} tadi {messages}"] if messages else [])
        if how == "input" and seconds >= BRIEFING_AFTER_SECONDS and time.localtime().tm_hour < 12 \
                and config.STARTUP_BRIEFING and _briefing_due():
            import ai_skills
            try:
                parts = [ai_skills.morning_briefing(), *news]
            except Exception as e:
                console.print(f"[dim red][briefing] {e}[/dim red]")
                parts = [f"{ai_skills.greeting()}, {T}.", *news]
            else:
                _briefing_done()
        elif news:
            parts = [f"Selamat datang kembali, {T}.", *news]
        elif how == "input" and seconds >= WELCOME_PLAIN_SECONDS and not media_played:
            parts = [f"Selamat datang kembali, {T}."]   # (something played all along: maybe watched, not gone)
        else:
            return
        text = " ".join(parts) + (" Mau saya bacakan?" if messages else "")   # the question last, after the messages
        if messages:
            notifications.mark_told(since=left_at)    # their next message isn't announced again right after this
        if self.announce(text, "info", listen=config.FOLLOWUP_SECONDS if messages else 0, push=False) and messages:
            notifications.offer_reading()
            notifications.prepare_reading()

    # ───────────────────── conversation windows ─────────────────────

    def mic_muted(self) -> bool:
        """Gate for the microphone: DIGIDAW must not hear his own voice. With echo cancellation (aec.py) its voice
        is already taken out of what the microphone hears, so it keeps listening and can be interrupted."""
        if aec.active():
            return False
        return tts.is_audible()

    def _barge_in(self, text: str) -> bool:
        """A phrase heard while Digidaw was talking. Only "Digidaw …" (the real name) or a plain "stop" counts: the
        speech stops. Returns True when the phrase should then be handled like any other ("Digidaw, putar musik",
        or just "Digidaw" = listen for the command)."""
        addressed, command, loose = detect_wake(text)
        clean = command.strip(" .,!?").lower()
        if addressed and not loose:
            tts.stop()
            if clean and BARGE_STOP_RE.fullmatch(clean):
                chime(up=False)
                console.print(f"[bold]⏹ disela:[/bold] {text}")
                return False
            console.print(f"[bold]⏹ disela:[/bold] {text}")
            return True
        if BARGE_STOP_RE.fullmatch(text.strip(" .,!?").lower()):
            tts.stop()
            chime(up=False)
            console.print(f"[bold]⏹ disela:[/bold] {text}")
            return False
        console.print(f"[dim]· terdengar saat Digidaw bicara: {len(text.split())} kata (diabaikan)[/dim]")
        return False

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

    def open_conversation(self, seconds: float, only=None):
        """Keep listening after an answer so the user can simply reply. Other apps' audio is
        lowered meanwhile, which also keeps song lyrics from being taken as the reply."""
        self.followup_until = time.time() + seconds
        self._followup_only = only           # a pattern: only such replies are accepted (None = any)
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
            metrics.count("ting_dibatalkan" if self._ting_undone else "ting_tanpa_perintah")
            self._ting_undone = False
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
            queued_at = getattr(done, "queued_at", time.time())
            earlier = self._heard_twice(text, source, queued_at)
            if earlier:
                where = "HP" if earlier["device"] == "hp" else "mikrofon PC"
                console.print(f"[dim]· \"{text}\" sudah dijalankan lewat {where}, diabaikan[/dim]")
                self.armed_until = 0.0
                self._awaiting_command = False
                done.reply = earlier["reply"]
                done.duplicate = earlier["device"]       # (the phone then doesn't say the PC's answer again)
                done.set()
                continue
            self.busy.set()
            self._queued_at = queued_at
            events.working_on(done.id)
            try:
                self._process(text, source)
            except Exception as e:
                console.print(f"[dim red]Error: {e}[/dim red]")
            finally:
                events.working_on(None)
                self._last_command = (text.lower(), time.time())
                replies = [e.get("text", "") for e in list(events.history)
                           if e.get("type") == "digidaw" and e.get("cmd") == done.id]
                done.reply = replies[-1] if replies else ""
                if source in ("voice", "hp", "hp-silent"):
                    self._recent.append({"text": text.lower().strip(" .,!?"), "device": _device(source),
                                         "at": queued_at, "reply": done.reply})
                self.busy.clear()
                done.set()

    def _pc_speaks(self, source: str) -> bool:
        """Should the PC say the answer out loud? Not when it came from the phone set to "HP saja", and not when
        the PC's microphone caught the same sentence the phone (set to "HP saja") also heard: the user is holding
        the phone, so the answer belongs there (the phone gets the reply through the duplicate check)."""
        if source == "hp-silent":
            return False
        if source == "voice":
            try:
                import mobile_server
                return not mobile_server.phone_covers(self._queued_at)
            except Exception:
                return True
        return True

    def _heard_twice(self, text: str, source: str, queued_at: float):
        """The same spoken command that just came from the other device (the phone and the PC's microphone
        both heard the user): the earlier one ran, this one is skipped. The queue runs one at a time, so
        whichever arrived first wins however long it took."""
        if source not in ("voice", "hp", "hp-silent"):
            return None
        key = text.lower().strip(" .,!?")
        for r in reversed(self._recent):
            if r["device"] != _device(source) and abs(queued_at - r["at"]) <= 10 \
                    and difflib.SequenceMatcher(None, r["text"], key).ratio() >= 0.8:
                return r
        return None

    def _process(self, text: str, source: str):
        if source in ("brief", "brief-final"):
            return self._plan_project(text, asked=source == "brief-final")
        presence.touch()                     # talking to Digidaw = being here (also from the HUD or the phone)
        if source in ("hud", "text", "hp", "hp-silent"):
            metrics.count("perintah_hp" if source.startswith("hp") else "perintah_hud")
        try:
            import daily
            daily.note_command()
        except Exception as e:
            diag.warn_once("core.daily", "hitungan perintah harian gagal", e)
        try:
            self._handle(text, source)
        finally:
            notifications.clear_offer()      # "Mau saya bacakan?" is answered by the very next command or not at all
        self._briefing_after_command(source)

    @staticmethod
    def _lamp_unfinished(clean: str) -> bool:
        """"ubah lampu kamera" / "ganti lampu": the lamp is named but nothing says what it should become."""
        low = clean.lower()
        if not LAMP_BARE_RE.fullmatch(low):
            return False
        try:
            import habits
            import smarthome
            return smarthome.has_lamps() and brain._route(habits.apply_aliases(low)) is None
        except Exception:
            return False

    def _handle(self, text: str, source: str):
        if self.brief is not None and source in ("hud", "text"):
            return self._brief_input(text)
        clean = text.strip(" .,!?")
        if self._pending_lamp:
            asked_at, self._pending_lamp = self._pending_lamp, 0.0
            if time.time() - asked_at < 25 and not CLOSING_RE.fullmatch(clean) and not BRIEF_CANCEL_RE.fullmatch(clean):
                low = clean.lower()          # "merah" -> "lampu merah", "matikan" -> "matikan lampu"
                text = clean = f"{clean} lampu" if LAMP_VERB_FIRST_RE.match(low) else f"lampu {clean}"
        verb = BARE_VERB_RE.fullmatch(clean.lower())
        if verb:
            self._pending_verb = (clean.lower(), time.time())
            said = verb.group(1).lower()
            reply = BARE_QUESTIONS.get(said, "Cari apa" if said.startswith("cari ") else "{v} apa").format(v=clean.capitalize()) + f", {T}?"
            events.emit("user", text=text, source=source)
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("digidaw", text=reply)
            if self._pc_speaks(source):
                tts.speak(reply)
            self.open_conversation(config.FOLLOWUP_SECONDS + 5)
            return
        if self._lamp_unfinished(clean):
            self._pending_lamp = time.time()
            reply = f"Lampunya mau diubah jadi apa, {T}? Warnanya, terang redupnya, atau suasananya."
            events.emit("user", text=text, source=source)
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {reply}")
            events.emit("digidaw", text=reply)
            if self._pc_speaks(source):
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
            if self._pc_speaks(source):
                tts.speak(reply)
            return
        self.armed_until = 0.0
        self._awaiting_command = False
        if source in ("hud", "hp", "hp-silent"):
            console.print(f"\n[bold yellow]You ({'HP' if source.startswith('hp') else 'HUD'}):[/bold yellow] {text}")
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
        # a long answer is spoken while the AI is still writing it (brain streams it into this)
        live = tts.LiveSpeech(shape=lambda sentence, first: persona.live_sentence(brain.fix_title(sentence), first)) \
            if config.STREAM_REPLIES and self._pc_speaks(source) else None
        brain.set_live(live)
        try:
            response, should_exit = self._run_with_deadline(text)
        finally:
            brain.set_live(None)
            if filler:
                filler.cancel()
            if live is not None:
                live.close()
        route = {"local": "lokal", "gemini": "gemini"}.get(getattr(brain, "_brain_used", ""), "cepat")
        metrics.count(f"rute_{route}")
        metrics.timing(f"proses_{route}", time.time() - started)
        console.print(f"[dim]   ⏱ diproses dalam {time.time() - started:.1f} dtk · {time.strftime('%d-%m %H:%M:%S')}"
                      f"{' (diucapkan sambil ditulis)' if live is not None and live.speaking() else ''}[/dim]")
        if live is not None and live.speaking() and (live.covered or not response):
            if response:
                console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {response}")
                events.emit("digidaw", text=response, brain=getattr(brain, "_brain_used", "") or "")
            live.wait(timeout=180)             # (already being said: wait for the end before listening again)
        elif response:
            if live is not None and live.speaking():
                live.wait(timeout=180)         # a part was said early; the reply changed after it (a question)
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {response}")
            events.emit("digidaw", text=response, brain=getattr(brain, "_brain_used", "") or "")
            if self._pc_speaks(source):        # (the phone reads it out itself when it is the one to answer)
                tts.speak(response)
        events.set_status("idle")
        if should_exit:
            if brain.consume_restart():
                self.restart_requested = True
                events.emit("shutdown", restart=True)
            self.shutdown.set()
            return
        question = brain.memory_question(text) if response else ""
        if question:
            console.print(f"[bold cyan]DIGIDAW:[/bold cyan] {question}")
            events.emit("digidaw", text=question)
            if self._pc_speaks(source):
                tts.speak(question)
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
            brain.cancel_current()               # it may not open / close / type anything after this answer
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

    _loop_tick = 0.0                 # the listening loop's last turn (systemd's watchdog asks alive())

    def alive(self, max_silence: float = 60) -> bool:
        """Is Digidaw still working? The listening loop turns every 0.2 s; not running at all (no microphone, still
        starting) counts as alive, only a loop that stopped turning doesn't."""
        return not self._loop_tick or time.time() - self._loop_tick < max_silence

    def voice_loop(self):
        stt.stt_engine.on_connectivity = self._connectivity
        stt.stt_engine.needs_full_text = lambda: (self._accepts_unaddressed() or self.brief is not None
                                                  or bool(events.state.get("dictation")))
        stt.stt_engine.media_playing = lambda: config.MEDIA_WORDS_WITHOUT_WAKE and media.is_playing()
        stt.stt_engine.on_wake_hint = self.wake_hint
        stt.stt_engine.on_wake_unconfirmed = self.wake_unconfirmed
        stt.stt_engine.on_phrase_for_us = tts.prewarm
        import local_stt
        local_stt.preload()
        if not stt.start_background(self.on_heard, self.mic_muted):
            console.print("[bold red]Mikrofon tidak bisa dipakai untuk mendengarkan terus-menerus.[/bold red]")
            if sys.stdin is None:          # no console: the HUD is the only input
                return self.shutdown.wait()
            return self.console_loop()
        aec.start()                        # echo cancellation: Digidaw can be interrupted while it talks
        next_health = time.time() + 600
        try:
            while not self.shutdown.is_set():
                self._loop_tick = time.time()
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

        if wake_echo(text):
            return                     # "Digidaw, digidaw, digidaw…" heard in music or noise
        if aec.active() and started < tts.voice_ended_at() - 0.4 and not self._barge_in(text):
            return                     # heard over Digidaw's own voice: only "Digidaw …" / "stop" interrupt it
        addressed, command, loose = detect_wake(text)
        if not addressed and after_misheard_name(command) != command:
            text = command = after_misheard_name(command)
        if not addressed and (time.time() < self.armed_until or self.in_followup()) and sounds_like_call(text) \
                and not safety.has_pending():
            console.print(f"[dim]· \"{text}\" dianggap panggilan \"Digidaw\" (salah dengar)[/dim]")
            addressed, command, loose = True, "", False
        if addressed and not loose and command and stray_before_call(text, command):
            command = ""               # "book Digidaw": a stray word, then the call
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
        if addressed and loose and command and not (ARMED_COMMAND_RE if _alias_first(text) else COMMAND_START_RE).match(command):
            addressed = False          # "…lagi dong" at the end of someone's sentence
        if not addressed and own_echo(text):
            console.print(f"[dim]· terdengar: {len(text.split())} kata (diabaikan — suara Digidaw sendiri)[/dim]")
            metrics.count("gema_sendiri")
            return
        if not addressed and self._doubted_at and ended <= self._doubted_at:
            return                     # the very phrase the chime answered (Whisper: "Digidaw"), not what comes next
        armed_loose = self._armed_loose and time.time() < self.armed_until
        if not addressed and armed_loose and not self._sounds_like_command(text):
            # the call wasn't sure and this isn't a command either: chatter, or a command badly heard. One such phrase
            # is forgiven (it made the user call again, twice in one evening); a second one ends the wait
            if self._loose_rejected:
                self.armed_until = 0.0
            self._loose_rejected = True
            console.print(f"[dim]· terdengar: {len(text.split())} kata (diabaikan — dipanggil dengan nama mirip, "
                          f"kalimatnya tidak seperti perintah)[/dim]")
            metrics.count("dibuang_nama_mirip")
            events.emit("heard", text=text, ignored=True)
            return
        if not addressed and self._followup_only is not None and self.in_followup() \
                and time.time() >= self.armed_until and not safety.has_pending() \
                and not self._followup_only.fullmatch(text.strip(" .,!?").lower()):
            # after "Mau saya bacakan?" only an answer counts: a TV or people talking nearby is not a command
            console.print(f"[dim]· terdengar: {len(text.split())} kata (diabaikan — bukan jawaban)[/dim]")
            return
        if not addressed and not self._accepts_unaddressed():
            detail = f"\"{text}\"" if config.LOG_OVERHEARD else f"{len(text.split())} kata"
            console.print(f"[dim]· terdengar: {detail} (diabaikan — panggil \"Digidaw\" dulu; "
                          f"{time.time() - ended:.1f} dtk setelah selesai bicara)[/dim]")
            metrics.count("terdengar_diabaikan")
            events.emit("heard", text=text, ignored=True)
            return
        if not command:            # just "Digidaw" — the command follows in the next phrase
            if loose and len(text.split()) > 3:
                return                 # an alias inside a longer sentence isn't a call
            if self._early_armed_at and time.time() - self._early_armed_at < 10 and self._awaiting_command:
                self.armed_until = time.time() + config.WAKE_LISTEN_SECONDS   # already chimed (wake_hint)
                loose = False          # the laptop itself heard "Digidaw" (Google wrote "digital"): a sure call
            else:
                self.arm()
            self._early_armed_at = 0.0
            self._armed_loose = loose
            return
        self._armed_loose = False
        self._doubted_at = 0.0
        if CLOSING_RE.fullmatch(command.strip(" .,!?")) and not safety.has_pending() and not screen_agent.waiting():
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
        metrics.count("perintah_suara")
        metrics.timing("dikenali", time.time() - ended)
        if self._awaiting_command and time.time() < self.armed_until + 2:
            metrics.count("ting_berhasil")
            self._awaiting_command = self._ting_undone = False       # (the chime's outcome is counted once)
        elif addressed:
            metrics.count("panggilan_langsung")
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
