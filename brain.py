import re
import time
import datetime
import functools

from rich.console import Console
from rich.markup import escape

import config
import events
import llm
import memory
import safety
import system_control as sc
import media
import window_manager
import ai_skills
import protocols
import hud_server
import project_builder
import security_scan
import screen_context
import generator
import alarms
import antigravity_analysis
import habits
import persona
import local_llm

console = Console()
T = config.USER_TITLE

_history: list = []            # the conversation, as google.genai Content objects
_tool_results: list[str] = []
_system_notes: list[str] = []  # things that happened outside Gemini (fast commands, confirmations)

MAX_HISTORY_CONTENTS = 40      # trim older turns beyond this to keep requests fast
KEEP_HISTORY_CONTENTS = 16

# Tools that return raw data Gemini still has to turn into an answer. Every other tool already
# returns the finished sentence, so DIGIDAW speaks it without a second Gemini round-trip.
NEEDS_REPLY_TOOLS = {"web_search", "get_news", "find_files", "list_running_apps", "list_windows",
                     "get_clipboard", "recall_memories", "list_protocols", "run_command", "daily_briefing",
                     "get_selected_text"}
# other ways models address the user; replaced by the configured title
OTHER_TITLES = r"sir|tuan|king|bos|boss|master|kak|mas|pak|madam|nyonya|tuanku"
# "buka firefox dan cari instagram": several commands in one sentence
COMPOUND_SPLIT = re.compile(r"\s+(?:dan|lalu|terus|kemudian|habis itu|setelah itu|sesudah itu|and then|then)\s+"
                            r"(?=(?:buka|tutup|putar|putarkan|mainkan|setel|cari|carikan|search|atur|set|naikkan|turunkan|"
                            r"kecilkan|besarkan|keraskan|matikan|nyalakan|ambil|tangkap|tulis|catat|kunci|pindah|pindahkan|pindahin|minimize|"
                            r"maximize|screenshot|jeda|pause|lanjutkan|stop|hentikan|kirim|mute|unmute|simpan|salin)\b)")
BROWSER_OPEN = re.compile(r"(?:buka|jalankan|open) (chrome|google chrome|firefox|mozilla|edge|microsoft edge|brave|opera)")
WEB_STEP = re.compile(r"(?:cari|carikan|search|buka|open|ke) (?:di (?:google )?)?(.+)")
NUMBER_WORDS = {"satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7, "delapan": 8,
                "sembilan": 9, "sepuluh": 10}
# ...unless the user also asked a question that needs explaining
QUESTION_RE = re.compile(r"\?|\b(?:siapa|apakah|kenapa|mengapa|bagaimana|gimana|jelaskan|ceritakan|"
                         r"kapan|dimana|di mana|who|why|how|explain)\b", re.IGNORECASE)

TAB_WORD = re.compile(r"\btab\b", re.IGNORECASE)
_user_text = ""                # the command Gemini is working on (for guards on its tool calls)
# "bukan itu, maksud saya X" / "salah, yang saya maksud X" right after a command: fix it and learn the word
CORRECTION_RE = re.compile(
    r"(?:(?:bukan|salah)(?: itu| yang itu| (?P<wrong>.+?))?(?: (?:tapi|tetapi))? (?:maksud(?:nya|ku| saya| aku)?|yang (?:saya|aku) maksud(?:kan)?) (?P<right>.+)"
    r"|(?:maksud(?:nya|ku| saya| aku)|yang (?:saya|aku) maksud(?:kan)?) (?:itu |adalah )?(?P<right2>.+))")
CORRECTION_WINDOW = 120        # seconds after a command in which "maksud saya ..." corrects it
TARGET_ARGS = ("name", "site", "query", "workspace", "names")
# names of things (apps, sites, windows) are worth learning as mishearings; song titles and search queries aren't
LEARN_FROM_TOOLS = {"open_application", "open_website", "open_many", "focus_window", "close_application", "close_many",
                    "move_to_workspace", "close_tab", "switch_tab"}
DICTATION_RE = re.compile(r"^\s*(?:ketik|ketikkan|tulis|tuliskan|catat|catatan|dikte)\b", re.IGNORECASE)
_last_action: dict = {}        # text, calls, at — the previous command, for corrections

EXIT_PHRASES = {"keluar", "exit", "quit", "shutdown digidaw", "matikan digidaw", "tutup digidaw",
                "goodbye", "selamat tinggal", "sampai jumpa"}


# ─────────────────────────── tool registry ───────────────────────────

def _logged(fn):
    """Wrap a skill so every tool call is shown on the console + HUD and its result recorded."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        arg_str = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        console.print(f"[dim cyan]  ⚙ {fn.__name__}({arg_str})[/dim cyan]")
        events.emit("tool", name=fn.__name__, args={k: str(v)[:80] for k, v in kwargs.items()})
        try:
            result = fn(*args, **kwargs)
        except ModuleNotFoundError as e:
            import oscompat
            result = (f"Modul Python '{e.name}' belum terinstall. Jalankan setup_digidaw "
                      f"atau: {oscompat.venv_pip_hint(e.name)}")
        except Exception as e:
            result = f"Error saat menjalankan {fn.__name__}: {e}"
        _tool_results.append(str(result))
        _tools_used.append(fn.__name__)
        _calls_this_turn.append((fn.__name__, dict(kwargs), str(result)))
        try:
            habits.record(fn.__name__, kwargs, str(result))
        except Exception:
            pass
        return result
    return wrapper


_tools_used: list[str] = []
_calls_this_turn: list[tuple[str, dict, str]] = []     # (tool, kwargs, result) of the current command
# tools that start work in the background: no open "follow-up" window afterwards, so a stray word
# ("Star") isn't taken as a second image / file / scan
BACKGROUND_TOOLS = {"generate_image", "create_file", "security_scan", "defender_scan", "improve_project",
                    "restore_project_version", "scan_file", "analisis_sistem"}


def started_background_job() -> bool:
    return any(t in BACKGROUND_TOOLS for t in _tools_used)


DROPPED_TOOLS: list[str] = []     # tools Gemini couldn't read (left out instead of breaking every request)


def self_check() -> dict:
    """One tiny request with exactly the setup every command uses (system prompt + all tools), so a broken
    tool, a rejected key or a missing connection shows up right away instead of at the first real command."""
    from google.genai import types
    result = {"ok": False, "model": "", "ms": 0, "dropped": list(DROPPED_TOOLS), "reason": ""}
    try:
        client = llm.get_client()
    except Exception:
        client = None
    if not client:
        llm.mark_failed(tried=False)
        result["reason"] = llm.short_reason()
        return result
    llm.mark_checking()
    tried = False
    for model in llm.candidates()[:2]:
        tried = True
        started = time.time()
        try:
            cfg = _generation_config(model).model_copy(update={
                "max_output_tokens": 16,
                "tool_config": types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))})
            llm.record_call(model)
            client.models.generate_content(model=model, contents="Balas hanya dengan satu kata: OK", config=cfg)
            llm.mark_ok(model)
            result.update(ok=True, model=model, ms=int((time.time() - started) * 1000))
            return result
        except Exception as e:
            console.print(f"[dim red]  [cek AI {model}] {escape(llm.report_error(model, e))}[/dim red]")
            if llm.classify(e) in llm.GLOBAL_FAILURES:
                break
    llm.mark_failed(tried)
    result["reason"] = llm.short_reason()
    return result


def ai_status() -> str:
    """Check right now whether the AI (Gemini) works, and if not, why ('status AI', 'cek koneksi AI',
    'kenapa AI tidak jalan')."""
    r = self_check()
    local = (f" Otak lokal {config.LOCAL_MODEL} siap." if local_llm.available(refresh=True)
             else f" Otak lokal tidak aktif: {local_llm.status_reason()}.")
    if config.BRAIN_MODE == "gemini":
        local = ""
    if r["ok"]:
        extra = (f" Catatan: {len(r['dropped'])} fitur dinonaktifkan karena error ({', '.join(r['dropped'][:3])})."
                 if r["dropped"] else "")
        return (f"AI berfungsi normal, {T}. Model {r['model'].replace('gemini-', '')} menjawab dalam "
                f"{r['ms'] / 1000:.1f} detik.{local}{extra}")
    return f"Gemini sedang tidak bisa dipakai, {T}: {r['reason']}.{local}"


def say(text: str) -> str:
    """Say exactly this text out loud ('katakan halo', 'ucapkan selamat ulang tahun')."""
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text[-1:] in ".!?" else ".")


def learned_habits() -> str:
    """What Digidaw has learned by itself about the user: favourite apps, sites, music and the misheard
    words it now corrects ('apa yang kamu pelajari tentang saya', 'apa kebiasaan saya')."""
    return habits.summary()


def forget_correction(word: str) -> str:
    """Forget a learned speech correction (the misheard word, e.g. 'ho') when it was learned wrongly."""
    return (f"Koreksi untuk '{word}' sudah saya lupakan, {T}." if habits.forget_alias(word)
            else f"Tidak ada koreksi tersimpan untuk '{word}', {T}.")


ALL_SKILLS = (sc.AI_TOOLS + media.AI_TOOLS + window_manager.AI_TOOLS + ai_skills.AI_TOOLS
              + protocols.AI_TOOLS + hud_server.AI_TOOLS + project_builder.AI_TOOLS + security_scan.AI_TOOLS + antigravity_analysis.AI_TOOLS + screen_context.AI_TOOLS + generator.AI_TOOLS + alarms.AI_TOOLS + [ai_status, learned_habits, forget_correction, say])
def _usable(fn) -> bool:
    """Gemini must be able to build a schema from the function signature; one bad tool would make every
    AI request fail, so such a tool is left out (and reported) instead."""
    try:
        from google.genai import types
        types.FunctionDeclaration.from_callable_with_api_option(callable=fn, api_option="GEMINI_API")
        return True
    except Exception as e:
        print(f"[brain] tool {fn.__name__} dilewati: tidak bisa dibaca Gemini ({str(e)[:120]})")
        DROPPED_TOOLS.append(fn.__name__)
        return False


TOOLS = [_logged(f) for f in ALL_SKILLS if _usable(f)]
TOOL_MAP = {f.__name__: f for f in TOOLS}


# ─────────────────────────── Gemini agent ───────────────────────────

def _system_prompt() -> str:
    now = datetime.datetime.now()
    prompt = config.SYSTEM_PROMPT + f"\nCurrent date/time: {sc.HARI[now.weekday()]}, {now:%d-%m-%Y %H:%M}."
    prompt += (f"\nThe user's title is '{T}' (exactly this spelling and capitalisation; never Sir, Tuan, King or another "
               f"title). Use it sparingly, not in every reply.")
    prompt += ("\nCall yourself 'aku' and the user 'kamu' (close, friendly)." if config.ADDRESS_STYLE == "kamu"
               else "\nCall yourself 'saya' and the user 'Anda' (never 'aku/kamu'), relaxed and warm.")
    try:
        prompt += screen_context.prompt_hint()
    except Exception:
        pass
    if config.PROJECT_DIR:
        prompt += (f"\nThe user's focus project folder is: {config.PROJECT_DIR}. When they say 'project', "
                   "'proyek saya' or ask to find/create files without a location, it's this folder "
                   "(open_project opens it).")
    try:
        prompt += habits.prompt_hint()
    except Exception:
        pass
    facts = memory.all_facts()
    if facts and len(facts) <= memory.ALL_IN_PROMPT:
        prompt += "\nThings you remember about the user:\n" + "\n".join(f"- {f}" for f in facts)
    return prompt


def _enrich_with_memory(message: str) -> str:
    """Prepend semantically relevant memories to the user message for better context."""
    try:
        if not memory.needs_lookup():
            return message                      # every fact is already in the system prompt
        relevant = memory.retrieve_relevant(message)
        if relevant:
            return f"[Konteks memori: {relevant}]\n{message}"
    except Exception:
        pass
    return message


def _generation_config(model: str):
    from google.genai import types
    return types.GenerateContentConfig(
        system_instruction=_system_prompt(),
        tools=TOOLS,
        temperature=0.6,
        max_output_tokens=1024,
        thinking_config=llm.thinking_config(model),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _trimmed_history(history: list) -> list:
    """Keep the recent turns, starting at a plain user message (never mid tool-call)."""
    recent = history[-KEEP_HISTORY_CONTENTS:]
    for i, content in enumerate(recent):
        parts = content.parts or []
        if content.role == "user" and parts and all(p.text for p in parts):
            return recent[i:]
    return []


def _commit(contents: list):
    global _history
    _history = contents if len(contents) <= MAX_HISTORY_CONTENTS else _trimmed_history(contents)


def reset_conversation():
    global _history
    _history = []
    _dialog.clear()
    _unsynced.clear()


def _combine(results: list[str]) -> str:
    """Turn one or more tool results into what DIGIDAW says."""
    parts = []
    for r in (r.strip() for r in results):
        if r.startswith(safety.CONFIRMATION_TOKEN):
            r = f"Apakah Anda yakin ingin {safety.pending_description()}, {T}? Jawab ya atau tidak."
        parts.append(r)
    # "A, Sir. B, Sir." -> "A. B, Sir."
    parts = [p.removesuffix(f", {T}.").rstrip(".") + "." for p in parts[:-1]] + parts[-1:]
    return " ".join(parts)


def _run_turn(model: str, message: str, direct_ok: bool) -> str:
    """One user turn with manual function calling. The history is only updated on success."""
    from google.genai import types
    client = llm.get_client()
    gen_config = _generation_config(model)
    contents = list(_history) + [types.Content(role="user", parts=[types.Part(text=message)])]
    for _ in range(config.MAX_TOOL_CALLS):
        llm.record_call(model)
        response = client.models.generate_content(model=model, contents=contents, config=gen_config)
        if not response.candidates or response.candidates[0].content is None:
            raise RuntimeError("respons kosong dari Gemini")
        contents.append(response.candidates[0].content)
        calls = response.function_calls or []
        if not calls:
            _commit(contents)
            return llm.clean(response.text)

        results = []
        for call in calls:
            fn = TOOL_MAP.get(call.name)
            if call.name in ("close_application", "kill_process") and TAB_WORD.search(_user_text):
                results.append("DITOLAK: user meminta menutup sebuah TAB, bukan seluruh aplikasi/browser. "
                               "Pakai close_tab dengan nama tabnya (atau close_current_tab untuk tab yang aktif).")
                continue
            results.append(str(fn(**(call.args or {}))) if fn else f"Tool '{call.name}' tidak tersedia.")
        contents.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name,
                                                                response={"result": result}))
            for call, result in zip(calls, results)
        ]))
        if direct_ok and not any(call.name in NEEDS_REPLY_TOOLS for call in calls):
            reply = _combine(results)
            contents.append(types.Content(role="model", parts=[types.Part(text=reply)]))
            _commit(contents)
            return reply
    raise RuntimeError("terlalu banyak langkah tool")


def ask_agent(text: str) -> str | None:
    """Let Gemini understand the request and run whatever skills it needs.
    Returns None if the AI is unavailable so the caller can fall back to local rules."""
    try:
        if not llm.get_client():
            llm.mark_failed(tried=False)
            return None
    except Exception as e:
        console.print(f"[dim red][Gemini Init Error] {escape(str(e))}[/dim red]")
        return None

    global _user_text
    _user_text = text
    message = text
    if _system_notes:
        message = "[Catatan sistem: " + " | ".join(_system_notes[-6:]) + "]\n" + text
        _system_notes.clear()
    message = _enrich_with_memory(message)
    direct_ok = not QUESTION_RE.search(text)

    # always start from the fastest model that is available right now (never "sticks" to a slow one)
    tried, slow_failures = False, 0
    for model in llm.candidates():
        tried = True
        _tool_results.clear()
        had_pending = safety.has_pending()
        try:
            reply = _run_turn(model, message, direct_ok)
        except Exception as e:
            console.print(f"[dim red]  [{model}] {escape(llm.report_error(model, e))}[/dim red]")
            if _tool_results:  # a skill already ran; don't repeat it via fallback
                llm.mark_ok(model)
                return _combine(_tool_results[-1:])
            kind = llm.classify(e)
            if kind in llm.GLOBAL_FAILURES:
                break          # no internet / bad key / a bug: the next model would fail the same way
            if kind == "busy":
                slow_failures += 1
                if slow_failures >= 3:
                    break      # Google is slow right now: don't make the user wait for every model
            continue           # quota / retired model: the next one answers immediately
        llm.mark_ok(model)

        if safety.has_pending() and not had_pending and "?" not in reply:
            reply = f"Apakah Anda yakin ingin {safety.pending_description()}, {T}? Jawab ya atau tidak."
        if not reply:
            reply = _combine(_tool_results[-1:]) if _tool_results else f"Selesai, {T}."
        return reply

    llm.mark_failed(tried)
    return None


# ───────────────────────── local brain (Ollama) ─────────────────────────
# Everyday commands and light questions go to the local model: instant, offline and free. Gemini takes what
# needs the internet (news, prices, "terbaru"), the screen, documents and the big background jobs, and
# whatever the local model is unsure about.

LOCAL_TOOL_NAMES = [
    "open_application", "open_many", "open_website", "close_application", "close_many", "focus_window",
    "window_action", "list_windows", "switch_workspace", "move_to_workspace", "close_tab", "switch_tab", "close_current_tab", "navigate",
    "play_youtube", "media_control", "now_playing", "play_spotify", "set_volume", "volume_up", "volume_down",
    "volume_mute", "set_brightness", "take_screenshot", "type_text", "press_keys", "write_note", "read_last_note",
    "set_timer", "set_alarm", "set_reminder_at", "list_timers", "cancel_timers", "get_time", "get_date",
    "get_weather", "get_system_status", "open_folder", "open_file", "find_files", "remember", "recall_memories",
    "learned_habits", "lock_workstation", "shutdown_pc", "restart_pc", "sleep_pc", "google_search",
]
# words that mean the answer has to come from the internet, the screen, a document or a big Gemini skill
ONLINE_TOPIC_RE = re.compile(
    r"\b(?:berita|news|headline|harga|kurs|nilai tukar|saham|bitcoin|crypto|kripto|ihsg|skor|klasemen|"
    r"hasil (?:pertandingan|pemilu|liga)|jadwal (?:pertandingan|tayang|rilis|kereta|pesawat|sholat|shalat)|"
    r"trending|viral|gempa|banjir|cari(?:kan)? (?:di |informasi |info )?(?:internet|google|web|online)|"
    r"googling|browsing|search|cek (?:di )?internet|link|website (?:resmi|untuk))\b")
RECENT_RE = re.compile(r"\b(?:terbaru|terkini|hari ini|kemarin|semalam|minggu ini|bulan ini|tahun ini|barusan|"
                       r"sekarang|saat ini|update|20[2-3]\d)\b")
ASKING_RE = re.compile(r"\b(?:apa|apakah|siapa|berapa|kapan|bagaimana|gimana|mana|kenapa|mengapa|ada)\b|\?")
GEMINI_SKILL_RE = re.compile(
    r"\b(?:layar|screen|lihat ini|baca ini|apa ini|error (?:apa|ini)|dokumen|file ini|pdf|halaman ini|artikel ini|"
    r"rangkum|ringkas|terjemahkan|teks yang (?:saya |aku )?(?:blok|pilih|sorot)|project|proyek|gambar|logo|poster|"
    r"wallpaper|presentasi|excel|word|protokol|protocol|scan|pindai|keamanan (?:laptop|komputer|pc|sistem)|"
    r"(?:cek|ada|scan|pindai) virus|malware|diagnos|briefing|"
    r"clipboard|perintah terminal|jalankan perintah)\b")
# questions that need real reasoning or a long, careful answer: the bigger model
COMPLEX_RE = re.compile(
    r"\b(?:analisis|analisa|analisislah|bandingkan|perbandingan|perbedaan|kelebihan dan "
    r"kekurangan|pro dan kontra|untung rugi|jelaskan (?:secara )?(?:detail|lengkap|mendalam|rinci)|secara (?:detail|"
    r"lengkap|mendalam|rinci)|langkah[- ]langkah|step by step|tutorial|strategi|rencana|rencanakan|susun(?:kan)?|"
    r"evaluasi|kritik|review|ulas|esai|essay|artikel|makalah|skripsi|proposal|"
    r"kode|coding|program(?:nya)?|script|skrip|python|javascript|html|css|sql|algoritma|debug|bug|error di|"
    r"rumus|persamaan|integral|turunan|matriks|probabilitas|statistik|hukum|pasal|medis|diagnosis|gejala|obat|dosis)\b")
LOCAL_UNSURE_RE = re.compile(
    r"tidak (?:tahu|yakin|memiliki (?:akses|informasi|data))|tidak (?:bisa|dapat) mengakses|informasi (?:terbaru|terkini|real)|"
    r"real-?time|batas pengetahuan|knowledge cutoff|data (?:saya|terakhir)|sebagai (?:model|ai)|i don't know|"
    r"tidak dapat (?:memberikan|mengetahui)|cek (?:sumber|situs) resmi|tidak ada tool|"
    r"tidak (?:bisa|dapat|mampu) (?:melakukan|mengubah|mengatur|membantu|menjalankan|membuka)", re.IGNORECASE)
LOCAL_MAX_STEPS = 4
DEICTIC_RE = re.compile(r"\b(?:ini|itu|tersebut|halaman|jendela|layar|tab|aplikasi yang|yang (?:sedang )?(?:dibuka|aktif))\b")
# a reply that says something was done ("Membuka tik", "Mencari YouTube di browser") although no tool ran
ACTION_CLAIM_RE = re.compile(
    r"^(?:baik[,.]? |oke[,.]? |siap[,.]? )?(?:saya )?(?:sedang |akan |sudah )?(?:membuka|mencari|memutar|menutup|"
    r"menjalankan|mengatur|menyetel|mengecilkan|membesarkan|menaikkan|menurunkan|mematikan|menyalakan|memindahkan|"
    r"mengetik|menyimpan|mencatat|membuat timer|beralih|mengaktifkan|menonaktifkan|memulai|menekan|pindah ke)\b|\b(?:sudah|telah) (?:di)?(?:jalankan|tekan|klik|ketik|restart|matikan)\w*|\bakan (?:restart|mati|dimatikan|"
    r"di-?restart|tidur)\b|\b(?:saya|aku) (?:sudah |telah )?(?:tekan|klik|pencet)\b|\b(?:sudah|telah|berhasil) "
    r"(?:saya )?(?:di|ter|me)?\w*(?:buka|tutup|putar|atur|setel|mati|nyala|aktif|pindah|catat|simpan|kunci|ketik)\w*\b",
    re.IGNORECASE)
# orders that must DO something on the PC (unlike "buatkan cerita" / "tulis puisi", which are answered in words)
# questions about THIS computer's current state: only a tool knows the answer
STATE_QUESTION_RE = re.compile(
    r"\b(?:berapa|apa saja|sisa|status|penggunaan|pemakaian|kondisi)\b.*\b(?:ram|memori|cpu|baterai|batre|disk|"
    r"penyimpanan|aplikasi|jendela|window|tab|timer|alarm|volume|kecerahan|wifi|jaringan|internet)\b|"
    r"\b(?:ram|memori|cpu|baterai|aplikasi|jendela|timer|alarm|volume)\b.*\b(?:berapa|apa saja|yang (?:terbuka|aktif|berjalan))\b")
# small talk that may mention a PC word ("makasih sudah buka musiknya") but asks for nothing
CHAT_RE = re.compile(r"\b(?:makasih|terima kasih|thanks|mantap|keren|bagus|oke|ok|sip|wah|wow|haha|hehe|begitu dong|"
                     r"ceritakan|jelaskan|terangkan|pantun|puisi|cerita|dongeng|lelucon|joke|lawak|definisi|arti(?:nya)?|"
                     r"maksud(?:nya)?|sejarah|fakta|tips|saran)\b")
ACTION_VERB_RE = re.compile(
    r"^(?:tolong |coba |bisa |bisakah |mohon |dong )?(?:buka|bukakan|tutup|nyalakan|hidupkan|matikan|aktifkan|nonaktifkan|"
    r"putar|putarkan|setel|mainkan|jeda|pause|lanjutkan|hentikan|stop|kecilkan|besarkan|naikkan|turunkan|keraskan|"
    r"pelankan|atur|ubah|ganti|rubah|jalankan|pindah|pindahkan|kirim|ketik|ketikkan|tekan|klik|pencet|kunci|minimize|"
    r"maximize|nonton|tonton|restart|reboot|shutdown|kembali ke|balik ke|"
    r"sembunyikan|tampilkan|perbesar|perkecil|redupkan|terangkan|ingatkan|bangunkan|hapus|simpan|salin|tangkap|ambil)\b")
# "Saya sudah buat puisi dan masukkan ke editor" / "Saya buka tab YouTube" / "sudah saya scan": a claimed deed
DONE_CLAIM_RE = re.compile(
    r"\b(?:saya|aku) (?:sudah |telah |baru saja |barusan )?(?!(?:sudah |telah )?(?:tahu|siap|paham|mengerti|ingat|"
    r"dengar|senang|di sini|bisa|akan|mau|ingin|kira|rasa|yakin|harap|sarankan|rekomendasikan)\b)"
    r"(?:buka|tutup|putar|putarkan|setel|atur|buat|bikin|tulis|catat|masukkan|simpan|kirim|scan|cek|periksa|"
    r"matikan|nyalakan|pindahkan|ketik|hapus|jalankan|cari|carikan)\b|"
    r"\b(?:sudah|telah) (?:saya |aku )?(?:buat|bikin|tulis|catat|masukkan|simpan|kirim|scan|cek|periksa|cari|"
    r"jalankan|hapus|ketik)\w*\b", re.IGNORECASE)
NUDGE = ("[Sistem] Kamu belum memanggil tool apa pun, jadi belum ada yang terjadi. Jika ini permintaan aksi, panggil "
         "tool yang tepat SEKARANG (untuk 'masukkan/tulis ke editor/catatan' pakai write_note dengan isi lengkapnya). "
         "Jika bukan permintaan aksi, jawab ulang langsung tanpa mengaku melakukan apa pun. Jika tidak ada tool yang "
         "cocok, panggil ask_online_ai.")
PLACEHOLDER_ARG_RE = re.compile(r"(?:current|default|auto|none|null|here|sekarang|saat ini|lokasi (?:saya|sekarang)|"
                                r"di sini|otomatis|kota saya|aktif)", re.IGNORECASE)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
DIALOG_TURNS = 6               # recent turns both brains see
_dialog: list[dict] = []       # {"user", "reply", "brain"}: the conversation, whichever brain answered
_unsynced: list[dict] = []     # turns answered without Gemini that Gemini hasn't been told about yet
_brain_used = ""               # "local" / "gemini" / "fast" for the current command (shown on the console)


def ask_online_ai(reason: str) -> str:
    """Hand this request to the online AI (Gemini) when it needs the internet or current information: news, prices,
    exchange rates, scores, schedules, anything after your knowledge, a web search, a complex question that needs
    careful reasoning or a long detailed answer, or when you are not sure.
    reason: what the user needs."""
    return "ESCALATE"


def gemini_reason(text: str) -> str:
    """Why this request should skip the local model ('' = Qwen answers it). Instant keyword check."""
    cmd = _normalize(text)
    if ONLINE_TOPIC_RE.search(cmd):
        return "butuh internet"
    if GEMINI_SKILL_RE.search(cmd):
        return "layar/dokumen/tugas besar"
    if RECENT_RE.search(cmd) and ASKING_RE.search(cmd) and not re.search(r"\b(?:jam|tanggal|hari apa)\b", cmd):
        return "data terbaru"   # "siapa juara liga sekarang" — but "jam berapa sekarang" is local
    if COMPLEX_RE.search(cmd):
        return "pertanyaan kompleks"
    words = len(cmd.split())
    if words > 30 or (words > 14 and len(ASKING_RE.findall(cmd)) >= 3):
        return "pertanyaan panjang"   # several questions in one go
    return ""


def needs_gemini(text: str) -> bool:
    return bool(gemini_reason(text))


# Only the tools a request can need go to the local model: a short prompt keeps the model inside the GPU's
# memory, which makes it 2-3x faster. A command that matches no group gets every tool (nothing is missed).
LOCAL_TOOL_GROUPS = [
    (r"buka|jalankan|open|tutup|close|keluar dari|aplikasi|app|situs|website|web", [
        "open_application", "open_many", "open_website", "close_application", "close_many", "focus_window"]),
    (r"jendela|window|minimi|maksimal|maximi|kecilkan jendela|kiri|kanan|pindah|workspace|ruang kerja|desktop", [
        "window_action", "focus_window", "list_windows", "switch_workspace", "move_to_workspace"]),
    (r"tab|halaman|scroll|gulir|refresh|muat ulang|kembali|mundur", [
        "close_tab", "switch_tab", "close_current_tab", "navigate"]),
    (r"putar|putarkan|setel|mainkan|nyalakan|nonton|tonton|lagu|musik|music|video|youtube|spotify|pause|jeda|lanjut|next|skip|"
     r"berikutnya|sebelumnya|playlist|play|stop|hentikan", [
        "play_youtube", "media_control", "now_playing", "play_spotify"]),
    (r"suara|volume|keras|pelan|kecilkan|besarkan|naikkan|turunkan|mute|bisu|senyap", [
        "set_volume", "volume_up", "volume_down", "volume_mute"]),
    (r"kecerahan|brightness|terang|redup|gelap", ["set_brightness"]),
    (r"catat|catatan|tulis|note|editor|masukkan|simpan ke", ["write_note", "read_last_note"]),
    (r"timer|alarm|ingatkan|ingetin|pengingat|reminder|bangunkan|menit|detik|jam \d|pukul", [
        "set_timer", "set_alarm", "set_reminder_at", "list_timers", "cancel_timers"]),
    (r"status|baterai|batre|cpu|ram|memori|sistem|laptop|komputer|pc", ["get_system_status"]),
    (r"cuaca|hujan|panas|suhu|dingin|cerah|weather", ["get_weather"]),
    (r"file|berkas|folder|direktori|dokumen|unduhan|download", ["open_folder", "open_file", "find_files"]),
    (r"ketik|ketikkan|tekan|shortcut|tombol|enter", ["type_text", "press_keys"]),
    (r"screenshot|tangkap layar|tangkapan layar", ["take_screenshot"]),
    (r"ingat|lupa|tentang saya|tentang aku|kebiasaan|favorit|kesukaan|pelajari", ["recall_memories", "learned_habits"]),
    (r"kunci|matikan|shutdown|restart|mulai ulang|sleep|tidurkan", [
        "lock_workstation", "shutdown_pc", "restart_pc", "sleep_pc"]),
    (r"cari|carikan|google|search", ["google_search"]),
]
LOCAL_TOOL_GROUPS = [(re.compile(rf"\b(?:{p})"), names) for p, names in LOCAL_TOOL_GROUPS]
ALWAYS_LOCAL_TOOLS = ["remember"]
CORE_LOCAL_TOOLS = ["open_application", "open_website", "close_application", "focus_window", "window_action",
                    "play_youtube", "media_control", "set_volume", "write_note", "set_timer", "get_system_status",
                    "google_search"]
COMMAND_VERB_RE = re.compile(r"^(?:tolong |coba |bisa |bisakah |mohon )?(?:buka|tutup|nyalakan|matikan|atur|setel|ubah|ganti|"
                             r"jalankan|hidupkan|pindahkan|kirim|hapus|simpan|bikin|buat|tampilkan|sembunyikan|perbesar|perkecil)\b")
_schema_cache: dict[str, dict] = {}


def _schema(name: str) -> dict:
    if name not in _schema_cache:
        _schema_cache[name] = local_llm.tool_schema(TOOL_MAP[name] if name in TOOL_MAP else ask_online_ai)
    return _schema_cache[name]


def _local_tools(text: str = "", offline: bool = False) -> list[dict] | None:
    """The tools for this request: matching groups + the tools of the previous command (follow-ups such as
    'kecilkan lagi') + remember. None = a command no group covers: Gemini (which has every skill) takes it;
    when Gemini is down the local model gets the core tools instead."""
    cmd = _normalize(text)
    names: list[str] = []
    for pattern, group in LOCAL_TOOL_GROUPS:
        if pattern.search(cmd):
            names += group
    if not names and COMMAND_VERB_RE.search(cmd):
        if not offline:
            return None
        names = list(CORE_LOCAL_TOOLS)
    if _last_action and time.time() - _last_action.get("at", 0) < CORRECTION_WINDOW:
        names += [n for n, _, _ in _last_action["calls"]]
    names += ALWAYS_LOCAL_TOOLS
    seen = []
    for n in names:
        if n in LOCAL_TOOL_NAMES and n in TOOL_MAP and n not in seen:
            seen.append(n)
    return [_schema(n) for n in seen]


def _local_system_prompt(offline: bool) -> str:
    """Only things that rarely change: Ollama reuses the processed prompt (tools included) while it stays the same,
    which saves seconds per command. The clock and the active window go into the user message instead."""
    pronouns = ('sebut dirimu "aku" dan panggil user "kamu", akrab seperti teman dekat.' if config.ADDRESS_STYLE == "kamu"
                else 'sebut dirimu "saya" dan panggil user "Anda" (jangan "aku/kamu"), tetap santai.')
    import random
    hour = datetime.datetime.now().hour
    part = "pagi" if 4 <= hour < 11 else "siang" if hour < 15 else "sore" if hour < 18 else "malam"
    examples = "\n".join(f"User: {q} → {random.choice(answers)}".replace("{part}", part)
                         for q, answers in config.LOCAL_TONE_EXAMPLES.items())
    prompt = config.LOCAL_SYSTEM_PROMPT.format(title=T, pronouns=pronouns, examples=examples)
    if offline:
        prompt += ("\nAI online (Gemini) sedang tidak tersedia. Jawab sebisamu; untuk informasi terbaru katakan "
                   "singkat bahwa itu butuh internet/AI online yang sedang tidak bisa dipakai.")
    try:
        prompt += habits.prompt_hint()
    except Exception:
        pass
    facts = memory.all_facts()
    if facts:
        prompt += "\nYang kamu ingat tentang user:\n" + "\n".join(f"- {f}" for f in facts[:memory.ALL_IN_PROMPT])
    return prompt


def _coerce_args(fn, args: dict) -> dict:
    """Small models send '30' for an int or extra keys: fit the arguments to the skill's signature."""
    import inspect
    import typing
    try:
        params = inspect.signature(fn).parameters
        hints = typing.get_type_hints(fn)
    except (TypeError, ValueError):
        return args
    fixed = {}
    for key, value in (args or {}).items():
        if key not in params:
            continue
        kind = hints.get(key)
        if isinstance(value, str) and PLACEHOLDER_ARG_RE.fullmatch(value.strip()) and params[key].default == "":
            continue        # "current" / "default" / "lokasi saya": the skill's own default does it right
        try:
            if kind is int and not isinstance(value, int):
                value = int(float(str(value).strip().rstrip("%")))
            elif kind is float and not isinstance(value, (int, float)):
                value = float(str(value))
            elif kind is bool and isinstance(value, str):
                value = value.strip().lower() in ("true", "ya", "yes", "1")
            elif kind is str and not isinstance(value, str):
                value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            elif typing.get_origin(kind) is list and isinstance(value, str):
                value = [v.strip() for v in re.split(r",| dan ", value) if v.strip()]
        except (TypeError, ValueError):
            continue
        fixed[key] = value
    return fixed


def ask_local(text: str, offline: bool = False) -> str | None:
    """Let the local model answer / run skills. None = hand the request to Gemini (or it failed)."""
    global _brain_used
    if not local_llm.available():
        return None
    messages = [{"role": "system", "content": _local_system_prompt(offline)}]
    for turn in _dialog[-DIALOG_TURNS:]:
        messages += [{"role": "user", "content": turn["user"][:300]}, {"role": "assistant", "content": turn["reply"][:300]}]
    now = datetime.datetime.now()
    part = "pagi" if 4 <= now.hour < 11 else "siang" if now.hour < 15 else "sore" if now.hour < 18 else "malam"
    context = [f"sekarang {sc.HARI[now.weekday()]} {part}, {now:%d-%m-%Y %H:%M}"]
    if DEICTIC_RE.search(_normalize(text)):         # "tutup ini", "rangkum halaman ini": then the active window matters
        try:
            context.append(screen_context.prompt_hint().strip())
        except Exception:
            pass
    context += _system_notes[-4:]
    messages.append({"role": "user", "content": "[Konteks: " + " | ".join(c for c in context if c) + "]\n" + text})
    tools = _local_tools(text, offline)
    if tools is None:
        console.print("[dim]  ↪ perintah di luar kemampuan otak lokal, diteruskan ke Gemini[/dim]")
        return None
    tools += [] if offline else [_schema("ask_online_ai")]
    direct_ok = not QUESTION_RE.search(text)
    ran: list[str] = []
    nudged = False
    cmd_n = _normalize(text)
    # an order, or a PC request a tool group matched (but not a question): a reply in words only means nothing happened
    pc_request = any(pattern.search(cmd_n) for pattern, _ in LOCAL_TOOL_GROUPS)
    is_action = bool(ACTION_VERB_RE.search(cmd_n)) or (pc_request and not ASKING_RE.search(cmd_n)
                                                     and not CHAT_RE.search(cmd_n)) \
        or bool(STATE_QUESTION_RE.search(cmd_n))     # "berapa ram saya", "aplikasi apa saja yang terbuka"
    try:
        for _ in range(LOCAL_MAX_STEPS):
            msg = local_llm.chat(messages, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = msg.get("content", "").strip()
                if not reply:
                    return _combine(ran[-1:]) if ran else None
                claims = ACTION_CLAIM_RE.search(reply) or DONE_CLAIM_RE.search(reply)
                if not ran and (claims or (is_action and not reply.rstrip().endswith("?"))):
                    if not nudged:          # "Membuka tik" without opening anything: make it really act
                        nudged = True
                        messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": NUDGE}]
                        continue
                    console.print("[dim]  ↪ otak lokal mengaku beraksi tanpa tool, diteruskan ke Gemini[/dim]")
                    return None
                if not offline and not ran and config.BRAIN_MODE == "hybrid" and LOCAL_UNSURE_RE.search(reply):
                    console.print("[dim]  ↪ otak lokal ragu, diteruskan ke Gemini[/dim]")
                    return None
                _brain_used = "local"
                return EMOJI_RE.sub("", reply).strip()
            if any(c["function"]["name"] == "ask_online_ai" for c in calls):
                if ran:
                    return _combine(ran[-1:])
                console.print("[dim]  ↪ butuh info online, diteruskan ke Gemini[/dim]")
                return None
            messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            results = []
            for call in calls:
                name = call["function"]["name"]
                fn = TOOL_MAP.get(name) if name in LOCAL_TOOL_NAMES else None
                if not fn:
                    result = f"Tool '{name}' tidak tersedia."
                else:
                    result = str(fn(**_coerce_args(fn, call["function"].get("arguments") or {})))
                results.append(result)
                messages.append({"role": "tool", "content": result, "tool_name": name})
            ran += results
            if safety.has_pending():
                _brain_used = "local"
                return f"Apakah Anda yakin ingin {safety.pending_description()}, {T}? Jawab ya atau tidak."
            if direct_ok and not any(c["function"]["name"] in NEEDS_REPLY_TOOLS for c in calls):
                _brain_used = "local"
                return _combine(results)
        _brain_used = "local"
        return _combine(ran[-1:]) if ran else None
    except Exception as e:
        console.print(f"[dim red]  [lokal {config.LOCAL_MODEL}] {escape(str(e)[:120])}[/dim red]")
        if "Connection" in type(e).__name__ or "connect" in str(e).lower():
            local_llm.mark_down("Ollama tidak berjalan")
        return _combine(ran[-1:]) if ran else None     # a skill already ran: never run it twice via Gemini


def _think(text: str) -> str | None:
    """Pick the brain for a request that isn't a simple fast command."""
    global _brain_used
    mode = config.BRAIN_MODE
    reason = gemini_reason(text)
    if mode != "gemini" and not reason:
        reply = ask_local(text)
        if reply:
            return reply
    elif reason and mode != "gemini":
        console.print(f"[dim]  ↪ ke Gemini: {reason}[/dim]")
    if _unsynced:                  # tell Gemini what was said while the local model answered
        _system_notes.append("percakapan sebelumnya (dijawab otak lokal): " + " / ".join(
            f"User: {t['user'][:120]} → Digidaw: {t['reply'][:160]}" for t in _unsynced[-4:]))
        _unsynced.clear()
    reply = ask_agent(text)
    if reply:
        _brain_used = "gemini"
        return reply
    if mode != "gemini" or local_llm.available():
        reply = ask_local(text, offline=True)     # Gemini is down: the local model answers what it can
        if reply:
            return reply
    return None


def _remember_turn(user: str, reply: str):
    turn = {"user": user, "reply": reply, "brain": _brain_used or "fast"}
    _dialog.append(turn)
    del _dialog[:-DIALOG_TURNS * 2]
    if turn["brain"] != "gemini":
        _unsynced.append(turn)
        del _unsynced[:-6]


# ───────────────────────── fast path (no Gemini) ─────────────────────────
# Short, unambiguous commands run instantly without a round-trip to Gemini. Anything that doesn't
# match these exact shapes still goes to Gemini, which understands free-form requests.

VAGUE_WORDS = {"yang", "favorit", "favoritku", "kesukaan", "kesukaanku", "tadi", "itu", "ini", "saya",
               "aku", "kemarin", "sebelumnya", "lagi", "apa", "sesuatu", "random", "acak", "enak", "bagus"}


def _normalize(text: str) -> str:
    t = re.sub(r"[.,!?;:]+", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:tolong|coba|mohon|bisa|bisakah|please)\s+", "", t)
    return re.sub(rf"\s+(?:dong|ya|yah|deh|please|{OTHER_TITLES}|{re.escape(T.lower())})$", "", t)


def fix_title(text: str) -> str:
    """'Baik, Sir.' / 'Maaf, King,' -> the configured title."""
    pattern = rf"(?<=[,] )(?:{OTHER_TITLES}|{re.escape(T)})(?=[.!?,;:]|\s*$)"
    text = re.sub(pattern, T, text, flags=re.IGNORECASE)
    return re.sub(rf"^(?:{OTHER_TITLES})(?=, )", T, text, flags=re.IGNORECASE)


def _num(text) -> int:
    if not text:
        return 0
    text = str(text).strip().lower()
    return int(text) if text.isdigit() else NUMBER_WORDS.get(text, 0)


def _flagged_delete():
    path = security_scan._state.get("flagged_file")
    return ("delete_file", {"path": path}) if path else None


def _play(query: str, mix: bool = False):
    query = query.strip()
    if ALARM_WORDS.search(query):
        return None   # "setel alarm jam ..." is an alarm, not a song
    if not query or set(query.split()) & VAGUE_WORDS:
        return None   # "putar lagu yang tadi" needs Gemini's context
    return "play_youtube", {"query": query, "mix": mix}


OPEN_MANY_SPLIT = re.compile(r"\s+(?:dan|sama|serta|juga|plus|and|lalu|terus)\s+")


def _open_many(target: str):
    """'terminal dan mousepad' / 'terminal mousepad dan firefox' / '2 terminal' / 'terminal dan terminal lagi'
    -> open_many, when every part is an app or site."""
    count = re.fullmatch(rf"(\d|{'|'.join(NUMBER_WORDS)}) (.+)", target)
    if count and 2 <= _num(count[1]) <= 6:
        one = _open_known(count[2], single=True)
        if one and one[0] == "open_application":
            return "open_many", {"names": [count[2]] * _num(count[1])}
    parts = [re.sub(r" (?:lagi|juga|satu lagi)$", "", p) for p in OPEN_MANY_SPLIT.split(target) if p]
    names = []
    for part in parts:
        if _open_known(part, single=True):
            names.append(part)
            continue
        words = part.split()            # commas are gone after _normalize: "terminal mousepad"
        if len(words) < 2 or not all(_open_known(w, single=True) for w in words):
            return None
        names += words
    if len(names) < 2 or not all(_open_known(n, single=True)[0] in ("open_application", "open_website") for n in names):
        return None
    return "open_many", {"names": names}


def _open_known(target: str, single: bool = False):
    target = target.strip()
    if not single:
        many = _open_many(target)
        if many:
            return many
    site = re.fullmatch(r"(?:situs|website|web|laman|halaman) (.+)", target)
    if site:
        target = site.group(1)          # "situs lms darmajaya" -> Gemini knows the address better
        if not (target in sc.WEBSITES or re.fullmatch(r"[\w-]+(?:\.[\w-]+)+(?:/\S*)?", target)):
            return None
    drive = re.fullmatch(r"(?:drive|disk|local disk|partisi) ?([a-z])(?::)?|([a-z]):", target)
    if drive:
        return "open_folder", {"name": (drive.group(1) or drive.group(2)).upper()}
    if re.fullmatch(r"(?:file|folder|direktori) (?:project|proyek)(?: (?:saya|kita|ku))?", target):
        return "open_project", {}
    bare = re.sub(r"^(?:tab|situs|halaman|web) ", "", target)        # "buka tab youtube" -> youtube
    if bare.replace(" ", "") in sc.WEBSITES:
        target = bare
    if target.replace(" ", "") in sc.WEBSITES:
        target = target.replace(" ", "")              # "tik tok" -> tiktok
    if target in sc.APPS:
        return "open_application", {"name": target}
    if target in sc.WEBSITES or re.fullmatch(r"[\w-]+(?:\.[\w-]+)+(?:/\S*)?", target):
        return "open_website", {"site": target}
    prefixed = [k for k in sc.WEBSITES if len(target) >= 3 and k.replace(" ", "").startswith(target.replace(" ", ""))]
    if len(prefixed) == 1 and not (sc._start_apps_cache and sc._find_start_app(target)):
        return "open_website", {"site": prefixed[0]}      # "buka tik" (cut off) -> tiktok
    if sc._start_apps_cache and len(target) >= 3 and sc._find_start_app(target):
        return "open_application", {"name": target}      # any installed app (list already loaded)
    return None


def _close_known(target: str, single: bool = False):
    target = target.strip()
    if re.fullmatch(r"(?:web |semua )?browser(?:nya)?|peramban", target):
        return "close_many", {"names": ["browser"]}
    if not single and OPEN_MANY_SPLIT.search(target):   # "tutup firefox dan mousepad"
        parts = [p for p in OPEN_MANY_SPLIT.split(target) if p]
        known = [_close_known(p, single=True) for p in parts]
        if len(parts) >= 2 and any(known):       # an app that isn't running is simply reported as such
            return "close_many", {"names": [(k[1].get("names") or [k[1].get("name")])[0] if k else p
                                            for k, p in zip(known, parts)]}
    if target in sc.PROCESS_ALIASES:
        return "close_application", {"name": target}
    if len(target) >= 3 and not re.search(r"\b(?:semua|tab|ini|itu|jendela)\b", target) and sc._matching_processes(target):
        return "close_application", {"name": target}      # a running app, e.g. "tutup teamviewer"
    if len(target) >= 3 and not re.search(r"\b(?:semua|tab|ini|itu|jendela)\b", target):
        import difflib
        import window_manager
        running = {w["process"].lower().removesuffix(".exe"): w["process"] for w in window_manager._windows()
                   if not window_manager._is_digidaw(w)}
        key = target.replace(" ", "")
        close = difflib.get_close_matches(key, list(running), n=1, cutoff=0.6) or \
            difflib.get_close_matches(key, [r for r in running if r[:2] == key[:2]], n=1, cutoff=0.5)
        if close:                                          # misheard: "tutup nope" -> notepad
            return "close_application", {"name": close[0]}
    return None


# speech recognition often hears "workspace 4" as "word PS4" / "WordPress 4" / "works ps 2"
WORKSPACE = r"(?:workspace|work space|workspes|wokspace|ruang kerja|desktop|words? ?ps|works? ?ps|for ps|wordpress|word press|work)"
WS_NUM = r"(?:ke-? ?|nomor |no )?(?:\d{1,2}|" + "|".join(NUMBER_WORDS) + ")"
WS_GLUED = re.compile(r"\b(words?|works?|work|for) ?ps(\d{1,2})\b")      # "word ps4" -> "word ps 4"


def _ws(words: str) -> str:
    word = re.split(r"[\s-]", words)[-1]          # "ke-4" / "nomor empat" -> the number
    return str(_num(word) or word)


def _ws_route(name: str, kwargs: dict):
    return (name, kwargs) if name in TOOL_MAP else None      # workspaces exist on Linux (Hyprland) only


FAST_ROUTES = [(re.compile(p), h) for p, h in [
    # ── "cari ..." with a site: search on YouTube, or just open the site ("cari youtube" cut off by the mic) ──
    (r"(?:cari|carikan|search)(?: di)? youtube (?!di$)(.+)|(?:cari|carikan|search) (.+) di youtube",
     lambda m: ("search_youtube", {"query": m[1] or m[2]})),
    (r"(?:cari|carikan|search)(?: di)? ([\w ]+?)(?: di| dong)?",
     lambda m: ("open_website", {"site": m[1]}) if m[1] in sc.WEBSITES else None),
    (r"(?:nonton|tonton|menonton|lihat video|tampilkan video)(?: video)? (?!apa\b)(.{3,}?)(?: di youtube)?",
     lambda m: _play(m[1])),
    (r"(?:skip|lewati|lewatin|lompati) (?:iklan|ads|ad)(?:nya)?(?: youtube)?", lambda m: ("media_control", {"action": "skip_ad"})),
    # ── is the AI working? (must work without the AI) ──
    (r"(?:cek|tes|test|periksa|status)(?: koneksi)? (?:ai|a i|gemini|otak(?: ai)?)(?: (?:saya|digidaw|nya))?|"
     r"(?:apakah|kenapa) ai(?: ?nya)? (?:berfungsi|jalan|normal|tidak (?:jalan|berfungsi|bisa)|error)",
     lambda m: ("ai_status", {})),
    # ── the HUD window ──
    (r"(?:buka|tampilkan|munculkan|perlihatkan|lihat|open|show|keluarkan)(?: lagi)? (?:hud|h u d|hut|had|dashboard|"
     r"tampilan(?: digidaw)?|panel digidaw|aplikasi digidaw|jendela digidaw|layar digidaw|digidaw)(?: nya| digidaw)?",
     lambda m: ("show_hud", {})),
    (r"(?:tutup|sembunyikan|minimize|minimalkan|kecilkan) (?:hud|h u d|hut|dashboard|tampilan digidaw|jendela digidaw)(?:nya)?",
     lambda m: ("hide_hud", {})),
    # ── open a built project by name ──
    (r"(?:buka|jalankan|lihat|tampilkan) (?:project|proyek) (?!saya$|ku$)(.+)",
     lambda m: ("open_last_project", {"project": "" if re.fullmatch(r"(?:yang )?(?:terakhir|terbaru)(?: (?:di)?buat)?", m[1]) else m[1]})),
    # ── generate images / files with Antigravity ──
    (r"(?:buat|buatkan|bikin|bikinkan|generate|gambarkan|lukiskan|desain|desainkan)(?: saya| aku| kan)?(?: sebuah| satu| (\d|dua|tiga|empat))?"
     r" (?:gambar|foto|ilustrasi|logo|poster|wallpaper|ikon|icon|banner|lukisan|karikatur|stiker|sticker|thumbnail|avatar|"
     r"desain gambar|image)\b.*", lambda m: ("generate_image", {"description": m[0], "count": _num(m[1]) or 1})),
    (r"(?:buat|buatkan|bikin|bikinkan|generate|susun|susunkan|tulis|tuliskan)(?: saya| aku| kan)?(?: sebuah| satu)?"
     r" (?:file|dokumen|document|word|excel|spreadsheet|tabel excel|presentasi|powerpoint|ppt|slide|pdf|laporan|surat|makalah|"
     r"proposal|cv|resume|daftar riwayat hidup|invoice|faktur|template|rpp|esai|essay|artikel|modul|kuesioner)\b.*",
     lambda m: ("create_file", {"description": m[0]})),
    (r"(?:buka )?(?:hasil generate|gambar (?:tadi|terakhir|yang tadi)|file (?:yang )?(?:tadi|terakhir) (?:dibuat|di-?generate))",
     lambda m: ("open_generated", {"which": "terakhir"})),
    (r"buka folder (?:hasil )?(?:generate|gambar|hasil)", lambda m: ("open_generated", {"which": "folder"})),
    (r"(?:status|progres) (?:pembuatan )?(?:gambar|file|generate)", lambda m: ("generate_status", {})),
    (r"(?:batalkan|batal|hentikan) (?:pembuatan )?(?:gambar|file)(?: nya)?", lambda m: ("cancel_generate", {})),
    # ── projects: where, versions ──
    (r"(?:di ?mana|dimana) (?:lokasi |letak |folder )?(?:project|proyek)(?: saya| ku| yang (?:sudah )?dibuat)?"
     r"(?: (?:disimpan|berada|ada|tersimpan))?|(?:lokasi|letak) (?:folder )?(?:project|proyek)(?: saya| ku)?",
     lambda m: ("project_location", {})),
    (r"(?:kembalikan|pulihkan|rollback|undo) (?:project|proyek) (.+?)(?: ke versi (?:sebelumnya|lama|(\w+)))?",
     lambda m: ("restore_project_version", {"project": m[1], "version": _num(m[2])})),
    (r"(?:kembalikan|pulihkan|rollback) (?:project|proyek)?(?: ?nya)? ?ke versi (?:sebelumnya|lama|(\w+))",
     lambda m: ("restore_project_version", {"project": "", "version": _num(m[1])})),
    (r"(?:batalkan|undo) perubahan (?:terakhir|tadi)(?: (?:di|pada|ke) (?:project|proyek) (.+))?",
     lambda m: ("restore_project_version", {"project": m[1] or "", "version": 0})),
    (r"(?:riwayat|history|daftar) versi(?: (?:project|proyek))?(?: (.+))?|riwayat (?:project|proyek)(?: (.+))?",
     lambda m: ("project_history", {"project": m[1] or m[2] or ""})),
    (r"apa (?:saja )?yang (?:di)?ubah (?:di|pada|dari) (?:project|proyek)(?: (.+))?|"
     r"(?:perubahan|update) (?:terakhir )?(?:di |pada )?(?:project|proyek)(?: (.+))?",
     lambda m: ("project_changes", {"project": m[1] or m[2] or ""})),
    # ── security follow-ups ──
    (r"(?:bersihkan|hapus) (?:entri |program )?startup(?: (?:yang )?yatim)?|bersihkan entri yatim",
     lambda m: ("clean_orphan_startup", {})),
    (r"(?:tutup|matikan|hentikan) (?:semua )?(?:aplikasi |app )?(?:remote(?: access)?|teamviewer|anydesk|rustdesk|ultraviewer)",
     lambda m: ("close_remote_access_apps", {})),
    (r"(?:jalankan|lakukan|mulai) (?:quick scan|scan cepat)(?: defender)?|scan virus(?: sekarang)?|quick scan(?: defender)?",
     lambda m: ("defender_scan", {"kind": "quick"})),
    (r"(?:jalankan|lakukan|mulai) (?:full scan|scan penuh|scan menyeluruh)(?: defender)?|full scan(?: defender)?",
     lambda m: ("defender_scan", {"kind": "full"})),
    (r"(?:scan|pindai|cek|periksa) file (?:itu|tadi|terakhir|yang tadi|yang baru)", lambda m: ("scan_file", {})),
    (r"hapus file (?:itu|tadi|yang tadi)", lambda m: _flagged_delete()),
    # ── sleep timer ──
    (r"(?:matikan|hentikan|stop|pause) (?:musik|lagu)(?:nya)? (?:dalam|setelah|sesudah) (\d+) (menit|jam)|sleep timer (\d+) (menit|jam)",
     lambda m: ("sleep_timer", {"minutes": int(m[1] or m[3]) * (60 if (m[2] or m[4]) == "jam" else 1), "target": "music"})),
    (r"(?:matikan|shutdown) (?:laptop|komputer|pc)(?:nya)? (?:dalam|setelah|sesudah) (\d+) (menit|jam)",
     lambda m: ("sleep_timer", {"minutes": int(m[1]) * (60 if m[2] == "jam" else 1), "target": "laptop"})),
    # ── browser / window navigation ──
    (r"(?:scroll|gulir|geser)(?: ke)? (?:bawah|turun)(?: (\d+) kali)?|(?:scroll|gulir)(?: aja)?(?: (?:instagram|halaman(?:nya)?|layar|terus))?",
     lambda m: ("navigate", {"action": "scroll_down", "times": int(m[1] or 1)})),
    (r"(?:scroll|gulir|geser)(?: ke)? (?:atas|naik)(?: (\d+) kali)?", lambda m: ("navigate", {"action": "scroll_up", "times": int(m[1] or 1)})),
    (r"(?:kembali|balik|back)(?: lagi)?(?: ke halaman sebelumnya)?|halaman sebelumnya", lambda m: ("navigate", {"action": "back"})),
    (r"(?:maju|forward)(?: ke halaman berikutnya)?|halaman berikutnya", lambda m: ("navigate", {"action": "forward"})),
    (r"(?:refresh|reload|muat ulang)(?: (?:halaman|page|tab))?(?: ini)?", lambda m: ("navigate", {"action": "refresh"})),
    (r"(?:buka )?tab baru", lambda m: ("navigate", {"action": "new_tab"})),
    (r"(?:pindah ke )?tab (?:berikutnya|selanjutnya|sebelah)", lambda m: ("navigate", {"action": "next_tab"})),
    (r"(?:pindah ke )?tab sebelumnya", lambda m: ("navigate", {"action": "previous_tab"})),
    (r"buka (?:lagi )?tab yang (?:tadi|baru) (?:di)?tutup", lambda m: ("navigate", {"action": "reopen_tab"})),
    (r"(?:ke )?paling (atas|bawah)(?: halaman)?", lambda m: ("navigate", {"action": "top" if m[1] == "atas" else "bottom"})),
    # ── the document that is open ──
    (r"(?:(?:analisis|analisa|cek|lihat) )?(?:(?:ini )?(?:file|dokumen|berkas) apa(?: ini)?|ini (?:file|dokumen|berkas) apa|info file(?: ini)?)",
     lambda m: ("analisis_dokumen", {"pertanyaan": "file apa ini"})),
    (r"(?:rangkum|ringkas|rangkumkan|ringkaskan|buat (?:ringkasan|rangkuman)|simpulkan|jelaskan|terjemahkan|analisis|analisa)"
     r"(?: isi)? (?:dokumen|file|berkas|pdf|word|excel|presentasi|slide|tabel|laporan|makalah|skripsi|jurnal)(?: ini| yang (?:sedang )?(?:saya )?buka)?"
     r"(?: (?:secara )?(?:lengkap|detail|singkat))?", lambda m: ("analisis_dokumen", {"pertanyaan": m[0]})),
    (r"apa (?:isi|inti|kesimpulan|poin penting) (?:dari )?(?:dokumen|file|pdf|laporan|presentasi) ini",
     lambda m: ("analisis_dokumen", {"pertanyaan": m[0]})),
    (r"(?:simpan|catat) (?:hasil )?(?:analisis|rangkuman|ringkasan)(?: dokumen)?(?: tadi| itu| ini)?",
     lambda m: ("simpan_hasil_analisis", {})),
    (r"(?:diagnosa|diagnosis|cek masalah|periksa masalah) (?:sistem|komputer|laptop|pc|windows)(?: (?:minggu ini|seminggu))?",
     lambda m: ("diagnosa_sistem", {"jam_terakhir": 168 if "minggu" in m[0] else 24})),
    # ── what's on screen ──
    (r"tutup tab(?: (?:ini|sekarang|yang ini|yang sedang dibuka))?", lambda m: ("close_current_tab", {})),
    (r"(?:tutup|close) tab (.+?)(?: (?:di|pada) (?:chrome|firefox|browser|brave|edge))?", lambda m: ("close_tab", {"name": m[1]})),
    (r"(?:pindah|beralih|switch|balik|kembali|buka|ke) ke tab (?!baru|berikutnya|selanjutnya|sebelah|sebelumnya)(.+)|"
     r"(?:kembali|balik) ke tab (?!baru|berikutnya|selanjutnya|sebelah|sebelumnya)(.+)",
     lambda m: ("switch_tab", {"name": m[1] or m[2]})),
    (r"(?:ringkas|ringkaskan|rangkum|rangkumkan|jelaskan|simpulkan) (?:isi )?(?:halaman|artikel|berita|web|website|situs|page|tab)"
     r"(?: web)? (?:ini|yang (?:sedang )?(?:saya )?(?:buka|baca))", lambda m: ("summarize_current_page", {})),
    (r"(?:halaman|artikel|berita) ini (?:tentang|membahas) apa", lambda m: ("summarize_current_page", {})),
    (r"(?:simpan|bookmark|tandai) (?:link|halaman|url|alamat|situs)(?: web)? ini", lambda m: ("save_current_link", {})),
    (r"(?:salin|copy|kopi) (?:link|url|alamat)(?: (?:halaman|web))?(?: ini)?", lambda m: ("copy_current_link", {})),
    (r"(?:saya )?(?:sedang )?(?:buka|lihat) (?:halaman |jendela |aplikasi )?apa(?: sekarang)?|ini (?:halaman|jendela|aplikasi) apa",
     lambda m: ("active_window_info", {})),
    (r"(?:sekarang )?(?:jam|pukul) berapa(?: sekarang)?", lambda m: ("get_time", {})),
    (r"(?:hari ini )?(?:tanggal|hari) (?:berapa|apa)(?: (?:hari ini|sekarang))?", lambda m: ("get_date", {})),
    (r"(?:atur |set |ubah |setel |naikkan |turunkan |kecilkan |besarkan |keraskan |pelankan |kurangi |tambah )?"
     r"(?:volume|suara)(?:nya)? (?:ke |jadi |menjadi |sampai |hingga |di |pada )?(\d{1,3})(?: ?(?:persen|%))?",
     lambda m: ("set_volume", {"level": int(m[1])})),
    (r"(?:naikkan|besarkan|keraskan|kencangkan|tambah(?:kan)?) (?:volume|suara)(?:nya)?"
     r"|(?:volume|suara) (?:naik|lebih (?:keras|besar|kencang))", lambda m: ("volume_up", {})),
    (r"(?:turunkan|kecilkan|pelankan|kurangi) (?:volume|suara)(?:nya)?"
     r"|(?:volume|suara) (?:turun|lebih (?:pelan|kecil))", lambda m: ("volume_down", {})),
    (r"mute|unmute|bisukan|matikan suara|nyalakan suara|hidupkan suara", lambda m: ("volume_mute", {})),
    (r"(?:putar|putarkan|lanjut ke|ke) (?:lagu |musik |video )?(?:selanjutnya|berikutnya)|(?:musik|lagu) (?:selanjutnya|berikutnya)",
     lambda m: ("media_control", {"action": "next"})),
    (r"(?:pause|paus|pos|jeda|stop|setop|hentikan|berhenti|matikan)(?: dulu)?(?: (?:musik|musiknya|lagu|lagunya|video|videonya))?"
     r"|(?:pause|paus|jeda|stop|matikan) (?:musik|lagu|video)(?:nya)?(?: dulu)?",
     lambda m: ("media_control", {"action": "pause"})),
    (r"(?:play|resume|lanjut|lanjutkan|putar lagi|putar kembali|putar|putarkan|nyalakan|setel)(?: (?:musik|musiknya|lagu|lagunya|video|videonya))?"
     r"|(?:play|resume|lanjut|lanjutkan|putar lagi|putar kembali)(?: (?:musik|musiknya|lagu|lagunya|video|videonya))?",
     lambda m: ("media_control", {"action": "play"})),
    (r"next|skip|lewati|ganti lagu|(?:lagu|video) (?:selanjutnya|berikutnya)", lambda m: ("media_control", {"action": "next"})),
    (r"previous|(?:lagu|video) sebelumnya", lambda m: ("media_control", {"action": "previous"})),
    (r"(?:ini )?lagu apa(?: ini)?|judul lagu(?: ini|nya)?(?: apa)?|lagu apa yang (?:sedang )?(?:diputar|main)",
     lambda m: ("now_playing", {})),
    (r"(?:putar|putarkan|play|setel|mainkan) (?:playlist|lagu-lagu|mix) (.+)", lambda m: _play(m[1], mix=True)),
    # "putar youtube / putar musik / lanjutkan musik": resume (or pick up the last music), not a search for "youtube"
    (r"(?:putar|putarkan|play|setel|mainkan|lanjutkan|lanjut)(?: lagi)? (?:pemutaran )?(?:youtube|musik(?:nya)?|lagu(?:nya)?|video(?:nya)?)"
     r"(?: (?:di )?youtube)?(?: lagi)?", lambda m: ("media_control", {"action": "play"})),
    # "putar musik tradisional": "musik" stays in the search (a genre); "putar lagu X": X is the title
    (r"(?:putar|putarkan|play|setel|mainkan) (lagu|musik|video) (.+?)(?: di youtube)?",
     lambda m: _play(re.sub(r"^(?:dari|oleh|by) ", "", m[2]) if re.match(r"(?:dari|oleh|by) ", m[2])
                     else f"musik {m[2]}" if m[1] == "musik" else m[2])),
    (r"(?:putar|putarkan|setel|mainkan) (?!(?:lagu|musik|video|playlist)$)(.{3,}?)(?: di youtube)?", lambda m: _play(m[1])),
    (r"(?:ambil |tangkap )?(?:screenshot|tangkapan layar|tangkap layar)(?: layar)?", lambda m: ("take_screenshot", {})),
    (r"kunci (?:komputer|laptop|pc|layar)", lambda m: ("lock_workstation", {})),
    # ── power: always the real skill (it asks "yakin?"), never an AI that might only SAY it is restarting ──
    (r"(?:restart|reboot|mulai ulang|nyalakan ulang|hidupkan ulang) (?:komputer|laptop|pc|sistem)(?: (?:saya|ku|sekarang))?|restart",
     lambda m: ("restart_pc", {})),
    (r"(?:matikan|shutdown|shut down|matiin) (?:komputer|laptop|pc|sistem)(?: (?:saya|ku|sekarang))?|shutdown|shut down",
     lambda m: ("shutdown_pc", {})),
    (r"(?:sleep|tidurkan|sleepkan) (?:komputer|laptop|pc)(?: (?:saya|ku))?", lambda m: ("sleep_pc", {})),
    (r"(?:batalkan|batal|cancel) (?:shutdown|restart|matikan komputer|mematikan komputer)", lambda m: ("cancel_shutdown", {})),
    # ── keyboard ──
    (r"(?:ketik|ketikkan|tuliskan di sini|type) (.+?)(?: (?:dan|lalu|terus|kemudian) (?:tekan |klik |pencet )?enter)?",
     lambda m: ("type_text", {"text": m[1], "press_enter": bool(re.search(r" (?:dan|lalu|terus|kemudian) (?:tekan |klik |pencet )?enter$", m[0]))})),
    (r"(?:klik|tekan|pencet|press) (?:tombol )?(enter|tab|escape|esc|spasi|space|backspace|delete|home|end|f\d{1,2}|"
     r"page ?up|page ?down|atas|bawah|kiri|kanan)(?: (\d) ?kali)?",
     lambda m: ("press_keys", {"keys": {"spasi": "space", "atas": "up", "bawah": "down", "kiri": "left", "kanan": "right",
                                       "page up": "pageup", "page down": "pagedown"}.get(m[1], m[1])})),
    # ── close everything (asks first) ──
    (r"tutup semua(?: (?:aplikasi|jendela|program|app|window))?(?: yang (?:sedang )?(?:terbuka|ada|berjalan|jalan))?(?: sekarang)?",
     lambda m: ("close_all_apps", {})),
    # ── "katakan X": just say it ──
    (r"(?:katakan|ucapkan|coba katakan|coba ucapkan) (?!apa\b|kenapa\b|ke\b|bahwa\b)(.{1,80})",
     lambda m: ("say", {"text": m[1]})),
    (r"(?:atur )?(?:kecerahan|brightness)(?: layar)? (?:ke |jadi )?(\d{1,3})(?: ?persen)?",
     lambda m: ("set_brightness", {"level": int(m[1])})),
    # ── Antigravity system analysis ──
    (r"(?:lakukan |jalankan |tolong |minta |suruh )?(?:sambungkan ke )?(?:antigravity |antigrafity )?(?:untuk )?"
     r"(?:analisis|analisa|analisi|periksa|cek) (?:seluruh |kondisi |kesehatan )?(?:sistem|laptop|komputer|pc)"
     r"(?: (?:saya|ku))?(?: (?:secara )?(?:menyeluruh|lengkap|total))?(?: (?:dengan|menggunakan|lewat|pake|pakai) (?:antigravity|antigrafity))?|"
     r"(?:minta |suruh )?(?:antigravity|antigrafity) (?:untuk )?(?:analisis|analisa|periksa|cek)(?: (?:seluruh |kondisi |kesehatan )?(?:sistem|laptop|komputer|pc))?",
     lambda m: ("analisis_sistem", {})),
    (r"(?:buka )?laporan (?:analisis sistem|antigravity)(?: terakhir)?",
     lambda m: ("last_system_analysis", {})),
    # ── Security scan ──
    (r"(?:lakukan |jalankan |tolong )?(?:analisis|analisa|analisi|scan|pindai|periksa|cek) (?:keamanan|security|virus|malware|ancaman)"
     r"(?: (?:sistem|laptop|komputer|pc))?(?: saya| ku)?(?: (?:secara )?(?:menyeluruh|lengkap|total))?|cek virus|apakah (?:laptop|komputer|pc|sistem) (?:saya |ku )?aman",
     lambda m: ("security_scan", {})),
    (r"(?:buka )?laporan (?:keamanan|analisis keamanan)(?: terakhir)?", lambda m: ("last_security_report", {})),
    (r"(?:status sistem|diagnostik(?: sistem)?|cek baterai)", lambda m: ("get_system_status", {})),
    (r"(?:cek |berapa )?(?:penggunaan|pemakaian|pakai|sisa|kapasitas)? ?(?:ram|memori|memory|cpu|prosesor|processor)"
     r"(?: (?:saya|laptop|komputer|ku))?(?: (?:berapa|sekarang|sudah berapa|terpakai))*(?: persen)?",
     lambda m: ("get_system_status", {})),
    (r"(?:baterai|batre|batrai)(?: (?:saya|laptop|ku))?(?: (?:berapa|tinggal berapa|sisa berapa))?(?: persen)?(?: sekarang)?",
     lambda m: ("get_system_status", {})),
    (r"(?:cek |lihat |tampilkan |sebutkan |coba cek )?(?:ada )?(?:jendela|aplikasi|window) apa saja(?: yang)? (?:terbuka|sedang terbuka|dibuka|berjalan|jalan)(?: sekarang)?|"
     r"(?:daftar|list) (?:jendela|window)(?: yang terbuka)?", lambda m: ("list_windows", {})),
    (r"(?:buka|open) (?:folder )?(?:project|proyek)(?: saya| ku)?", lambda m: ("open_project", {})),
    (r"(?:buat|bikin|buatkan|bikinkan)(?: saya| aku)?(?: sebuah| satu)? (?:project|proyek|aplikasi|app|website|web|situs|game|program)\b.*",
     lambda m: ("create_project", {"description": m[0]})),
    (r"(?:status|progres|progress) (?:project|proyek|pembuatan project)(?: (.+))?",
     lambda m: ("project_build_status", {"project": m[1] or ""})),
    (r"(?:batalkan|batal|hentikan|stop) (?:pembuatan|pengerjaan|antrean) (?:project|proyek)(?: (.+))?",
     lambda m: ("cancel_project_build", {"project": m[1] or ""})),
    (r"(?:hentikan|matikan|stop|tutup) (?:project|proyek)(?: (.+))?", lambda m: ("stop_project", {"project": m[1] or ""})),
    (r"(?:daftar|list) (?:project|proyek)(?: saya)?|project apa saja(?: yang (?:sudah )?(?:dibuat|ada))?",
     lambda m: ("list_projects", {})),
    # ── workspaces (Hyprland) ──
    (rf"(?:(?:pindah|beralih|switch|balik|kembali|pergi|buka|lompat)(?: ke)? |ke |\w{{3,8}}(?: ke)? )?{WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)",
     lambda m: _ws_route("switch_workspace", {"workspace": _ws(m[1])})),
    (rf"(?:pindahkan|pindahin|kirim|kirimkan|lempar|taruh|move) (.+?) ke {WORKSPACE} ({WS_NUM}|berikutnya|selanjutnya|sebelumnya)"
     r"( (?:dan|lalu|terus) (?:ikut(?: pindah)?|ikuti|ke sana|pindah ke sana))?",
     lambda m: _ws_route("move_to_workspace", {"workspace": _ws(m[2]), "follow": bool(m[3]),
                                               "name": "" if re.fullmatch(r"(?:jendela|aplikasi)? ?(?:ini|aktif|yang aktif|sekarang)|jendela|aplikasi", m[1]) else m[1]})),
    (r"(?:pindah|beralih|switch|fokus|balik) ke (?:jendela |aplikasi )?(?!halaman|tab|versi|mode)(.+)",
     lambda m: ("focus_window", {"name": m[1]})),
    (r"(?:buka|open|jalankan) (.+)", lambda m: _open_known(m[1])),
    (r"(?:tutup|close) (.+)", lambda m: _close_known(m[1])),
]]


def _bare_name(cmd: str):
    """Just the name of a site or app the user often opens ("instagram", "youtube"): open it."""
    if cmd in sc.WEBSITES:
        return "open_website", {"site": cmd}
    if cmd in sc.APPS:
        return "open_application", {"name": cmd}
    return None


def _route(text: str):
    """(tool name, kwargs) for a simple command, or None if Gemini should handle it."""
    cmd = WS_GLUED.sub(r"\1 ps \2", _normalize(text))
    bare = _bare_name(cmd)
    if bare:
        return bare
    for pattern, handler in FAST_ROUTES:
        match = pattern.fullmatch(cmd)
        if match:
            action = handler(match)
            if action and action[0] in VERBATIM_ARGS:
                action = _with_original_text(text, *action)
            return action
    return None


# what the user dictates is typed / said exactly as spoken (capitals, punctuation), not in the lowercased command form
VERBATIM_ARGS = {"type_text": "text", "say": "text"}
_VERBATIM_RE = re.compile(r"^(?:\W*(?:tolong|coba|mohon)\s+)?(?:ketikkan|ketik|tuliskan di sini|type|katakan|ucapkan|"
                          r"coba katakan|coba ucapkan|bilang|sebutkan|sebut)\s+(.+?)(?:\s+(?:dan|lalu|terus|kemudian)\s+"
                          r"(?:tekan\s+|klik\s+|pencet\s+)?enter)?[\s.!]*$", re.IGNORECASE | re.DOTALL)


def _with_original_text(text: str, name: str, kwargs: dict):
    m = _VERBATIM_RE.match(text.strip())
    if m:
        kwargs = dict(kwargs, **{VERBATIM_ARGS[name]: m[1].strip()})
    return name, kwargs


def _compound(text: str) -> str | None:
    """Several simple commands in one sentence, run in order. None -> let Gemini handle the sentence."""
    parts = [p.strip() for p in COMPOUND_SPLIT.split(_normalize(text)) if p.strip()]
    if len(parts) < 2:
        return None
    steps = []
    browser = BROWSER_OPEN.fullmatch(parts[0])
    if browser:
        # "buka firefox dan cari instagram": do the web steps in THAT browser (it opens by itself)
        name = browser.group(1).split()[-1]
        for part in parts[1:]:
            web = WEB_STEP.fullmatch(part)
            steps.append(("open_website", {"site": web.group(1), "browser": name}) if web else _route(part))
    else:
        steps = [_route(p) for p in parts]
    if not all(steps):
        return None
    results = []
    for i, (name, kwargs) in enumerate(steps):
        results.append(str(TOOL_MAP[name](**kwargs)))
        if i < len(steps) - 1 and name in ("open_application", "open_website", "focus_window", "open_many"):
            import time
            time.sleep(1.0)
    _system_notes.append(f"perintah '{text}' dijalankan bertahap: {' | '.join(results)}")
    del _system_notes[:-6]
    return _combine(results)


ALARM_WORDS = re.compile(r"\b(?:timer|alarm|ingatkan|ingetin|pengingat|reminder|bangunkan|bangunin|weker|hitung mundur)\b")
ALARM_STOP = re.compile(r"(?:oke |ok |iya |ya )?(?:stop|setop|matikan|hentikan|cukup|sudah|udah|diam|berhenti|oke|ok|sip)"
                        r"(?: (?:alarm|alarmnya|timer|timernya|bunyinya|dulu|ya|saja|aja|deh|sudah bangun))?(?: sudah bangun)?")
ALARM_SNOOZE = re.compile(r"(?:tunda|snooze|nanti dulu|nanti lagi|sebentar lagi|lima menit lagi|5 menit lagi)"
                          r"(?: alarm(?:nya)?)?(?: (?:selama |untuk )?(\d+|lima|sepuluh|lima belas) menit)?(?: lagi)?")


def _alarm_route(cmd: str):
    if alarms.is_ringing():
        if ALARM_SNOOZE.fullmatch(cmd):
            m = ALARM_SNOOZE.fullmatch(cmd)
            return "snooze_alarm", {"minutes": _num(m[1]) or config.ALARM_SNOOZE_MINUTES} if m[1] else {}
        if ALARM_STOP.fullmatch(cmd):
            return "stop_alarm", {}
    if not ALARM_WORDS.search(cmd):
        return None
    m = re.fullmatch(r"(?:batalkan|hapus|matikan|hentikan|cancel|stop) (?:semua )?(timer|alarm|pengingat)(?:nya)?(?: (.+))?", cmd)
    if m:
        return "cancel_timers", {"keyword": m[2] or ("semua" if "semua" in cmd else m[1])}
    if re.search(r"\b(?:apa saja|daftar|sisa waktu|berapa lama lagi|ada (?:timer|alarm)|kapan)\b", cmd) or \
            re.fullmatch(r"(?:timer|alarm|pengingat)(?: saya| ku)?", cmd):
        return "list_timers", {}
    return None


_pending_alarm: dict = {}


def fast_route(text: str) -> str | None:
    """Run a simple command locally. Returns the reply, or None if Gemini should handle it."""
    cmd_alarm = _normalize(text)
    action = _alarm_route(cmd_alarm)
    if action:
        name, kwargs = action
        return _combine([str(TOOL_MAP[name](**kwargs))])
    pending = _pending_alarm.get("text")
    if pending and time.time() - _pending_alarm["at"] < 25 and not ALARM_WORDS.search(cmd_alarm):
        _pending_alarm.clear()
        joined = f"{pending} {'jam ' if re.match(r'^[0-9a-z]+(?:[.:][0-9]{2})?(?: (?:pagi|siang|sore|malam))?$', cmd_alarm) and not re.search(r'menit|detik', cmd_alarm) else ''}{text}"
        reply = alarms.handle(re.sub(r"\bjam\s+jam\b", "jam", joined))
        if reply:
            return _combine([reply])
    if ALARM_WORDS.search(cmd_alarm):
        reply = alarms.handle(text)
        if reply:
            return _combine([reply])
        if re.fullmatch(r"(?:set|setel|pasang|atur|buat|bikin)?\s*(?:alarm|timer|pengingat)(?: (?:jam|pukul|untuk))?", cmd_alarm):
            _pending_alarm.update(text=re.sub(r"\s+(?:jam|pukul|untuk)$", "", cmd_alarm), at=time.time())
            kind = "Timer berapa lama" if "timer" in cmd_alarm else "Jam berapa"
            return f"{kind}, {T}?"
    compound = _compound(text)
    if compound:
        return compound
    action = _route(text)
    if action:
        name, kwargs = action
        result = str(TOOL_MAP[name](**kwargs))
        _system_notes.append(f"perintah '{text}' langsung dijalankan: {result}")
        del _system_notes[:-6]
        return _combine([result])
    return None


def ask_gemini(prompt: str) -> str:
    """Backwards-compatible plain question helper."""
    return ask_agent(prompt) or (
        "I heard your request, Sir, but my neural core is offline. Local system commands remain operational."
    )


# ───────────────────────── local fallback router ─────────────────────────

def local_route(text: str) -> str:
    """Keyword-based command handling used when Gemini is unavailable (no key / offline)."""
    cmd = text.lower().strip()

    def has(*keys):
        return any(k in cmd for k in keys)

    # Notes: "buka notepad dan catat ...", "catat berikut: ..."
    m = re.search(r"(?:catat(?:kan)?|tulis(?:kan)?(?: catatan)?)\s*(?:berikut|ini|bahwa)?\s*:?\s*(.+)", text, re.I)
    if m and m.group(1).strip():
        return sc.write_note(m.group(1).strip())

    # Protocols
    m = re.search(r"(?:protokol|protocol|mode)\s+(.+)|(.+?)\s+protocol$", cmd)
    if m and protocols._resolve(m.group(1) or m.group(2), protocols.load()):
        return protocols.run_protocol(m.group(1) or m.group(2))

    # Recycle bin / cleanup
    if has("recycle bin", "tempat sampah", "sampah") and has("kosong", "hapus", "bersih", "empty"):
        return sc.empty_recycle_bin()
    if has("file temp", "file sementara", "bersihkan temp"):
        return sc.clean_temp_files()

    # Power
    if has("batalkan shutdown", "batal shutdown", "cancel shutdown", "batalkan restart"):
        return sc.cancel_shutdown()
    if has("matikan komputer", "matikan laptop", "shutdown komputer", "shutdown pc", "shut down"):
        return sc.shutdown_pc()
    if has("restart", "mulai ulang"):
        return sc.restart_pc()
    if has("sleep", "tidurkan"):
        return sc.sleep_pc()
    if has("kunci komputer", "kunci pc", "lock pc", "lock workstation", "kunci layar", "kunci laptop"):
        return sc.lock_workstation()

    # Diagnostics / info
    if has("analisis sistem", "analisa sistem", "analisis seluruh sistem"):
        return antigravity_analysis.analisis_sistem()
    if has("status", "diagnostik", "diagnostic", "cek sistem", "baterai", "battery", "cpu", "ram"):
        return sc.get_system_status()
    if has("jam berapa", "pukul berapa", "what time"):
        return sc.get_time()
    if has("tanggal berapa", "hari apa", "what date"):
        return sc.get_date()
    if has("cuaca", "weather"):
        m = re.search(r"(?:cuaca|weather)\s+(?:di\s+|in\s+)?(.+)", cmd)
        return sc.get_weather(m.group(1) if m else "")
    if has("screenshot", "tangkap layar"):
        return sc.take_screenshot()
    if has("berita", "news"):
        return ai_skills.get_news()

    # Timer / alarm / reminder: understood locally, also offline
    reply = alarms.handle(text)
    if reply:
        return reply

    # Volume
    m = re.search(r"volume\s+(?:ke\s+)?(\d{1,3})", cmd)
    if m:
        return sc.set_volume(int(m.group(1)))
    if has("volume naik", "volume up", "besarkan suara", "keraskan", "tambah volume"):
        return sc.volume_up()
    if has("volume turun", "volume down", "kecilkan suara", "pelankan", "kurangi volume"):
        return sc.volume_down()
    if has("mute", "bisu", "matikan suara", "unmute"):
        return sc.volume_mute()
    m = re.search(r"(?:kecerahan|brightness)\s+(?:ke\s+)?(\d{1,3})", cmd)
    if m:
        return sc.set_brightness(int(m.group(1)))

    # Media: explicit pause / resume / skip before "putar <judul>"
    if re.fullmatch(r"(?:tolong\s+)?(?:pause|paus|jeda|jeda dulu|stop|stop musik|stop lagu|hentikan(?: musik| lagu| video)?|berhenti)", cmd):
        return media.media_control("pause")
    if re.fullmatch(r"(?:tolong\s+)?(?:play|putar|putar lagi|lanjut|lanjutkan|resume|lanjutkan (?:musik|lagu|video))", cmd):
        return media.media_control("play")
    if has("lagu selanjutnya", "next track", "skip lagu", "lagu berikutnya", "video berikutnya"):
        return media.media_control("next")
    if has("lagu sebelumnya", "previous track", "video sebelumnya"):
        return media.media_control("previous")
    if has("lagu apa ini", "judul lagu", "sedang diputar", "now playing"):
        return media.now_playing()

    # YouTube / music: "putar lagu X", "play X di youtube", "putar playlist X"
    m = re.search(r"\b(?:putar(?:kan)?|play|setel|mainkan)\s+(playlist\s+|lagu-lagu\s+)?(?:lagu\s+|video\s+|musik\s+)?(.+?)(?:\s+(?:di|on)\s+youtube)?$", cmd)
    if m and not has("spotify") and m.group(2) not in ("musik", "lagu", "lagi"):
        return media.play_youtube(m.group(2), mix=bool(m.group(1)))
    m = re.search(r"(?:cari di youtube|search youtube)\s+(.+)", cmd)
    if m:
        return sc.search_youtube(m.group(1))

    # Windows
    m = re.search(r"(?:pindah ke|beralih ke|switch to|fokus ke)\s+(.+)", cmd)
    if m:
        return window_manager.focus_window(m.group(1))

    # Close app
    m = re.search(r"(?:paksa tutup|force close)\s+(?:aplikasi\s+)?(.+)", cmd)
    if m:
        return sc.close_application(m.group(1), force=True)
    m = re.search(r"(?:tutup|close|matikan)\s+(?:aplikasi\s+)?(.+)", cmd)
    if m:
        return sc.close_application(m.group(1))

    # Folders
    m = re.search(r"(?:buka|open)\s+folder\s+(.+)", cmd)
    if m:
        return sc.open_folder(m.group(1))

    # Launch app / website
    m = re.search(r"(?:buka|open|jalankan|launch)\s+(.+)", cmd)
    if m:
        return sc.open_app_or_web(m.group(1))

    # Web search
    m = re.search(r"(?:cari|search|googling)\s+(.+)", cmd)
    if m:
        return sc.google_search(m.group(1))

    if memory.all_facts() and has("ingat apa", "apa yang kamu ingat"):
        return memory.as_text()
    m = re.search(r"(?:ingat(?:lah)? bahwa|remember that)\s+(.+)", cmd)
    if m:
        return memory.add(m.group(1))

    return (f"Maaf {T}, otak AI saya sedang offline sehingga saya belum memahami perintah itu. "
            f"Perintah sistem dasar tetap bisa digunakan.")


# ───────────────────────────── entry point ─────────────────────────────

# restarting Digidaw itself (a bare "restart" stays "restart the laptop?", which asks for confirmation)
RESTART_SELF_RE = re.compile(r"(?:tolong |coba )?(?:restart|mulai ulang|muat ulang|reboot|reload)"
                             r" (?:digidaw|dirimu|diri ?mu|diri sendiri|dirimu sendiri|kamu|sistem digidaw|aplikasi digidaw|"
                             r"asisten|program digidaw)(?: sekarang| dulu| dong| ya)?")
_restart_requested = False


def consume_restart() -> bool:
    """True once after "restart Digidaw" was said (core then restarts instead of shutting down)."""
    global _restart_requested
    flag, _restart_requested = _restart_requested, False
    return flag


def _is_exit(cmd: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", "", cmd).replace("digidaw", "").strip()
    return cleaned in EXIT_PHRASES


def process_command(text: str) -> tuple[str, bool]:
    """
    Process incoming user text / speech.
    Returns: (response_text, should_exit)
    """
    global _brain_used
    _tools_used.clear()
    _calls_this_turn.clear()
    _brain_used = ""
    fixed = text if DICTATION_RE.match(text) else habits.apply_aliases(text)
    if fixed != text:
        console.print(f"[dim]  ↪ dikoreksi: {escape(fixed)}[/dim]")
    reply = _correction(fixed)
    should_exit = False
    if reply is None:
        reply, should_exit = _process_command(fixed)
    if _calls_this_turn:
        _last_action.update(text=fixed, calls=list(_calls_this_turn), at=time.time())
    if reply and not should_exit:
        _remember_turn(fixed, reply)
        if _brain_used:
            console.print(f"[dim]   🧠 {'lokal · ' + config.LOCAL_MODEL if _brain_used == 'local' else _brain_used}[/dim]")
    return persona.speak(fix_title(reply)) if reply else reply, should_exit


def _correction(text: str) -> str | None:
    """'bukan itu, maksud saya hotspot' after 'buka ho': run the previous command again with the right word
    and remember the mishearing (ho -> hotspot) so it is understood correctly next time."""
    if not _last_action or time.time() - _last_action["at"] > CORRECTION_WINDOW:
        return None
    m = CORRECTION_RE.fullmatch(_normalize(text))
    if not m:
        return None
    right = (m["right"] or m["right2"] or "").strip()
    if not right or len(right.split()) > 6:
        return None
    before = _normalize(_last_action["text"])
    wrong = (m["wrong"] or "").strip()
    if not wrong:                                # the target the previous command acted on
        for _, kwargs, _ in _last_action["calls"]:
            for arg in TARGET_ARGS:
                value = kwargs.get(arg)
                value = " ".join(value) if isinstance(value, list) else str(value or "")
                if value and value.lower() in before:
                    wrong = value.lower()
                    break
            if wrong:
                break
    if not wrong or not re.search(rf"(?<!\w){re.escape(wrong)}(?!\w)", before):
        return None                              # can't tell what to replace: let Gemini handle it
    retry = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, before, count=1)
    tools = {name for name, _, _ in _last_action["calls"]}
    _last_action.clear()
    reply, _ = _process_command(retry)
    worked = bool(reply) and not habits.FAILED_RESULT.search(reply)     # only a correction that worked is learned
    learned = worked and bool(tools & LEARN_FROM_TOOLS) and len(wrong.split()) <= 2 and habits.learn_alias(wrong, right)
    console.print(f"[dim]  ↪ koreksi: '{escape(wrong)}' → '{escape(right)}' ({'diingat' if learned else 'sekali ini'})[/dim]")
    note = f" Lain kali '{wrong}' saya pahami sebagai {right}." if learned else ""
    return (reply or f"Baik, {T}.") + note


def _process_command(text: str) -> tuple[str, bool]:
    cmd = text.lower().strip()
    if not cmd:
        return "", False

    if _is_exit(cmd):
        return f"Shutting down systems. Have a wonderful day, {T}.", True
    if RESTART_SELF_RE.fullmatch(re.sub(r"[^\w\s]", "", cmd).strip()):
        global _restart_requested
        _restart_requested = True
        return f"Baik, {T}. Saya mulai ulang sekarang, sekitar sepuluh detik.", True

    # Answer to a pending "are you sure?" question
    if safety.has_pending():
        answer = safety.classify_answer(cmd)
        if answer == "yes":
            desc = safety.pending_description()
            result = safety.confirm()
            _system_notes.append(f"user mengonfirmasi '{desc}', hasil: {result}")
            return result, False
        if answer == "no":
            desc = safety.pending_description()
            result = safety.cancel()
            _system_notes.append(f"user membatalkan '{desc}'")
            return result, False
        safety.cancel()  # user moved on to something else

    if cmd in ("reset", "lupakan percakapan", "mulai percakapan baru"):
        reset_conversation()
        return f"Percakapan sudah saya reset, {T}.", False

    reply = fast_route(text)
    if reply:
        return reply, False

    reply = _think(text)
    if reply:
        return reply, False
    offline = local_route(text)
    if offline.startswith("Maaf") and "offline" in offline:
        return llm.explain(T), False
    return offline, False
