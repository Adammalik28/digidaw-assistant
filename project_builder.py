"""
Project Builder: "Digidaw, buat project ..." -> a spoken brief -> a coding agent builds it -> Digidaw
starts it and confirms it's running. Afterwards the project can be changed by voice
("tambahkan fitur X ke project kalkulator"), started, stopped and reopened — also after a restart,
because every project keeps its metadata in <project>/.digidaw/project.json.

Flow
1. core collects the spoken brief (brief mode) and hands the transcript to plan().
2. plan() turns the transcript into a project name, a one-line summary and a clean spec (Gemini).
3. After the user confirms, start_build() runs the coding agent (Antigravity CLI by default, or
   Claude Code) non-interactively, streaming its progress to the HUD.
4. The agent leaves RUN.md with "DIGIDAW_RUN: <command>" / "DIGIDAW_URL: <url>". Digidaw starts that
   command in its own terminal window, checks the URL answers, opens it and announces the result.
5. improve_project() resumes the same agent conversation in the same folder with a change request,
   then restarts and re-verifies the project.

Several projects can be worked on at once (MAX_PARALLEL_BUILDS); further requests wait in a queue.
Every project gets its own port so one project's server is never mistaken for another's, and a
screenshot of the running page is shown on the HUD.
"""
import os
import re
import json
import time
import shutil
import difflib
import socket
import tempfile
import itertools
import datetime
import threading
import subprocess
import webbrowser
from pathlib import Path

import requests

import config
import events
import oscompat
import safety
import project_versions as versions

IS_WINDOWS = oscompat.IS_WINDOWS
T = config.USER_TITLE
ANNOUNCE = None      # set by main: speaks + shows on the HUD (core.announce)
BRIEF_HOOK = None    # set by main: core.start_brief(initial_text)

AGENT_LABELS = {"antigravity": "Antigravity", "claude": "Claude Code"}

PORT_BASE = 3100

RUN_RULES = """- Do NOT leave a long-running server or app running when you finish.
- Keep RUN.md in this directory up to date. Its first lines must be exactly:
  DIGIDAW_RUN: <one command, run from this directory, that starts the project>
  DIGIDAW_URL: <the http://localhost:PORT address if it serves a web page, otherwise: none>
  followed by a short explanation in Indonesian of what the project does and how to use it."""

BUILD_PROMPT = """You are building a brand-new software project from scratch in the CURRENT directory
(it is empty apart from BRIEF.md). The user described it by voice; the spec below was cleaned up
from that transcript, so interpret intent and choose sensible defaults instead of asking questions.

Requirements:
- Build a complete, working project. Prefer simple, popular stacks that run on Windows
  (Node.js/npm and Python 3.12 are installed).
- Install all dependencies and make sure it actually works: build it / run its tests / start it
  briefly to check there are no errors. Fix problems until it works.
- If it serves a web page, it MUST listen on port {port} (http://localhost:{port}); other projects use
  other ports.
{run_rules}

Project: {name}
Spec:
{spec}
"""

if not IS_WINDOWS:
    BUILD_PROMPT = BUILD_PROMPT.replace(
        "stacks that run on Windows\n  (Node.js/npm and Python 3.12 are installed).",
        "stacks that run on Arch Linux\n  (Node.js/npm and Python 3 are installed; use a virtualenv for Python packages, "
        "never sudo or pacman).")

CHANGE_PROMPT ="""Change the existing project in the CURRENT directory ("{name}") as the user asked by voice:

{request}

Interpret the request sensibly (it was transcribed from speech) and don't ask questions.
Read the current files first: they may have been edited by hand or restored to an earlier version
since your last turn.
Keep everything that already works, install any new dependencies, and verify the project still
builds / passes its tests / starts without errors. {port_rule}
{run_rules}
"""

_jobs: dict[str, dict] = {}     # job id -> build / change job (running, queued or recently finished)
_queue: list[str] = []          # job ids waiting for a free slot
_servers: dict[str, subprocess.Popen] = {}   # project folder -> terminal Digidaw started
_lock = threading.RLock()
_ids = itertools.count(1)
ACTIVE = ("building", "starting")
HIDDEN_KEYS = ("proc", "spec", "prompt", "files", "resume_id")


# ───────────────────────────── projects on disk ─────────────────────────────

def _projects_root() -> Path:
    root = Path(config.PROJECTS_ROOT) if config.PROJECTS_ROOT else Path.home() / "Projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _meta_path(folder: Path) -> Path:
    return folder / ".digidaw" / "project.json"


def _save_meta(b: dict):
    folder = Path(b["folder"])
    meta = {k: b.get(k) for k in ("name", "agent", "conversation_id", "created", "summary", "status", "url", "port")}
    meta["updated"] = time.time()
    _meta_path(folder).parent.mkdir(parents=True, exist_ok=True)
    _meta_path(folder).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _adopt_legacy(folder: Path):
    """Projects built before project.json existed: recover name and agent conversation from their files."""
    brief = folder / "BRIEF.md"
    heading = re.search(r"^#\s+(.+)$", brief.read_text(encoding="utf-8", errors="replace"), re.M) if brief.exists() else None
    conversation, agent = "", config.CODING_AGENT
    log = folder / ".digidaw" / "build.log"
    if log.exists():
        with open(log, encoding="utf-8", errors="replace") as f:
            first = f.readline()
        try:
            event = json.loads(first)
            conversation = event.get("conversation_id") or event.get("session_id") or ""
            agent = "antigravity" if "event" in event else "claude"
        except ValueError:
            pass
    created = log.stat().st_mtime if log.exists() else folder.stat().st_mtime
    _save_meta({"folder": str(folder), "name": heading.group(1).strip() if heading else folder.name, "agent": agent,
                "conversation_id": conversation, "created": created, "status": "done", "summary": "", "url": ""})


def all_projects() -> list[dict]:
    """Projects Digidaw has built, newest first."""
    for digidaw_dir in _projects_root().glob("*/.digidaw"):
        if not (digidaw_dir / "project.json").exists():
            try:
                _adopt_legacy(digidaw_dir.parent)
            except OSError:
                pass
    found = []
    for meta_file in _projects_root().glob("*/.digidaw/project.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta["folder"] = str(meta_file.parent.parent)
        found.append(meta)
    return sorted(found, key=lambda m: m.get("updated") or 0, reverse=True)


def _find(name: str = "") -> dict | None:
    """A project by (fuzzy) name, or the most recent one."""
    projects = all_projects()
    if not projects:
        return None
    if not name.strip():
        return projects[0]
    needle = name.lower().replace("project", "").replace("proyek", "").strip()
    for p in projects:
        label = f"{p.get('name', '')} {Path(p['folder']).name}".lower()
        if needle and (needle in label or all(w in label for w in needle.split())):
            return p
    names = {p.get("name", "").lower(): p for p in projects}
    close = difflib.get_close_matches(needle, list(names), n=1, cutoff=0.5)
    return names[close[0]] if close else None


# ───────────────────────────── helpers ─────────────────────────────

def agent_exe(agent: str) -> str | None:
    local_bin = Path.home() / ".local" / "bin"       # not always on PATH when started from the desktop
    if agent == "claude":
        candidates = [shutil.which("claude"), str(local_bin / "claude.exe"), str(local_bin / "claude")]
    else:
        candidates = [shutil.which("agy"), os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe"), str(local_bin / "agy")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def agent_command(agent: str, prompt: str, resume_id: str = "") -> list[str]:
    """Non-interactive command line for the chosen coding agent (optionally continuing a conversation)."""
    exe = agent_exe(agent)
    if not exe:
        raise FileNotFoundError(f"{AGENT_LABELS.get(agent, agent)} tidak ditemukan di komputer ini")
    cmd = [exe, "-p", prompt, "--output-format", "stream-json"]
    if agent == "claude":
        cmd.append("--verbose")   # Claude Code needs it for stream-json in print mode
        if resume_id:
            cmd.append("--continue")
    elif resume_id:
        cmd += ["--conversation", resume_id]
    if config.AGENT_SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    return cmd


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or f"project-{datetime.datetime.now():%Y%m%d-%H%M}"


def _unique_folder(slug: str) -> Path:
    folder = _projects_root() / slug
    n = 2
    while folder.exists() and any(folder.iterdir()):
        folder = _projects_root() / f"{slug}-{n}"
        n += 1
    return folder


def _say(text: str, level: str = "info"):
    if ANNOUNCE:
        ANNOUNCE(text, level)
    else:
        print(f"[DIGIDAW] {text}")


def _public(job: dict) -> dict:
    data = {k: v for k, v in job.items() if k not in HIDDEN_KEYS}
    data["files"] = len(job.get("files", ()))
    if job["status"] == "queued" and job["id"] in _queue:
        data["position"] = _queue.index(job["id"]) + 1
    return data


def _emit(job: dict, **data):
    events.emit("build", **{**_public(job), **data})


def _active() -> list[dict]:
    """Jobs whose coding agent is working (they occupy a slot)."""
    return [j for j in _jobs.values() if j["status"] == "building"]


def _pending() -> list[dict]:
    return [j for j in _jobs.values() if j["status"] in ACTIVE + ("queued",)]


def _busy(folder: str | Path | None = None) -> bool:
    """Is any job (or a job for this project folder) running or waiting?"""
    return any(folder is None or j["folder"] == str(folder) for j in _pending())


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _assign_port() -> int:
    """A port no other Digidaw project uses and nothing is listening on."""
    used = {int(m["port"]) for m in all_projects() if m.get("port")} | {j.get("port") for j in _jobs.values()}
    port = PORT_BASE
    while port in used or not _port_free(port):
        port += 1
    return port


def _url_ok(url: str) -> bool:
    try:
        return requests.get(url, timeout=3).status_code < 500
    except requests.RequestException:
        return False


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} menit" if minutes else f"{int(seconds)} detik"


# ───────────────────────────── plan ─────────────────────────────

def plan(transcript: str, agent: str = "", ask: bool = False) -> dict:
    """Turn a spoken brief into {name, slug, summary, spec, agent, questions}. With ask=True, up to two
    questions come back when the brief misses something essential (empty when it is clear enough)."""
    agent = agent or config.CODING_AGENT
    lower = transcript.lower()
    if "claude" in lower:
        agent = "claude"
    elif "antigravity" in lower or "anti gravity" in lower:
        agent = "antigravity"
    questions = []
    ask_rule = (
        "questions (array berisi 0 sampai 2 pertanyaan singkat bahasa Indonesia untuk user, masing-masing diakhiri "
        "tanda tanya. Tanyakan HANYA jika ada hal penting yang benar-benar belum jelas dan akan mengubah hasil secara "
        "besar, misalnya jenis aplikasi (web/desktop/mobile), fitur inti, atau data apa yang dikelola. JANGAN tanya "
        "soal warna, framework, atau detail yang bisa dipilih sendiri. Jika deskripsi sudah cukup, isi []), "
        if ask else "")
    try:
        import llm
        raw = llm.generate(
            "Transkrip suara berikut adalah permintaan user untuk membuat sebuah project software.\n"
            "Balas HANYA JSON dengan kunci: name (nama project singkat, 2-4 kata), "
            "summary (1 kalimat bahasa Indonesia, maksimal 25 kata, untuk dibacakan), " + ask_rule +
            "spec (spesifikasi lengkap dan jelas dalam bahasa Inggris untuk AI coding agent: tujuan, fitur, "
            "halaman/komponen, teknologi yang disarankan jika user tidak menyebut, tampilan).\n\n"
            f"Transkrip: {transcript}",
            max_tokens=1500)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        name = str(data.get("name") or "").strip() or "Project Baru"
        summary = str(data.get("summary") or "").strip() or transcript[:140]
        spec = str(data.get("spec") or "").strip() or transcript
        if ask and isinstance(data.get("questions"), list):
            questions = [str(q).strip() for q in data["questions"] if str(q).strip()][:2]
    except Exception:
        words = re.findall(r"\w+", transcript)
        name, summary, spec = " ".join(words[:4]).title() or "Project Baru", transcript[:160], transcript
    return {"name": name, "slug": _slug(name), "summary": summary, "spec": spec,
            "transcript": transcript, "agent": agent, "questions": questions}


# ───────────────────────────── build / change ─────────────────────────────

def _new_job(**fields) -> dict:
    job = {"id": f"b{next(_ids)}", "status": "queued", "step": "Menunggu giliran…", "steps": 0, "files": set(),
           "url": "", "error": "", "conversation_id": "", "queued_at": time.time(), "started": time.time()}
    job.update(fields)
    return job


def _submit(job: dict) -> str:
    """Start the job now if a slot is free, otherwise queue it. Returns '' or the queue position text."""
    with _lock:
        _jobs[job["id"]] = job
        for old in [j for j in _jobs.values() if j["status"] not in ACTIVE + ("queued",)][:-6]:
            _jobs.pop(old["id"], None)          # keep only the few most recent finished jobs
        if len(_active()) < max(1, config.MAX_PARALLEL_BUILDS):
            _launch(job)
            return ""
        job["status"] = "queued"
        _queue.append(job["id"])
        _save_meta(job)
        _emit(job)
        return f"antrean nomor {len(_queue)}"


def _launch(job: dict):
    job.update(status="building", started=time.time(), step="Menyiapkan agen…")
    _save_meta(job)
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"project-{job['id']}").start()
    _emit(job)


def _start_next():
    """A slot came free: start the oldest queued job."""
    with _lock:
        while _queue and len(_active()) < max(1, config.MAX_PARALLEL_BUILDS):
            job = _jobs.get(_queue.pop(0))
            if job and job["status"] == "queued":
                _launch(job)
                _say(f"Sekarang saya mulai mengerjakan {job['name']} dari antrean, {T}.", "info")
        for waiting in (_jobs.get(i) for i in _queue):
            if waiting:
                _emit(waiting)


def start_build(p: dict) -> str:
    """Start building a planned project in the background (or queue it). Returns what Digidaw says."""
    if not agent_exe(p["agent"]):
        return (f"{AGENT_LABELS[p['agent']]} tidak ditemukan di komputer ini, {T}. "
                f"Pilih agen lain di Control Panel, bagian Project Builder.")
    with _lock:
        folder = _unique_folder(p["slug"])
        (folder / ".digidaw").mkdir(parents=True, exist_ok=True)
        (folder / "BRIEF.md").write_text(
            f"# {p['name']}\n\n{p['summary']}\n\n## Spesifikasi\n\n{p['spec']}\n\n"
            f"## Transkrip suara\n\n{p['transcript']}\n", encoding="utf-8")
        port = _assign_port()
        job = _new_job(name=p["name"], folder=str(folder), agent=p["agent"], mode="build", created=time.time(),
                       summary=p["summary"], port=port,
                       prompt=BUILD_PROMPT.format(name=p["name"], spec=p["spec"], port=port, run_rules=RUN_RULES))
        queued = _submit(job)
    if queued:
        return (f"Baik, {T}. Sudah ada {len(_active())} project yang sedang dikerjakan, jadi {p['name']} masuk "
                f"{queued}. Saya mulai otomatis begitu ada yang selesai.")
    return (f"Baik, {T}. Saya mulai membuat {p['name']} dengan {AGENT_LABELS[p['agent']]}. "
            f"Ini bisa memakan beberapa menit; saya kabari begitu project-nya berjalan.")


def _describe_tool(name: str, params: dict) -> tuple[str, str | None]:
    """(progress text, file written) for one agent tool call."""
    target = params.get("TargetFile") or params.get("AbsolutePath") or params.get("file_path") or params.get("path") or ""
    short = Path(str(target)).name if target else ""
    command = str(params.get("CommandLine") or params.get("command") or "")[:80]
    if name in ("write_to_file", "Write", "notebook_edit"):
        return f"Menulis {short}", str(target)
    if name in ("replace_file_content", "multi_replace_file_content", "sed_file", "Edit", "MultiEdit"):
        return f"Mengubah {short}", str(target)
    if name in ("run_command", "Bash", "PowerShell"):
        return f"Menjalankan: {command}", None
    if name == "generate_image":
        return f"Membuat gambar: {str(params.get('ImageName') or '').strip(chr(34)).replace('_', ' ')}", None
    if name in ("view_file", "Read"):
        return f"Membaca {short}", None
    if name in ("list_dir", "find_by_name", "grep_search", "Glob", "Grep", "LS"):
        return "Menelusuri file project", None
    if name in ("search_web", "read_url_content", "WebSearch", "WebFetch"):
        return "Mencari referensi di web", None
    if name.startswith("browser_") or "browser" in name:
        return "Mengecek tampilan di browser", None
    return name.replace("_", " "), None


def _handle_event(event: dict, b: dict):
    """Update the build state from one stream-json event (Antigravity or Claude Code)."""
    # Antigravity: {"event": "init"|"step_update"|"result", ...}
    kind = event.get("event")
    if kind == "init":
        b["conversation_id"] = event.get("conversation_id") or b.get("conversation_id", "")
        return
    if kind == "step_update":
        su = event.get("step_update") or {}
        if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
            info = su.get("tool_info") or {}
            text, written = _describe_tool(su.get("tool_name") or info.get("name", "tool"), info.get("parameters") or {})
            b["step"], b["steps"] = text, b["steps"] + 1
            if written:
                b["files"].add(written)
        return
    if kind == "result":
        result = event.get("result") or {}
        b["agent_status"] = result.get("status", "")
        b["agent_summary"] = (result.get("response") or "")[-1500:]
        return
    # Claude Code: {"type": "system"|"assistant"|"result", ...}
    etype = event.get("type")
    if etype == "system":
        b["conversation_id"] = event.get("session_id") or b.get("conversation_id", "")
    elif etype == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                text, written = _describe_tool(block.get("name", "tool"), block.get("input") or {})
                b["step"], b["steps"] = text, b["steps"] + 1
                if written:
                    b["files"].add(written)
    elif etype == "result":
        b["agent_status"] = "SUCCESS" if not event.get("is_error") else "ERROR"
        b["agent_summary"] = str(event.get("result") or "")[-1500:]


def _run(b: dict):
    try:
        _run_job(b)
    finally:
        _start_next()


def _run_job(b: dict):
    folder = Path(b["folder"])
    log_path = folder / ".digidaw" / ("build.log" if b["mode"] == "build" else "change.log")
    deadline = time.time() + config.BUILD_TIMEOUT_MINUTES * 60
    last_emit = 0.0
    try:
        cmd = agent_command(b["agent"], b["prompt"], b.get("resume_id", ""))
        with open(log_path, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    creationflags=oscompat.NO_WINDOW)
            b["proc"] = proc
            threading.Thread(target=_watchdog, args=(b, proc, deadline), daemon=True).start()
            for line in proc.stdout:
                log.write(line)
                log.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    _handle_event(json.loads(line), b)
                except ValueError:
                    b["step"] = line[:120]
                if time.time() - last_emit > 1.0:
                    last_emit = time.time()
                    _emit(b)
            code = proc.wait()
    except Exception as e:
        b.update(status="failed", error=str(e))
        _save_meta(b)
        _emit(b)
        _say(f"Maaf {T}, pengerjaan {b['name']} gagal dimulai: {e}", "warning")
        return

    if b["status"] == "cancelled":
        _save_meta(b)
        return
    agent_failed = b.get("agent_status") not in (None, "", "SUCCESS")
    if (code != 0 or agent_failed) and not (folder / "RUN.md").exists():
        b.update(status="failed", error=f"agen berhenti ({b.get('agent_status') or 'kode ' + str(code)})")
        _save_meta(b)
        _emit(b)
        _say(f"Maaf {T}, {AGENT_LABELS[b['agent']]} berhenti sebelum {b['name']} selesai. "
             f"Detailnya ada di log project.{_rollback_hint(b)}", "warning")
        return
    if b.get("agent_summary"):
        events.emit("digidaw", text=f"Ringkasan dari {AGENT_LABELS[b['agent']]}: {b['agent_summary'][-600:]}")
    try:
        label = f"Perubahan: {b.get('request', '')}" if b["mode"] == "change" else f"Versi awal: {b['summary'] or b['name']}"
        saved = versions.snapshot(folder, label, kind=b["mode"], summary=b.get("agent_summary", ""))
        if saved:
            b["version"] = saved["number"]
    except OSError as e:
        print(f"[versions] {e}")
    b.update(status="starting", step="Menjalankan dan mengecek project…")
    _save_meta(b)
    _emit(b)
    threading.Thread(target=_start_next, daemon=True).start()   # the agent is done: its slot is free
    _launch_and_verify(b)


def _watchdog(b: dict, proc, deadline):
    while proc.poll() is None:
        if time.time() > deadline:
            b.update(status="failed", error="melebihi batas waktu")
            proc.kill()
            return
        time.sleep(2)


def _rollback_hint(b: dict) -> str:
    if b.get("mode") != "change" or len(versions.load(b["folder"])) < 2:
        return ""
    return f" Kalau perlu, ucapkan: kembalikan project {b['name']} ke versi sebelumnya."


def _run_info(folder: Path) -> tuple[str, str]:
    text = (folder / "RUN.md").read_text(encoding="utf-8", errors="replace") if (folder / "RUN.md").exists() else ""
    run = re.search(r"DIGIDAW_RUN:\s*`?([^`\r\n]+)`?", text)
    url = re.search(r"DIGIDAW_URL:\s*`?(https?://[^\s`]+)", text)
    return (run.group(1).strip() if run else ""), (url.group(1).strip() if url else "")


def open_terminal(command: str, folder: Path, title: str = "Project"):
    """Start the project in its own terminal window (so the user can see it) and remember it."""
    title = re.sub(r"[&|<>^\"%]", "", title)[:50]
    if IS_WINDOWS:
        proc = subprocess.Popen(["cmd", "/k", f"title {title} - Digidaw & {command}"], cwd=folder,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        import linux_desktop as ld
        argv = ld.terminal_argv(str(folder), command, f"{title} - Digidaw")
        if argv:
            proc = subprocess.Popen(argv, cwd=folder, start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:                                        # no terminal emulator: run it in the background, log to a file
            log = open(folder / ".digidaw" / "run.log", "a", encoding="utf-8")
            proc = subprocess.Popen(["bash", "-c", command], cwd=folder, start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    _servers[str(folder)] = proc


def _stop_server(folder: Path) -> bool:
    proc = _servers.pop(str(folder), None)
    if proc and proc.poll() is None:
        oscompat.kill_tree(proc.pid)
        return True
    return False


def _launch_and_verify(b: dict):
    folder = Path(b["folder"])
    command, url = _run_info(folder)
    took = _minutes(time.time() - b["started"])
    files = len(b.get("files", ()))
    done_what = "diperbarui" if b["mode"] == "change" else "selesai dibuat"
    detail = f"dalam {took}" + (f", {files} file" if files else "")
    if not command:
        b.update(status="done", step="Selesai (tanpa perintah menjalankan)")
        _save_meta(b)
        _emit(b)
        _say(f"{b['name']} sudah {done_what} {detail}, {T}, tetapi agen tidak menuliskan cara menjalankannya. "
             f"Saya buka folder project-nya.", "info")
        oscompat.open_folder(folder)
        return

    if url and _url_ok(url) and str(folder) not in _servers:
        # something already answers on that address (e.g. an old copy): don't start a second one
        b.update(status="running", url=url, step=f"Berjalan di {url}")
    else:
        open_terminal(command, folder, b["name"])
        if url:
            deadline = time.time() + 90
            while time.time() < deadline and not _url_ok(url):
                time.sleep(2)
            if not _url_ok(url):
                b.update(status="failed", url=url, error="tidak merespons setelah dijalankan")
                _save_meta(b)
                _emit(b)
                _say(f"{b['name']} sudah {done_what}, {T}, tetapi belum merespons di "
                     f"{url.split('//')[-1]}. Silakan cek jendela terminalnya.{_rollback_hint(b)}", "warning")
                return
        b.update(status="running", url=url, step=f"Berjalan di {url}" if url else f"Berjalan: {command}")
    _save_meta(b)
    _emit(b)
    if url:
        webbrowser.open(url)
        _say(f"{b['name']} sudah {done_what} {detail} dan berjalan, {T}. Saya buka di browser.", "info")
        threading.Thread(target=_preview, args=(b,), daemon=True).start()
    else:
        _say(f"{b['name']} sudah {done_what} {detail} dan berjalan di jendela terminal, {T}.", "info")


def preview_path(folder: str | Path) -> Path:
    return Path(folder) / ".digidaw" / "preview.png"


def _preview(b: dict):
    """Screenshot the running page with a headless browser and show it on the HUD."""
    if not config.PROJECT_PREVIEW or not b.get("url"):
        return
    try:
        import media
        exe = media.browser_exe()
    except Exception:
        exe = None
    if not exe:
        return
    out = preview_path(b["folder"])
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="digidaw-preview-") as profile:
            subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
                            f"--user-data-dir={profile}", "--no-first-run", "--window-size=1280,800",
                            "--virtual-time-budget=6000", f"--screenshot={out}", b["url"]],
                           capture_output=True, timeout=45, creationflags=oscompat.NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return
    if out.exists() and out.stat().st_size > 0:
        b["preview"] = int(out.stat().st_mtime)
        _emit(b)


# ───────────────────────────── skills ─────────────────────────────

def create_project(description: str = "") -> str:
    """Start creating a NEW software project (website, app, game, tool...) by voice: Digidaw listens to
    the whole description, confirms it, then builds it with a coding agent (Antigravity by default,
    or Claude Code if the user says so) and reports when it runs. Use for 'buat project ...',
    'bikin aplikasi/website ...'. description = whatever the user already said about it."""
    if BRIEF_HOOK is None:
        return f"Pembuat project hanya tersedia saat Digidaw berjalan penuh, {T}."
    return BRIEF_HOOK(description)


def improve_project(request: str, project: str = "") -> str:
    """Change / add features to / fix a project Digidaw built earlier, e.g. 'tambahkan fitur riwayat ke
    kalkulator', 'ubah warnanya jadi biru', 'perbaiki error-nya'. The same coding agent continues its
    earlier conversation, then Digidaw restarts and re-checks the project. project = its name
    (empty = the most recent project)."""
    target = _find(project)
    if not target:
        return f"Saya tidak menemukan project{' ' + project if project else ''} yang pernah saya buat, {T}."
    agent = target.get("agent") or config.CODING_AGENT
    if not agent_exe(agent):
        return f"{AGENT_LABELS.get(agent, agent)} tidak ditemukan di komputer ini, {T}."
    folder = Path(target["folder"])
    name = target.get("name", folder.name)
    with _lock:
        if _busy(folder):
            return f"{name} masih sedang dikerjakan, {T}. Tunggu selesai dulu, lalu minta perubahannya lagi."
        _stop_server(folder)
        try:
            if not versions.load(folder):
                versions.snapshot(folder, "Sebelum perubahan pertama", kind="base")
            elif versions.is_modified(folder):
                versions.snapshot(folder, "Perubahan manual (di luar Digidaw)", kind="manual")
        except OSError as e:
            print(f"[versions] {e}")
        with open(folder / "BRIEF.md", "a", encoding="utf-8") as f:
            f.write(f"\n## Perubahan {datetime.datetime.now():%d-%m-%Y %H:%M}\n\n{request}\n")
        port = target.get("port")
        port_rule = (f"If it serves a web page it must keep listening on port {port}." if port else
                     "Keep the web server on the port it already uses.")
        job = _new_job(name=name, folder=str(folder), agent=agent, mode="change", request=request[:300],
                       created=target.get("created") or time.time(), summary=target.get("summary", ""), port=port,
                       conversation_id=target.get("conversation_id", ""), resume_id=target.get("conversation_id", ""),
                       prompt=CHANGE_PROMPT.format(name=name, request=request, port_rule=port_rule, run_rules=RUN_RULES))
        queued = _submit(job)
    if queued:
        return f"Baik, {T}. Perubahan untuk {name} masuk {queued}; saya kerjakan begitu ada slot kosong."
    return f"Baik, {T}. Saya minta {AGENT_LABELS[agent]} memperbarui {name}. Saya kabari kalau sudah berjalan lagi."


def _pick_job(project: str = "") -> dict | None:
    """The job the user means: by name, else the only / most recent unfinished one, else the last one."""
    jobs = list(_jobs.values())
    if not jobs:
        return None
    if project.strip():
        needle = project.lower().replace("project", "").replace("proyek", "").strip()
        for j in reversed(jobs):
            label = f"{j['name']} {Path(j['folder']).name}".lower()
            if needle and (needle in label or all(w in label for w in needle.split())):
                return j
        close = difflib.get_close_matches(needle, [j["name"].lower() for j in jobs], n=1, cutoff=0.5)
        return next((j for j in reversed(jobs) if close and j["name"].lower() == close[0]), None)
    pending = _pending()
    return pending[-1] if pending else jobs[-1]


def _describe_job(j: dict) -> str:
    elapsed = _minutes(time.time() - j["started"])
    labels = {"building": f"sedang dikerjakan ({elapsed}, {len(j.get('files', ()))} file), langkah terakhir: {j['step']}",
              "queued": f"menunggu di antrean nomor {_queue.index(j['id']) + 1 if j['id'] in _queue else '?'}",
              "starting": "sedang dijalankan dan dicek",
              "running": f"sudah berjalan{' di ' + j['url'].split('//')[-1] if j['url'] else ''}",
              "done": "sudah selesai", "failed": f"gagal: {j['error']}", "cancelled": "dibatalkan"}
    return f"{j['name']} {labels.get(j['status'], j['status'])}"


def project_build_status(project: str = "") -> str:
    """Report the progress of the projects being built / changed / queued (project = a name, empty = all)."""
    if project.strip():
        job = _pick_job(project)
        return f"{_describe_job(job)}, {T}." if job else f"Tidak ada pengerjaan project bernama {project}, {T}."
    pending = _pending()
    if not pending:
        return f"{_describe_job(list(_jobs.values())[-1])}, {T}." if _jobs else \
            f"Belum ada project yang sedang dikerjakan di sesi ini, {T}."
    return "; ".join(_describe_job(j) for j in pending) + f", {T}."


def cancel_project_build(project: str = "") -> str:
    """Stop the coding agent building / changing a project, or take it out of the queue
    (project = its name; empty = the most recent unfinished one)."""
    job = _pick_job(project)
    if not job or job["status"] not in ACTIVE + ("queued",):
        return f"Tidak ada project{' ' + project if project else ''} yang sedang dikerjakan, {T}."
    with _lock:
        was = job["status"]
        job["status"] = "cancelled"
        if job["id"] in _queue:
            _queue.remove(job["id"])
    if job.get("proc") and job["proc"].poll() is None:
        oscompat.kill_tree(job["proc"].pid)
    _save_meta(job)
    _emit(job)
    if was == "queued":
        _start_next()
        return f"{job['name']} dikeluarkan dari antrean, {T}."
    return f"Pengerjaan {job['name']} dibatalkan, {T}."


def open_last_project(project: str = "") -> str:
    """Open a project Digidaw built (default: the most recent) in the editor and run it, e.g. 'buka project
    kalkulator'. If it's already running it is just shown in the browser."""
    target = _find(project)
    if not target:
        return f"Saya tidak menemukan project{' ' + project if project else ''} yang pernah saya buat, {T}."
    folder = Path(target["folder"])
    name = target.get("name", folder.name)
    if shutil.which("code"):
        subprocess.Popen(["code", str(folder)], **oscompat.detached()) if not IS_WINDOWS else \
            subprocess.Popen(f'code "{folder}"', shell=True)
    else:
        oscompat.open_folder(folder)
    if _busy(folder):
        return f"Membuka {name}; project ini masih sedang dikerjakan agen, {T}."
    command, url = _run_info(folder)
    if url and _url_ok(url):
        webbrowser.open(url)
        return f"{name} sudah berjalan, {T}. Saya buka di browser dan editor."
    if command:
        open_terminal(command, folder, name)
        if url:
            threading.Thread(target=lambda: (_wait_url(url, 60) and webbrowser.open(url)), daemon=True).start()
        return f"Membuka dan menjalankan {name}, {T}."
    return f"Membuka project {name}, {T}."


def _wait_url(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _url_ok(url):
            return True
        time.sleep(2)
    return False


def stop_project(project: str = "") -> str:
    """Stop (close) a running project that Digidaw started, e.g. 'hentikan project kalkulator'."""
    target = _find(project)
    if target and _stop_server(Path(target["folder"])):
        return f"{target.get('name')} sudah dihentikan, {T}."
    if not project and _servers:
        for folder in list(_servers):
            _stop_server(Path(folder))
        return f"Semua project yang saya jalankan sudah dihentikan, {T}."
    return f"Tidak ada project yang sedang saya jalankan{' dengan nama ' + project if project else ''}, {T}."


def list_projects() -> str:
    """List the projects Digidaw has built (name, when, status)."""
    projects = all_projects()
    if not projects:
        return f"Belum ada project yang saya buat, {T}."
    parts = []
    for p in projects[:8]:
        when = datetime.datetime.fromtimestamp(p.get("created") or p.get("updated") or 0).strftime("%d/%m")
        running = " (berjalan)" if p["folder"] in _servers and _servers[p["folder"]].poll() is None else ""
        parts.append(f"{p.get('name', Path(p['folder']).name)} ({when}){running}")
    return "Project Anda: " + "; ".join(parts)


def project_history(project: str = "") -> str:
    """List the saved versions of a project Digidaw built ('riwayat versi project kalkulator')."""
    target = _find(project)
    if not target:
        return f"Saya tidak menemukan project{' ' + project if project else ''} yang pernah saya buat, {T}."
    saved = versions.load(target["folder"])
    if not saved:
        return f"{target.get('name')} belum punya riwayat versi, {T}. Versi disimpan otomatis setiap kali project dibuat atau diubah."
    items = [f"versi {v['number']} ({versions.when(v)}): {v['label'][:80].rstrip('.')}" for v in saved[-6:]]
    extra = " File saat ini sudah diubah sejak versi terakhir." if versions.is_modified(target["folder"]) else ""
    return f"Riwayat {target.get('name')}: " + "; ".join(reversed(items)) + "." + extra


def project_changes(project: str = "") -> str:
    """What the last change to a project did: which files changed and what the agent reported
    ('apa saja yang diubah di project kalkulator')."""
    target = _find(project)
    if not target:
        return f"Saya tidak menemukan project{' ' + project if project else ''} yang pernah saya buat, {T}."
    saved = versions.load(target["folder"])
    if len(saved) < 2:
        return f"{target.get('name')} belum pernah diubah sejak dibuat, {T}."
    new, old = saved[-1], saved[-2]
    text = f"Versi {new['number']} ({new['label'][:100]}): {versions.describe_diff(versions.diff(old, new))}."
    if new.get("summary"):
        text += f" Laporan agen: {new['summary'][-400:]}"
    return text


def restore_project_version(project: str = "", version: int = 0) -> str:
    """Undo changes to a project: restore its files to an earlier saved version and restart it
    ('kembalikan project kalkulator ke versi sebelumnya', 'kembalikan ke versi 2', 'batalkan perubahan
    terakhir'). version = the version number, 0 = the one before the current version."""
    target = _find(project)
    if not target:
        return f"Saya tidak menemukan project{' ' + project if project else ''} yang pernah saya buat, {T}."
    folder, name = target["folder"], target.get("name", Path(target["folder"]).name)
    if _busy(folder):
        return f"{name} sedang dikerjakan agen, {T}. Batalkan atau tunggu selesai dulu."
    saved = versions.load(folder)
    if not saved:
        return f"{name} belum punya riwayat versi, {T}."
    if version:
        chosen = next((v for v in saved if v["number"] == int(version)), None)
        if not chosen:
            return f"Versi {version} tidak ada. Versi yang tersedia: {', '.join(str(v['number']) for v in saved)}, {T}."
    else:
        modified = versions.is_modified(folder)
        if not modified and len(saved) < 2:
            return f"{name} baru punya satu versi, belum ada yang bisa dikembalikan, {T}."
        chosen = saved[-1] if modified else saved[-2]
    return safety.guard(f"mengembalikan {name} ke versi {chosen['number']} ({chosen['label'][:60]})",
                        _do_restore, folder=folder, name=name, number=chosen["number"])


def _do_restore(folder: str, name: str, number: int) -> str:
    was_running = _stop_server(Path(folder))
    versions.snapshot(folder, f"Sebelum dikembalikan ke versi {number}", kind="backup")
    chosen, deps = versions.restore(folder, number)
    versions.snapshot(folder, f"Dikembalikan ke versi {number}", kind="restore")
    with open(Path(folder) / "BRIEF.md", "a", encoding="utf-8") as f:
        f.write(f"\n## Dikembalikan ke versi {number} ({datetime.datetime.now():%d-%m-%Y %H:%M})\n\n{chosen['label']}\n")
    threading.Thread(target=_restart_after_restore, args=(Path(folder), name, deps, was_running), daemon=True).start()
    return (f"{name} sudah dikembalikan ke versi {number}, {T}. Versi sebelumnya tetap tersimpan, jadi bisa dibatalkan. "
            + ("Dependensinya berubah, saya install ulang lalu jalankan." if deps else "Saya jalankan ulang project-nya."))


def _restart_after_restore(folder: Path, name: str, deps: bool, was_running: bool):
    if deps and (folder / "package.json").exists() and shutil.which("npm"):
        try:
            subprocess.run("npm install", cwd=folder, shell=True, capture_output=True, timeout=600,
                           creationflags=oscompat.NO_WINDOW)
        except subprocess.TimeoutExpired:
            pass
    command, url = _run_info(folder)
    if not command:
        return
    open_terminal(command, folder, name)
    if url and _wait_url(url, 90):
        webbrowser.open(url)
        _say(f"{name} versi lama sudah berjalan lagi, {T}.", "info")
    elif url:
        _say(f"{name} sudah dikembalikan, tetapi belum merespons di {url.split('//')[-1]}, {T}.", "warning")


def snapshot() -> dict:
    return {"jobs": [_public(j) for j in _jobs.values()]}


AI_TOOLS = [create_project, improve_project, project_build_status, cancel_project_build,
            open_last_project, stop_project, list_projects, project_history, project_changes, restore_project_version]
