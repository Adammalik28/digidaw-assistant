import re
import time
import datetime
import functools
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from rich.console import Console
from rich.markup import escape

import config
import diag
import metrics
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
import agenda
import mobile_server
import notifications
import gcal_sync
import weather
import smarthome
import home_auto
import watchers
import daily
import whatsapp
import antigravity_analysis
import habits
import persona
import local_llm
import local_router
import experience
import file_tools
import pc_control
import screen_agent

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
        _check_cancel()                  # the user was already told this command took too long: do nothing more
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
            daily.note_tool(fn.__name__)
        except Exception:
            pass
        return result
    return wrapper


_tools_used: list[str] = []
_calls_this_turn: list[tuple[str, dict, str]] = []     # (tool, kwargs, result) of the current command
# tools that start work in the background: no open "follow-up" window afterwards, so a stray word
# ("Star") isn't taken as a second image / file / scan
BACKGROUND_TOOLS = {"do_on_screen", "click_on_screen", "generate_image", "create_file", "security_scan", "defender_scan", "improve_project",
                    "restore_project_version", "scan_file", "analisis_sistem"}


def started_background_job() -> bool:
    return any(t in BACKGROUND_TOOLS for t in _tools_used)


DROPPED_TOOLS: list[str] = []     # tools Gemini couldn't read (left out instead of breaking every request)


def self_check() -> dict:
    """One tiny request with exactly the setup every command uses (system prompt + all tools), so a broken
    tool, a rejected key or a missing connection shows up right away instead of at the first real command."""
    from google.genai import types
    result = {"ok": False, "model": "", "ms": 0, "dropped": list(DROPPED_TOOLS), "reason": "", "kind": ""}
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
    # every model, not only the first two: on a busy evening the two fastest both answered "high demand" while the
    # others worked, and Digidaw announced the AI as unusable at every start
    for model in llm.candidates():
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
    result["kind"] = llm._last_error.get("kind", "") if tried else ""
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
              + protocols.AI_TOOLS + hud_server.AI_TOOLS + project_builder.AI_TOOLS + security_scan.AI_TOOLS + antigravity_analysis.AI_TOOLS + screen_context.AI_TOOLS + generator.AI_TOOLS + alarms.AI_TOOLS + agenda.AI_TOOLS + mobile_server.AI_TOOLS + gcal_sync.AI_TOOLS + notifications.AI_TOOLS + weather.AI_TOOLS + smarthome.AI_TOOLS + home_auto.AI_TOOLS + watchers.AI_TOOLS + daily.AI_TOOLS + whatsapp.AI_TOOLS + metrics.AI_TOOLS + pc_control.AI_TOOLS + file_tools.AI_TOOLS + screen_agent.AI_TOOLS + [ai_status, learned_habits, forget_correction, say])
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
    """Only what rarely changes: Gemini reuses (caches) an unchanged prompt prefix, which is faster and saves quota.
    The clock, the active window and the open windows go into each message instead (_dynamic_context)."""
    prompt = config.SYSTEM_PROMPT
    prompt += (f"\nThe user's title is '{T}' (exactly this spelling and capitalisation; never Sir, Tuan, King or another "
               f"title). Use it sparingly, not in every reply.")
    prompt += ("\nCall yourself 'aku' and the user 'kamu' (close, friendly)." if config.ADDRESS_STYLE == "kamu"
               else "\nCall yourself 'saya' and the user 'Anda' (never 'aku/kamu'), relaxed and warm.")
    prompt += ("\nEach user message starts with a [Konteks: ...] line: the current date/time, the window the user is "
               "looking at and, for window commands, the open windows. When they say 'ini' (this page/tab/text/link) "
               "they mean the active window: use summarize_current_page, close_current_tab, save_current_link, "
               "copy_current_link or get_selected_text. Use the open-window list to pick the right target (a site open "
               "in a browser is closed with close_tab, an app with close_application).")
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


WINDOW_TALK_RE = re.compile(r"\b(?:buka|tutup|close|pindah|beralih|fokus|ke|kembali|balik|jendela|aplikasi|tab|minimize|"
                            r"maximize|kecilkan|besarkan|workspace|layar|ini|itu)\b")


def _windows_hint(limit: int = 8) -> str:
    """'jendela terbuka: Firefox (TikTok - Make Your Day); Discord (Friends - Discord); Kitty (zsh)'."""
    try:
        import window_manager as wm
        wins = wm.closable_windows()[:limit]
    except Exception:
        return ""
    if not wins:
        return "tidak ada jendela aplikasi yang terbuka"
    parts = []
    for w in wins:
        label = wm._app_label(w)
        title = re.sub(r"\s+[—–-]\s+(?:Mozilla Firefox|Google Chrome|Chromium|Brave|Discord)$", "", w["title"]).strip()[:45]
        parts.append(f"{label} ({title})" if title and title.lower() != label.lower() else label)
    return "jendela terbuka: " + "; ".join(parts)


def _dynamic_context(text: str, windows: bool | None = None, active: bool = True) -> str:
    """The changing facts for one message: time, active window, and the open windows for window commands.
    active=False leaves the active window out unless the request points at it ("ini", "halaman ini"): the small
    local model otherwise starts commenting on it ("Aku lihat jendela Waydroid aktif")."""
    now = datetime.datetime.now()
    part = "pagi" if 4 <= now.hour < 11 else "siang" if now.hour < 15 else "sore" if now.hour < 18 else "malam"
    context = [f"sekarang {sc.HARI[now.weekday()]} {part}, {now:%d-%m-%Y %H:%M}"]
    try:
        w = screen_context.current() if active or DEICTIC_RE.search(_normalize(text)) else None
        if w:
            kind = f"tab {screen_context.BROWSERS[w['process'].lower()]}" if screen_context.is_browser(w) \
                else w["process"].removesuffix(".exe")
            context.append(f'jendela aktif: "{w["title"][:90]}" ({kind})')
    except Exception:
        pass
    if windows if windows is not None else WINDOW_TALK_RE.search(_normalize(text)):
        hint = _windows_hint()
        if hint:
            context.append(hint)
    return "[Konteks: " + " | ".join(context) + "]"


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
            r = safety.question()
        parts.append(r)
    # "A, Sir. B, Sir." -> "A. B, Sir."
    parts = [p.removesuffix(f", {T}.").rstrip(".") + "." for p in parts[:-1]] + parts[-1:]
    return " ".join(parts)


# ── a slow Google answer must not freeze Digidaw for 20-40 s ──
HEDGE_AFTER = 4.0          # no answer yet after this many seconds: ask the next model too, take whichever is first
FIRST_CALL_BUDGET = 12.0   # the first decision never waits longer than this (each model alone may take 20 s); for
                           # online questions a search answered by the local model takes over (_local_search_answer)
# A command that ran out of time must not open / close / type anything afterwards. Each command runs on its own
# thread with its own number, so cancelling the late one never touches the next command.
_command_local = threading.local()
_command_ids = iter(range(1, 10**12))
_current_command = 0
_cancelled: set[int] = set()


class Cancelled(Exception):
    pass


def cancel_current():
    """The command took too long and the user was told so: stop it before it does anything else."""
    _cancelled.add(_current_command)


def _check_cancel():
    if getattr(_command_local, "id", None) in _cancelled:
        raise Cancelled("perintah dibatalkan karena terlalu lama")


def _turn_contents(message: str) -> list:
    from google.genai import types
    return list(_history) + [types.Content(role="user", parts=[types.Part(text=message)])]


# ── speaking an answer while it is being written (config.STREAM_REPLIES) ──
# core gives each spoken command a tts.LiveSpeech; a question's answer is then streamed from Gemini and every finished
# sentence is said at once, instead of waiting for the whole answer (a long one takes seconds longer to write).
_live = None
_voiced: set[int] = set()            # id() of responses whose text went to the live voice


def set_live(live):
    global _live
    _live = live
    _voiced.clear()


class Superseded(Exception):
    """Another model already started answering (and speaking): this one's answer isn't needed."""


def _merge_stream(chunks: list):
    """The chunks of a streamed answer as one response, the same as generate_content would give: the text pieces
    joined (with the thought signature Gemini puts on the last one), function calls as they came."""
    from google.genai import types
    parts, finish = [], None
    for chunk in chunks:
        cand = chunk.candidates[0] if chunk.candidates else None
        if cand is None:
            continue
        finish = cand.finish_reason or finish
        for p in (cand.content.parts or []) if cand.content else []:
            if p.function_call:
                parts.append(p)
            elif p.thought:
                continue
            elif p.text is not None or p.thought_signature:
                last = parts[-1] if parts else None
                if last is not None and not last.function_call and last.text is not None:
                    parts[-1] = types.Part(text=(last.text or "") + (p.text or ""),
                                           thought_signature=p.thought_signature or last.thought_signature)
                else:
                    parts.append(types.Part(text=p.text or "", thought_signature=p.thought_signature))
    parts = [p for p in parts if p.function_call or p.text or p.thought_signature]
    content = types.Content(role="model", parts=parts) if parts else None
    return types.GenerateContentResponse(candidates=[types.Candidate(content=content, finish_reason=finish)])


def _stream_generate(model: str, contents: list, gen_config, claim: dict | None = None):
    """generate_content, streamed: the answer's text goes to the live voice while it arrives. With several models
    racing (claim), only the first one to answer speaks; the others stop (Superseded)."""
    live, mine, voiced = _live, claim is None, False
    chunks = []
    stream = llm.get_client().models.generate_content_stream(model=model, contents=contents, config=gen_config)
    for chunk in stream:
        chunks.append(chunk)
        if not mine:
            with claim["lock"]:
                claim["owner"] = claim["owner"] or model
                mine = claim["owner"] == model
            if not mine:
                raise Superseded(model)
        cand = chunk.candidates[0] if chunk.candidates else None
        if live is not None and cand is not None and cand.content:
            delta = "".join(p.text for p in cand.content.parts or [] if p.text and not p.thought)
            if delta:
                live.feed(delta)
                voiced = True
    if voiced:
        live.flush()
    response = _merge_stream(chunks)
    if voiced:
        _voiced.add(id(response))
    return response


def _first_response(models: list[str], contents: list, stream: bool = False):
    """The first model's answer; if Google is slow (nothing after HEDGE_AFTER s) the next model is asked as well and
    the first answer wins. A model that fails at once (quota) is replaced at once. Returns (model, response).
    stream: the answer is spoken while it is written (only the model that answers first speaks; its answer is then
    the one used, however long the rest of it takes)."""
    client = llm.get_client()
    queue, pending = list(models), {}
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="gemini")
    start, hedged, last_error = time.time(), False, None
    key = llm.active_key()
    claim = {"owner": None, "lock": threading.Lock()} if stream else None
    backup = None                        # a finished answer from a model that isn't the one speaking

    def launch() -> bool:
        nonlocal key
        if not queue and llm.key_count() > 1:
            fresh = llm.candidates()         # every model of this key at its limit: another key may have room
            if llm.active_key() != key:
                key = llm.active_key()
                queue.extend(fresh)
        if not queue:
            return False
        model = queue.pop(0)
        llm.record_call(model)
        if stream:
            pending[pool.submit(_stream_generate, model, contents, _generation_config(model), claim)] = model
        else:
            pending[pool.submit(client.models.generate_content, model=model, contents=contents,
                                config=_generation_config(model))] = model
        return True

    def budget() -> float:               # an answer already being spoken may take as long as it needs to finish
        return config.AI_TIMEOUT_SECONDS + 15 if claim and claim["owner"] else FIRST_CALL_BUDGET

    try:
        launch()
        while pending and time.time() - start < budget():
            _check_cancel()
            done, _ = wait(list(pending), timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                if not hedged and queue and time.time() - start >= HEDGE_AFTER:
                    hedged = True
                    console.print(f"[dim]  ↪ {', '.join(pending.values())} lambat, {queue[0]} ikut ditanya[/dim]")
                    launch()
                continue
            for future in done:
                model = pending.pop(future)
                try:
                    response = future.result()
                    if not response.candidates or response.candidates[0].content is None:
                        raise RuntimeError("respons kosong dari Gemini")
                except Superseded:
                    continue                     # the other model answered first and is speaking
                except Exception as e:
                    last_error = e
                    console.print(f"[dim red]  [{model}] {escape(llm.report_error(model, e))}[/dim red]")
                    if llm.classify(e) in llm.GLOBAL_FAILURES:
                        raise
                    if claim and claim["owner"] == model and backup:
                        return backup            # the one speaking broke off: the other's finished answer
                    if not pending:
                        launch()                 # failed at once (quota, retired): the next model right away
                    continue
                if claim and claim["owner"] not in (None, model):
                    backup = backup or (model, response)
                    continue                     # the model that is speaking finishes its own answer
                if hedged:
                    console.print(f"[dim]  ↪ dijawab {model} ({time.time() - start:.1f} dtk)[/dim]")
                return model, response
        if backup:
            return backup
        if last_error and not pending:
            raise last_error
        raise TimeoutError(f"Gemini tidak menjawab dalam {FIRST_CALL_BUDGET:.0f} dtk")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # a late answer from the slower model is simply ignored


def _run_turn(model: str, message: str, direct_ok: bool, first=None, stream: bool = False) -> str:
    """One user turn with manual function calling. The history is only updated on success.
    first: the model's answer to the opening request when _first_response already got it.
    stream: the answer is spoken while it is written (tts.LiveSpeech)."""
    from google.genai import types
    client = llm.get_client()
    gen_config = _generation_config(model)
    contents = _turn_contents(message)
    for step in range(config.MAX_TOOL_CALLS):
        _check_cancel()
        if step == 0 and first is not None:
            response = first
        else:
            llm.record_call(model)
            if stream and _live is not None:
                response = _stream_generate(model, contents, gen_config)
            else:
                response = client.models.generate_content(model=model, contents=contents, config=gen_config)
        if not response.candidates or response.candidates[0].content is None:
            raise RuntimeError("respons kosong dari Gemini")
        contents.append(response.candidates[0].content)
        calls = response.function_calls or []
        if not calls:
            _commit(contents)
            reply = llm.clean(response.text)
            if _live is not None and id(response) in _voiced:
                _live.covered_text = reply       # said already, while it was written
            return reply

        results = []
        for call in calls:
            _check_cancel()                      # out of time: no action may happen after the user was told so
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


def _answer_from_results(question: str) -> str:
    """The model ran out of quota after the search: one short, tool-free request to the next model that is free
    turns the results into an answer (a search takes 2-3 requests, and each model only has ~14 a minute)."""
    from google.genai import types
    results = "\n\n".join(r for _, _, r in _calls_this_turn[-3:])[:6000]
    prompt = (f"Pertanyaan user: {question}\n\nHasil pencarian/tool:\n{results}\n\n"
              f"Jawab pertanyaan itu dalam 1-3 kalimat singkat berbahasa Indonesia, hanya dari hasil di atas "
              f"(sebut angka/fakta terbaru dan sumbernya bila ada). Jangan memakai markdown. Sapa user dengan \"{T}\" paling banyak sekali.")
    for model in llm.candidates():
        try:
            llm.record_call(model)
            cfg = types.GenerateContentConfig(max_output_tokens=400, thinking_config=llm.thinking_config(model))
            reply = llm.clean(llm.get_client().models.generate_content(model=model, contents=prompt, config=cfg).text)
            if reply:
                llm.mark_ok(model)
                return reply
        except Exception as e:
            console.print(f"[dim red]  [{model}] {escape(llm.report_error(model, e))}[/dim red]")
    return f"Maaf {T}, hasil pencarian sudah ada tapi AI sedang penuh, jadi belum bisa saya ringkas. Coba tanya lagi sebentar lagi."


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
    message = _dynamic_context(text) + "\n" + message
    direct_ok = not QUESTION_RE.search(text)
    # a question's answer is spoken while it is written; an action's reply is the tool's own short result
    stream = _live is not None and not direct_ok and config.STREAM_REPLIES

    # always start from the fastest model that is available right now (never "sticks" to a slow one); a slow first
    # answer is hedged with the next model (_first_response) instead of waiting 20 s per model
    models = llm.candidates()
    tried, slow_failures, first, first_model = bool(models), 0, None, None
    if models:
        try:
            first_model, first = _first_response(models, _turn_contents(message), stream=stream)
            models = [first_model] + [m for m in models if m != first_model]
        except Cancelled:
            return None
        except Exception as e:
            if not isinstance(e, TimeoutError):
                console.print(f"[dim red]  [Gemini] {escape(str(e)[:120])}[/dim red]")
            models = []
    for model in models:
        _tool_results.clear()
        had_pending = safety.has_pending()
        try:
            reply = _run_turn(model, message, direct_ok, first if model == first_model else None, stream=stream)
            first = None
        except Cancelled:
            return _combine(_tool_results[-1:]) if _tool_results else None
        except Exception as e:
            console.print(f"[dim red]  [{model}] {escape(llm.report_error(model, e))}[/dim red]")
            if _tool_results:  # a skill already ran; don't repeat it via fallback
                llm.mark_ok(model)
                if any(name in NEEDS_REPLY_TOOLS for name, _, _ in _calls_this_turn):
                    return _answer_from_results(text)       # raw search results must never be read out
                return _combine(_tool_results[-1:])
            kind = llm.classify(e)
            if kind in llm.GLOBAL_FAILURES:
                break          # no internet / bad key / a bug: the next model would fail the same way
            if kind == "busy":
                slow_failures += 1
                if slow_failures >= 2:
                    break      # Google is slow right now: don't make the user wait for every model
            continue           # quota / retired model: the next one answers immediately
        llm.mark_ok(model)

        if safety.has_pending() and not had_pending and "?" not in reply:
            reply = safety.question()
        if not reply:
            reply = _combine(_tool_results[-1:]) if _tool_results else f"Selesai, {T}."
        if _live is not None and getattr(_live, "covered_text", None) == reply:
            _live.covered = True                 # core must not say it a second time
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
    "add_event", "list_events", "delete_event",
    "get_weather", "get_forecast", "get_system_status", "get_temperature", "open_folder",
    "lamp_power", "lamp_brightness", "lamp_color", "lamp_status", "open_file", "find_files", "remember", "recall_memories",
    "learned_habits", "lock_workstation", "shutdown_pc", "restart_pc", "sleep_pc", "google_search",
    "read_notifications", "reply_message", "lamp_scene", "sun_today", "watch_for", "list_watches", "cancel_watch", "daily_recap",
    "screen_time", "performance_report",
]
# words that mean the answer has to come from the internet, the screen, a document or a big Gemini skill
ONLINE_TOPIC_RE = re.compile(
    r"\b(?:berita|news|headline|harga|kurs|nilai tukar|saham|bitcoin|crypto|kripto|ihsg|skor|klasemen|"
    r"hasil (?:pertandingan|pemilu|liga)|jadwal (?:pertandingan|tayang|rilis|kereta|pesawat|sholat|shalat)|"
    r"trending|viral|gempa|banjir|cari(?:kan)? (?:di |informasi |info )?(?:internet|google|web|online)|"
    r"juara|pemenang|siapa yang menang|musim (?:ini|lalu|kemarin)|tahun lalu|bulan lalu|minggu lalu|"
    r"googling|browsing|search|cek (?:di )?internet|link|website (?:resmi|untuk))\b")
RECENT_RE = re.compile(r"\b(?:terbaru|terkini|hari ini|kemarin|semalam|minggu ini|bulan ini|tahun ini|barusan|"
                       r"sekarang|saat ini|update|20[2-3]\d)\b")
# "ada" asks only at the start ("ada acara apa?"); "tanggal 2 ada pameran" is a statement
ASKING_RE = re.compile(r"\b(?:apa|apakah|siapa|berapa|kapan|bagaimana|gimana|mana|kenapa|mengapa)\b|^ada\b|\?")
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
    r"penyimpanan|aplikasi|jendela|window|tab|timer|alarm|volume|kecerahan|wifi|jaringan|internet|jadwal|agenda|"
    r"acara|kegiatan)(?:ku|nya)?\b|\b(?:jadwal|agenda|acara)(?:ku|nya)?\b.*\b(?:apa|apa saja|ada|hari ini|besok)\b|"
    r"\b(?:apa|ada)\b.*\b(?:jadwal|agenda|acara|kegiatan)(?:ku|nya)?\b|"
    r"\b(?:ram|memori|cpu|baterai|aplikasi|jendela|timer|alarm|volume)\b.*\b(?:berapa|apa saja|yang (?:terbuka|aktif|berjalan))\b")
# small talk that may mention a PC word ("makasih sudah buka musiknya") but asks for nothing
CHAT_RE = re.compile(r"\b(?:makasih|terima kasih|thanks|mantap|keren|bagus|oke|ok|sip|wah|wow|haha|hehe|begitu dong|"
                     r"ceritakan|jelaskan|terangkan|pantun|puisi|cerita|dongeng|lelucon|joke|lawak|definisi|arti(?:nya)?|"
                     r"maksud(?:nya)?|sejarah|fakta|tips|saran)\b")
ACTION_VERB_RE = re.compile(
    r"^(?:tolong |coba |bisa |bisakah |mohon |dong )?(?:buka|bukakan|tutup|nyalakan|hidupkan|matikan|aktifkan|nonaktifkan|"
    r"putar|putarkan|setel|mainkan|jeda|pause|lanjutkan|hentikan|stop|kecilkan|besarkan|naikkan|turunkan|keraskan|"
    r"pelankan|atur|ubah|ganti|rubah|jalankan|pindah|pindahkan|kirim|ketik|ketikkan|tekan|klik|pencet|kunci|minimize|"
    r"balas|bales|balasin|balaskan|jawab|jawabin|bilang ke|bilangin|sampaikan|kabari|kabarin|berikan jawaban|"
    r"beri jawaban|kasih jawaban|"
    r"maximize|nonton|tonton|restart|reboot|shutdown|kembali ke|balik ke|ingat|ingatkan|ingetin|ingatin|catat|catatkan|"
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


MULTI_ACTION_RE = re.compile(r"(?:^|\b(?:dan|lalu|terus|kemudian|habis itu|setelah itu|sambil)\s+|,\s*)"
                             r"(?:buka|tutup|putar|putarkan|ketik|ketikkan|cari|carikan|catat|tulis|kirim|setel|"
                             r"atur|matikan|nyalakan|pindah|pindahkan|simpan|ingatkan)\b")


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
    if len(MULTI_ACTION_RE.findall(cmd)) >= 2:
        return "beberapa perintah sekaligus"
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
    (r"^ke |kembali ke|balik ke|beralih|fokus|jendela|window|minimi|maksimal|maximi|kecilkan jendela|kiri|kanan|pindah|workspace|ruang kerja|desktop", [
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
    (r"acara|agenda|jadwal|ulang ?tahun|ultah|tanggal|rapat|meeting|kondangan|ujian|wisuda|arisan|janji|deadline", [
        "add_event", "list_events", "delete_event"]),
    (r"status|baterai|batre|cpu|ram|memori|sistem|laptop|komputer|pc|suhu|panas|kipas|temperatur|gpu", [
        "get_system_status", "get_temperature"]),
    (r"cuaca|hujan|panas|suhu|dingin|cerah|weather|besok|lusa|prakiraan|ramalan", ["get_weather", "get_forecast"]),
    (r"lampu|lamp|suasana", ["lamp_power", "lamp_brightness", "lamp_color", "lamp_scene", "lamp_status"]),
    (r"matahari|terbenam|terbit|senja", ["sun_today"]),
    (r"kabari|kabarin|beri ?tahu|kasih ?tahu|pantau|awasi", ["watch_for", "list_watches", "cancel_watch"]),
    (r"rangkuman|rekap|ringkasan hari|hari ini ngapain|screen time|waktu layar|di depan laptop", [
        "daily_recap", "screen_time"]),
    (r"kinerja|performa|seberapa (?:bagus|baik|cepat)", ["performance_report"]),
    (r"pesan|chat|notif|notifikasi|whatsapp|telegram|email|surel", ["read_notifications"]),
    (r"balas|bales|kirim (?:pesan|wa|whatsapp)", ["reply_message"]),
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
    wanted = set(names)
    seen = [n for n in LOCAL_TOOL_NAMES if n in wanted and n in TOOL_MAP]     # one fixed order: a stable prompt prefix
    return [_schema(n) for n in seen]


def _local_system_prompt(offline: bool) -> str:
    """Only things that rarely change: Ollama reuses the processed prompt (tools included) while it stays the same,
    which saves seconds per command. The clock and the active window go into the user message instead."""
    pronouns = ('sebut dirimu "aku" dan panggil user "kamu", akrab seperti teman dekat.' if config.ADDRESS_STYLE == "kamu"
                else 'sebut dirimu "saya" dan panggil user "Anda" (jangan "aku/kamu"), tetap santai.')
    import random
    hour = datetime.datetime.now().hour
    part = "pagi" if 4 <= hour < 11 else "siang" if hour < 15 else "sore" if hour < 18 else "malam"
    # the same examples for the whole part of the day: an unchanged prompt is reused by Ollama (~1 s saved per request)
    rng = random.Random(f"{datetime.date.today()}-{part}")
    examples = "\n".join(f"User: {q} → {rng.choice(answers)}".replace("{part}", part)
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
    if config.LOCAL_ROUTER:
        return _ask_routed(text, offline)
    messages = [{"role": "system", "content": _local_system_prompt(offline)}]
    for turn in _dialog[-DIALOG_TURNS:]:
        messages += [{"role": "user", "content": turn["user"][:300]}, {"role": "assistant", "content": turn["reply"][:300]}]
    context = _dynamic_context(text, active=False)
    notes = " | ".join(_system_notes[-4:])
    messages.append({"role": "user", "content": context + (f"\n[Catatan: {notes}]" if notes else "") + "\n" + text})
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
                    metrics.count("eskalasi_lokal")
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
                _check_cancel()
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
                return safety.question()
            if direct_ok and not any(c["function"]["name"] in NEEDS_REPLY_TOOLS for c in calls):
                _brain_used = "local"
                return _combine(results)
        _brain_used = "local"
        return _combine(ran[-1:]) if ran else None
    except Cancelled:
        return _combine(ran[-1:]) if ran else None
    except Exception as e:
        console.print(f"[dim red]  [lokal {config.LOCAL_MODEL}] {escape(str(e)[:120])}[/dim red]")
        if "Connection" in type(e).__name__ or "connect" in str(e).lower():
            local_llm.mark_down("Ollama tidak berjalan")
        return _combine(ran[-1:]) if ran else None     # a skill already ran: never run it twice via Gemini


RESUME_RE = re.compile(r"(?:oke |ok |ya |baik )?(?:sudah|udah|beres|selesai|lanjut|lanjutkan|terus|teruskan|terusin)"
                       r"(?: ya| dong| tugasnya| sekarang)?")
TALK_RULES = """Di giliran ini kamu hanya berbicara: tidak ada yang kamu lakukan di komputer, jadi jangan pernah mengaku \
sudah atau sedang melakukan sesuatu. Kalau tidak tahu, bilang tidak tahu; jangan mengarang fakta, angka, atau nama."""


def _talk_prompt(offline: bool) -> str:
    """The character without the tool rules: for answering in words."""
    return _local_system_prompt(offline).split("Aturan kerja:")[0].rstrip() + "\n\n" + TALK_RULES


def _local_talk(text: str, offline: bool) -> str | None:
    """Small talk with the local model; None when it claims a deed anyway (the online AI takes over)."""
    global _brain_used
    history = [(t["user"][:300], t["reply"][:300]) for t in _dialog[-DIALOG_TURNS:]]
    reply = local_llm.generate(_talk_prompt(offline), f"{_dynamic_context(text, active=False)}\n{text}", history=history,
                               max_tokens=160, temperature=0.7)
    if not reply or (not offline and (ACTION_CLAIM_RE.search(reply) or DONE_CLAIM_RE.search(reply))):
        return None
    _brain_used = "local"
    return EMOJI_RE.sub("", reply).strip()


def _routed_tool(tool: str, text: str, context: str, offline: bool) -> str | None:
    """The chosen skill: its arguments, the run, the answer. None = the online AI takes over."""
    global _brain_used
    fn = TOOL_MAP.get(tool)
    if not fn:
        return None
    shots = [(i["text"], i["args"]) for i in experience.similar(text, tool=tool, limit=2)]
    args = local_router.fill_args(fn, text, context, shots)
    if args is None:
        console.print(f"[dim]  ↪ argumen {tool} belum jelas, diteruskan ke Gemini[/dim]")
        return None
    _check_cancel()
    result = str(fn(**_coerce_args(fn, args)))
    _brain_used = "local"
    if safety.has_pending():
        return safety.question()
    if tool in NEEDS_REPLY_TOOLS or QUESTION_RE.search(text):     # asked something: the answer, not the whole report
        try:
            answer = local_llm.generate(_talk_prompt(offline), f"Pertanyaan user: {text}\nHasil dari sistem: {result[:1500]}\n"
                                        "Jawab singkat berdasarkan hasil itu saja, jangan menambah apa pun.", max_tokens=120)
            if answer:
                return EMOJI_RE.sub("", answer).strip()
        except Exception as e:
            diag.warn_once("local.phrase", "otak lokal gagal merangkai hasil alat", e)
    return _combine([result])


def _ask_routed(text: str, offline: bool) -> str | None:
    """The local brain: one choice from a fixed list (local_router.py), then that choice carried out. None = the
    online AI takes the request (it needs the internet, or the local model failed)."""
    global _brain_used
    turn = _dialog[-1] if _dialog and time.time() - _dialog[-1].get("at", 0) < 120 else None
    try:
        similar = [(i["text"], i["tool"]) for i in experience.similar(text)]
        choice = local_router.pick(text, (turn["user"], turn["reply"]) if turn else None, similar)
        console.print(f"[dim]  🧭 pilihan otak lokal: {choice}[/dim]")
        if choice == local_router.ASK:
            _brain_used = "local"
            return f"Maaf {T}, kurang jelas. Maksudnya apa?"
        if choice == local_router.CHAT or (offline and choice in (local_router.AI, local_router.KNOW)):
            return _local_talk(text, offline)
        if choice in (local_router.AI, local_router.KNOW):
            console.print("[dim]  ↪ diteruskan ke Gemini[/dim]")
            return None
        return _routed_tool(choice, text, _dynamic_context(text, active=False), offline)
    except Cancelled:
        return None
    except Exception as e:
        console.print(f"[dim red]  [lokal {config.LOCAL_MODEL}] {escape(str(e)[:120])}[/dim red]")
        if "Connection" in type(e).__name__ or "connect" in str(e).lower():
            local_llm.mark_down("Ollama tidak berjalan")
        return _combine([r for _, _, r in _calls_this_turn[-1:]]) if _calls_this_turn else None   # never run a skill twice


# a scrap of speech with no request in it ("dan tik", "this", "yang itu tadi") — misheard; ask instead of guessing
FRAGMENT_RE = re.compile(r"(?:dan|sama|terus|lalu|atau|tapi|trus|this|that|the|a|um|eh|hmm|anu)(?: \S{1,12})?")


# short questions about one current fact: a web search answered by the local model takes ~2.5 s (Gemini 5-15 s)
QUICK_FACT_RE = re.compile(r"\b(?:harga|kurs|nilai tukar|skor|klasemen|hasil pertandingan|jadwal (?:sholat|shalat|imsak)|"
                           r"hasil (?:liga|pertandingan)|bitcoin|emas|saham|juara|pemenang)\b")
NOT_FOUND_RE = re.compile(r"belum ketemu|tidak (?:menyebutkan|ditemukan|ada informasi)|tidak memuat", re.IGNORECASE)


_search_answers: dict[str, tuple[float, str]] = {}     # the same question again within a minute: no second search


def _local_search_answer(text: str) -> str | None:
    """Gemini is down or too slow for a question about current facts: search the web here (~1 s) and let the local
    model answer from the results — never from its own (outdated) memory, which only makes numbers up."""
    global _brain_used
    key = _normalize(text)
    cached = _search_answers.get(key)
    if cached and time.time() - cached[0] < 60:
        _brain_used = "local"
        return cached[1]
    if not local_llm.available():
        return None
    try:
        results = ai_skills.web_search(text)
    except Exception:
        return None
    if results.startswith("Pencarian untuk"):          # nothing found / no internet
        return None
    prompt = (f"{_dynamic_context(text, active=False)}\n[Hasil pencarian internet]\n{results[:3500]}\n\n"
              f"Jawab pertanyaan user HANYA dari hasil pencarian di atas, 1-2 kalimat lisan. Sebut angka/fakta terbaru "
              f"beserta sumbernya. Jika hasil di atas tidak memuat jawabannya, katakan belum ketemu; jangan menebak.\n"
              f"Pertanyaan: {text}")
    try:
        msg = local_llm.chat([{"role": "system", "content": _local_system_prompt(False)},
                              {"role": "user", "content": prompt}], None, max_tokens=200)
    except Exception:
        return None
    reply = EMOJI_RE.sub("", msg.get("content", "")).strip()
    if reply:
        _brain_used = "local"
        _search_answers[key] = (time.time(), reply)
        console.print("[dim]  ↪ dijawab otak lokal dari hasil pencarian web[/dim]")
    return reply or None


def _think(text: str) -> str | None:
    """Pick the brain for a request that isn't a simple fast command."""
    global _brain_used
    mode = config.BRAIN_MODE
    cmd = _normalize(text)
    if FRAGMENT_RE.fullmatch(cmd) and not (_dialog and _dialog[-1]["reply"].rstrip().endswith("?")):
        _brain_used = "fast"
        return f"Maaf {T}, sepertinya kepotong. Maksudnya apa?"
    reason = gemini_reason(text)
    if mode == "hybrid" and reason in ("butuh internet", "data terbaru") and QUICK_FACT_RE.search(cmd) \
            and len(cmd.split()) <= 12 and not ACTION_VERB_RE.search(cmd):
        reply = _local_search_answer(text)             # "harga emas hari ini": searched + answered here, ~2.5 s
        if reply and not NOT_FOUND_RE.search(reply):
            return reply
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
    if reason in ("butuh internet", "data terbaru"):
        reply = _local_search_answer(text)          # current facts: from a real search, never made up
        if reply:
            return reply
        return (f"Maaf {T}, AI online sedang lambat dan pencarian juga belum berhasil, jadi informasi terbarunya belum "
                f"bisa saya cek. Coba tanya lagi sebentar lagi.")
    if mode != "gemini" or local_llm.available():
        reply = ask_local(text, offline=True)     # Gemini is down: the local model answers what it can
        if reply:
            return reply
    return None


def _remember_turn(user: str, reply: str):
    turn = {"user": user, "reply": reply, "brain": _brain_used or "fast", "at": time.time()}
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
            joined = part.replace(" ", "")
            names.append(joined if joined in sc.WEBSITES else part)     # "tik tok" -> tiktok (not a Google search)
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
        target = site.group(1)          # "situs lms kampus" -> Gemini knows the address better
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
    # misheard / cut off ("termi", "mouse pa", "ig"): the resolver; inside a list ("x dan y") only a sure match counts
    return _resolved("open", target, sure_only=single)


def _close_known(target: str, single: bool = False):
    target = target.strip()
    if re.match(r"semua (?!aplikasi|jendela|program|app|window|tab\b)\S", target):
        target = target[6:]                      # "tutup semua tik to": every TikTok tab / window = close TikTok
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
    if len(target) >= 2 and not re.search(r"\b(?:semua|tab|ini|itu|jendela)\b", target):
        # misheard: "tutup file" -> File manager (not Firefox), "tutup tito" -> the TikTok tab; only what is open
        return _resolved("close", target, sure_only=single)
    return None


# ── names the lists don't know: the resolver ("buka termi", "ke disco", "tutup tito") ──
_pending_pick: dict = {}       # {"heard", "purpose", "options": [(label, key, (tool, kwargs))], "at"}: "Maksudnya X?"
PICK_SECONDS = 40
PICK_VERB = {"open": "buka", "close": "tutup", "focus": "pindah ke"}


def _is_open(m) -> bool:
    """For "tutup ...": is this candidate actually open right now?"""
    if m.kind == "window":
        return True
    if m.kind == "site":
        try:
            import screen_context
            return bool(screen_context._browser_windows())
        except Exception:
            return False
    try:
        import window_manager
        if any(w["process"].lower() == m.target for w in window_manager.closable_windows()):
            return True
    except Exception:
        pass
    return bool(sc._matching_processes(m.key))


def _action_for(m, purpose: str):
    if purpose == "close":
        return ("close_tab", {"name": m.key}) if m.kind == "site" else ("close_application", {"name": m.key})
    if purpose == "focus" or m.kind == "window":          # already open: bring it to the front
        if m.kind == "app":
            return "open_application", {"name": m.key}     # "ke telegram" while it isn't open: open it
        return "focus_window", {"name": m.key}
    return ("open_website", {"site": m.key}) if m.kind == "site" else ("open_application", {"name": m.key})


def _resolved(purpose: str, heard: str, sure_only: bool = False):
    """(tool, kwargs) for a sure match, ("_ask", {...}) to ask "Maksudnya X?", or None (the AI decides)."""
    import resolver
    kinds = {"open": ("app", "site", "window"), "close": ("window", "app", "site"),
             "focus": ("window", "site", "app")}[purpose]
    verdict, matches = resolver.resolve(heard, kinds, keep=_is_open if purpose == "close" else None,
                                        sure_only=sure_only)
    if verdict == "sure":
        return _action_for(matches[0], purpose)
    if verdict == "ask":
        return "_ask", {"heard": heard, "purpose": purpose,
                        "options": [(m.label, m.key, _action_for(m, purpose)) for m in matches]}
    return None


def _ask_close(target: str):
    import resolver
    verdict, matches = resolver.resolve(target, ("window", "app", "site"), keep=_is_open, sure_only=True)
    if verdict != "sure":
        return None
    m = matches[0]
    return "_ask", {"heard": target, "purpose": "close", "options": [(m.label, m.key, _action_for(m, "close"))]}


def _focus_known(target: str, raw_fallback: bool = True):
    target = re.sub(r"^(?:jendela|aplikasi|app) ", "", target.strip())
    found = _resolved("focus", target)
    if found:
        return found
    return ("focus_window", {"name": target}) if raw_fallback else None


def _ask_pick(info: dict) -> str:
    _pending_pick.clear()
    _pending_pick.update(info, at=time.time())
    verb = PICK_VERB[info["purpose"]]
    labels = [label for label, _, _ in info["options"]]
    what = f"{labels[0]} atau {labels[1]}" if len(labels) > 1 else labels[0]
    return f"Maksudnya {verb} {what}, {T}?"


PICK_YES_RE = re.compile(r"(?:ya|iya|iyah|yap|yup|betul|benar|bener|oke|ok|okay|boleh|silakan|itu|yang itu|yang pertama|"
                         r"pertama|nomor satu|yang satu|lanjut)(?: (?:dong|deh|benar|betul|itu|yang itu|tuh))?")
PICK_SECOND_RE = re.compile(r"(?:yang )?(?:kedua|dua|nomor dua|terakhir|yang lain|yang satunya)")
PICK_NO_RE = re.compile(r"(?:bukan|tidak|nggak|enggak|gak|ga|salah|batal|jangan)\b.*")


def _answer_pick(cmd: str) -> str | None:
    """The answer to "Maksudnya buka TikTok?": runs the choice and learns the mishearing for next time."""
    if not _pending_pick:
        return None
    if time.time() - _pending_pick["at"] > PICK_SECONDS:
        _pending_pick.clear()
        return None
    options = _pending_pick["options"]
    choice = None
    if PICK_YES_RE.fullmatch(cmd):
        choice = options[0]
    elif PICK_SECOND_RE.fullmatch(cmd) and len(options) > 1:
        choice = options[1]
    else:
        for option in options:
            if option[0].lower() in cmd or option[1] in cmd:
                choice = option
                break
    heard = _pending_pick["heard"]
    if choice is None:
        _pending_pick.clear()
        if PICK_NO_RE.fullmatch(cmd):
            return f"Baik, {T}. Sebutkan lagi namanya."
        return None                                  # the user moved on to something else
    _pending_pick.clear()
    label, key, (name, kwargs) = choice
    result = str(TOOL_MAP[name](**kwargs))
    try:
        import resolver
        if 3 <= len(heard) <= 30 and heard not in resolver.NOT_NAMES and heard != key and len(heard.split()) <= 3:
            habits.learn_alias(heard, key)           # "tito" -> tiktok from now on, no question
    except Exception:
        pass
    _system_notes.append(f"user memilih {label} untuk '{heard}': {result}")
    return result


# speech recognition often hears "workspace 4" as "word PS4" / "WordPress 4" / "works ps 2"
WORKSPACE = r"(?:workspace|work space|workspes|wokspace|ruang kerja|desktop|words? ?ps|works? ?ps|for ps|wordpress|word press|work|ws|sp|es pe|we es|(?:kurs|kursus|course|kers) ?ps)"
WS_NUM = r"(?:ke-? ?|nomor |no )?(?:\d{1,2}|" + "|".join(NUMBER_WORDS) + ")"
WS_GLUED = re.compile(r"\b(words?|works?|work|for|kurs|kursus|course|kers) ?ps(\d{1,2})\b")   # "word ps4" -> "word ps 4"


def _ws(words: str) -> str:
    word = re.split(r"[\s-]", words)[-1]          # "ke-4" / "nomor empat" -> the number
    return str(_num(word) or word)


def _ws_route(name: str, kwargs: dict):
    return (name, kwargs) if name in TOOL_MAP else None      # workspaces exist on Linux (Hyprland) only


FORECAST_WHEN = r"(?:besok|lusa|nanti(?: sore| malam| siang| pagi)?|hari ini|sore ini|malam ini|siang ini|sekarang)"


def _forecast_when(said: str) -> str:
    return next((w for w in ("lusa", "besok", "hari ini") if w in said), "nanti")


def _read_offered():
    """"ya" right after "Mau saya bacakan?": read what was offered. None = nothing was offered (a normal "ya")."""
    senders = notifications.take_offer()
    if senders is None:
        return None
    return "read_notifications", ({"messages_only": True, "sender": senders} if senders else {"messages_only": True})




# said alone these are questions, even when an app has the same name (the clock app is "jam")
QUESTION_WORDS = {"jam", "waktu", "tanggal", "cuaca", "berita", "hari", "jam sekarang", "sekarang jam"}


def _bare_name(cmd: str):
    """Just the name of a site or app the user often opens ("instagram", "youtube", "ig", "tik tok dan instagram"):
    open it. Only exact names and short forms: a random word ("dingin") must never open something."""
    import resolver
    if cmd in QUESTION_WORDS:
        return None                      # "jam" alone asks the time ("buka jam" still opens the clock)
    cmd = resolver.SHORT.get(cmd, cmd)
    if cmd.replace(" ", "") in sc.WEBSITES:
        return "open_website", {"site": cmd.replace(" ", "")}
    if cmd in sc.WEBSITES:
        return "open_website", {"site": cmd}
    if cmd in sc.APPS:
        return "open_application", {"name": cmd}
    if OPEN_MANY_SPLIT.search(cmd):
        parts = [resolver.SHORT.get(p.strip(), p.strip()) for p in OPEN_MANY_SPLIT.split(cmd) if p.strip()]
        if len(parts) >= 2 and all(p.replace(" ", "") in sc.WEBSITES or p in sc.APPS for p in parts):
            return "open_many", {"names": [p.replace(" ", "") if p.replace(" ", "") in sc.WEBSITES else p for p in parts]}
    if re.fullmatch(r"[a-z]{3,12}", cmd) and cmd not in resolver.NOT_NAMES:
        ranked = resolver.rank(cmd, ("site", "app"))
        # one cut-off word ("tik"): only a near-certain start of a name, nothing merely similar ("dingin")
        if ranked and ranked[0].score >= 0.93 and (len(ranked) == 1 or ranked[1].score < ranked[0].score - 0.04):
            m = ranked[0]
            return ("open_website", {"site": m.key}) if m.kind == "site" else ("open_application", {"name": m.key})
    return None


def _open_in_browser(target: str, browser: str):
    """'buka github di firefox' -> that site in that browser."""
    import resolver
    target = resolver.SHORT.get(target.strip(), target.strip())
    if target.replace(" ", "") in sc.WEBSITES:
        return "open_website", {"site": target.replace(" ", ""), "browser": browser}
    if re.fullmatch(r"[\w-]+(?:\.[\w-]+)+(?:/\S*)?", target):
        return "open_website", {"site": target, "browser": browser}
    verdict, matches = resolver.resolve(target, ("site",), sure_only=True)
    return ("open_website", {"site": matches[0].key, "browser": browser}) if verdict == "sure" else None


def _active_title() -> str:
    try:
        import window_manager
        wins = window_manager.closable_windows()
        return (wins[0]["title"] if wins else "").lower()
    except Exception:
        return ""


def _search_route(query: str):
    """'cari kepiting': YouTube when YouTube is in front (or it is music / a video), otherwise Google."""
    query = re.sub(r"\s+(?:di google|di internet|di web|dong)$", "", query.strip())
    if not query:
        return None
    if "youtube" in _active_title() or re.match(r"(?:video|musik|klip|lagu) ", query):
        return "search_youtube", {"query": query}
    return "google_search", {"query": query}


def _route(text: str):
    """(tool name, kwargs) for a simple command, or None if Gemini should handle it."""
    cmd = WS_GLUED.sub(r"\1 ps \2", _normalize(text))
    cmd = re.sub(r"\b(ws|sp)(\d{1,2})\b", r"\1 \2", cmd)                 # "sp1" -> "sp 1" (= workspace 1)
    cmd = re.sub(r"\bke(merah|putih|biru|hijau|kuning|ungu|pink|oranye|jingga|toska)\b", r"ke \1", cmd)   # "keputih"
    cmd = re.sub(r"^(.{3,40}?)(?: \1)+$", r"\1", cmd)     # "putar lagu. putar lagu. putar lagu": said once
    cmd = re.sub(r"^(?:titik|tik|ketip|ketig|ketek) (?=.+ (?:dan|lalu|terus) (?:tekan |klik )?enter$)", "ketik ", cmd)
    cmd = re.sub(rf"\bpindah keluar (?={WORKSPACE} )", "pindah ke ", cmd)   # "pindah keluar SP1" = "pindah ke ws 1"
    bare = _bare_name(cmd)
    if bare:
        return bare
    for pattern, handler in FAST_ROUTES:
        match = pattern.fullmatch(cmd)
        if match:
            action = handler(match)
            if not action:
                continue                 # this pattern declined ("cari kepiting" isn't a site): a later one may fit
            if action[0] in VERBATIM_ARGS:
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


# "balas oke saya otw" (to the chat just heard about), "balas ke Rina bilang …", "kirim WhatsApp ke Budi isinya …":
# matched on the original text, so the message keeps its own capitals and punctuation
_REPLY_SEP = r"(?:\s+(?:isinya|bilang|bahwa|katakan)\b|\s*[:,])\s*"
REPLY_TO_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:balas|bales|balasin|balaskan)(?:lah)?(?:\s+(?:pesan|chat|wa)(?:nya)?)?"
                         r"\s+(?:ke|kepada|untuk|buat)\s+(?P<to>[^:,]{2,40}?)" + _REPLY_SEP + r"(?P<text>\S.*?)\s*$", re.I)
SEND_TO_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:kirim|kirimkan|kirimin)(?:\s+pesan)?\s+(?:wa|whatsapp|pesan wa|"
                        r"pesan whatsapp|pesan)\s+(?:ke|kepada|untuk|buat)\s+(?P<to>[^:,]{2,40}?)" + _REPLY_SEP +
                        r"(?P<text>\S.*?)\s*$", re.I)
REPLY_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:balas|bales|balasin|balaskan)(?:lah)?(?:\s+(?:pesan|chat|wa)(?:nya)?|"
                      r"\s+dia)?(?:\s+(?:isinya|bilang|bahwa|dengan))?\s*[:,]?\s+(?P<text>\S.*?)\s*$", re.I)


# "jawab oke", "bilang ke dia nanti saya telepon", "berikan jawaban oke": also a reply, but only right after a message
# was announced or read out (otherwise "jawab pertanyaan ini" is meant for Digidaw)
ANSWER_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:(?:jawab|jawabin|jawablah|jawabkan)(?:\s+(?:pesan|chat)(?:nya)?)?"
                       r"(?:\s+(?:ke\s+)?dia)?|(?:bilang|bilangin|sampaikan|katakan)\s+(?:ke|kepada|sama)\s+(?:dia|nya)|"
                       r"(?:berikan|beri|kasih|kirim|kirimkan)\s+(?:jawaban|balasan)(?:\s+(?:ke|kepada)\s+dia)?)"
                       r"(?:\s+(?:isinya|bilang|bahwa|kalau|dengan))?\s*[:,]?\s+(?P<text>\S.*?)\s*$", re.I)
# "bilang ke Rina kalau saya telat", "sampaikan ke Budi: oke"
TELL_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:bilang|bilangin|sampaikan|kabari|kabarin|kasih tahu|beri tahu|beritahu)"
                     r"\s+(?:ke|kepada|sama)\s+(?!(?:dia|saya|aku|gue)\b)(?P<to>[^:,]{2,40}?)"
                     r"(?:\s+(?:kalau|bahwa|isinya)\b\s*|\s*[:,]\s*)(?P<text>\S.*?)\s*$", re.I)
# "kirim pesan ke bisnis rar": no "isinya" between the name and the message — split at a chat name Digidaw knows
SEND_LOOSE_RE = re.compile(r"^\s*(?:tolong\s+|coba\s+)?(?:kirim|kirimkan|kirimin)(?:\s+(?:pesan|chat))?(?:\s+(?:wa|whatsapp))?"
                           r"\s+(?:ke|kepada|untuk|buat)\s+(?P<rest>\S.*?)[\s.!?]*$", re.I)
# "kirim pesan WhatsApp" / "balas pesannya" with nothing else: Digidaw asks for what is missing
COMPOSE_RE = re.compile(r"(?:kirim|kirimkan|kirimin|bikin|buat|tulis) (?:pesan |chat )?(?:wa|whatsapp)|"
                        r"(?:kirim|kirimkan|kirimin) (?:pesan|chat)|(?:balas|bales|balasin|balaskan|jawab)(?: (?:pesan|chat)(?:nya)?| dia)?")


def _reply_route(text: str):
    """(to, message) for a spoken reply to a chat, or None."""
    for pattern in (REPLY_TO_RE, SEND_TO_RE, TELL_RE, REPLY_RE, ANSWER_RE):
        m = pattern.match(text)
        if m:
            to = (m.groupdict().get("to") or "").strip()
            message = m["text"].strip().rstrip(".")
            if pattern is REPLY_RE and re.match(r"(?:email|surel|komentar|chat ini|pesan ini|ke\b)", message, re.I):
                return None              # "balas email …": not a WhatsApp reply
            if pattern is ANSWER_RE and not notifications.last_chat(max_age=10 * 60):
                return None              # no message just came in: "jawab …" is for Digidaw itself
            return to, message
    m = SEND_LOOSE_RE.match(text)
    if m:
        rest = m["rest"].strip()
        for name in sorted(notifications.known_chats(), key=len, reverse=True):
            if rest.lower().startswith(name.lower() + " ") and len(rest) > len(name) + 1:
                return name, rest[len(name):].strip(" ,:")
    return None


def _message_route(text: str) -> str | None:
    """Messages by voice: a reply / "bilang ke Rina kalau …" (asks "yakin?" before anything is sent), or a message
    built up step by step when a part is missing ("Ke siapa?", "Pesannya apa?")."""
    if whatsapp.composing():
        return _combine([whatsapp.compose_answer(text)])
    reply_to = _reply_route(text)
    if reply_to:
        return _combine([str(TOOL_MAP["reply_message"](text=reply_to[1], to=reply_to[0]))])
    cmd = _normalize(text)
    if COMPOSE_RE.fullmatch(cmd):
        last = notifications.last_chat(max_age=10 * 60) if cmd.startswith(("balas", "bales", "jawab")) else None
        if cmd.startswith(("balas", "bales", "jawab")) and not last:
            return None
        return whatsapp.compose(last["name"] if last else "")
    m = SEND_LOOSE_RE.match(text)
    if m and len(m["rest"].split()) <= 3 and not re.search(r"\b(?:workspace|ws|layar|monitor|desktop)\b", m["rest"], re.I):
        return whatsapp.compose(m["rest"].strip())        # "kirim pesan ke Bisnis": the text is still missing
    return None


# "lagi", "dikit lagi", "sekali lagi": the last adjustment once more (volume, lamp, screen, next song)
_last_fast: dict = {"tool": "", "kwargs": {}, "at": 0.0}
REPEAT_SECONDS = 90
REPEATABLE = {"volume_up", "volume_down", "lamp_brightness", "media_control", "scroll", "zoom"}


def _repeat_last(said: str):
    """The last fast command again ("lagi"), a little less of it ("dikit lagi": half the step), or None when there
    is nothing sensible to repeat (then the AI hears "lagi" as it is)."""
    tool, kwargs = _last_fast["tool"], dict(_last_fast["kwargs"])
    if time.time() - _last_fast["at"] > REPEAT_SECONDS or tool not in REPEATABLE:
        return None
    if tool == "lamp_brightness" and not kwargs.get("relative"):
        return None                      # "lampu 50%" again changes nothing
    if tool == "media_control" and kwargs.get("action") not in ("next", "previous", "skip_ad"):
        return None
    if re.search(r"\b(?:dikit|sedikit)\b", said):
        for key in ("amount", "percent"):
            if isinstance(kwargs.get(key), (int, float)):
                kwargs[key] = int(kwargs[key] / 2) or (1 if kwargs[key] > 0 else -1)
    return tool, kwargs


def fast_route(text: str) -> str | None:
    """Run a simple command locally. Returns the reply, or None if Gemini should handle it."""
    message = _message_route(text)               # "balas oke saya otw", "kirim pesan ke Bisnis" … (always "yakin?")
    if message:
        return message
    watch = watchers.parse(text)                 # "kabari saya kalau …" (before the alarms: "ingatkan saya kalau …")
    if watch:
        return _combine([str(TOOL_MAP["watch_for"](*watch))])
    if not alarms.is_ringing():
        try:
            reply = agenda.handle(text)          # dated events ("ulang tahun ibu tanggal 20 Oktober") before alarms
        except Exception as e:
            console.print(f"[dim red]  [agenda] {escape(str(e)[:100])}[/dim red]")
            reply = None
        if reply:
            _calls_this_turn.append(("agenda", {}, reply))
            return _combine([reply])
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
        if name == "_ask":                       # a name heard only half-right: "Maksudnya buka TikTok?"
            return _ask_pick(kwargs)
        result = str(TOOL_MAP[name](**kwargs))
        if name != "_repeat":
            _last_fast.update(tool=name, kwargs=dict(kwargs), at=time.time())
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
    if has("suhu laptop", "suhu cpu", "suhu prosesor", "suhu komputer", "laptop panas", "laptop kepanasan"):
        return sc.get_temperature()
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


WAKE_PREFIX_RE = re.compile(r"^\s*(?:(?:hei|hai|halo|oke|ok|hey)\s+)?(?:digidaw|digi daw|didaw|digidau)\b[\s,.!?]*",
                            re.IGNORECASE)


def process_command(text: str) -> tuple[str, bool]:
    """
    Process incoming user text / speech.
    Returns: (response_text, should_exit)
    """
    global _brain_used, _current_command
    _command_local.id = _current_command = next(_command_ids)
    _tools_used.clear()
    _calls_this_turn.clear()
    _brain_used = ""
    text = WAKE_PREFIX_RE.sub("", text, count=1).strip() or text     # "Digidaw, buka Firefox." from the phone
    fixed = text if DICTATION_RE.match(text) else habits.apply_aliases(text)
    if fixed != text:
        console.print(f"[dim]  ↪ dikoreksi: {escape(fixed)}[/dim]")
    reply = _correction(fixed)
    should_exit = False
    if reply is None:
        reply, should_exit = _process_command(fixed)
    if _command_local.id in _cancelled:
        return "", False                 # timed out: its late answer and actions must not land in the next command
    if _calls_this_turn:
        _last_action.update(text=fixed, calls=list(_calls_this_turn), at=time.time())
        if _brain_used == "gemini" and len(_calls_this_turn) == 1 and not habits.FAILED_RESULT.search(_calls_this_turn[0][2]):
            experience.note(fixed, _calls_this_turn[0][0], _calls_this_turn[0][1])   # the teacher showed how: learned
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
    experience.forget(_last_action["text"])          # it did the wrong thing: not an example to learn from
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

    picked = _answer_pick(_normalize(text))
    if picked is not None:
        return picked, False

    # The screen job asked the user to do something ("isi kata sandinya"): "sudah" goes on
    if screen_agent.waiting() and RESUME_RE.fullmatch(cmd):
        return screen_agent.resume_task(), False

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


# the fast routes themselves live in routes.py, grouped by subject (they use the helpers above)
from routes import FAST_ROUTES  # noqa: E402
