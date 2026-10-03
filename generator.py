"""
Generate images and files with the Antigravity CLI.

"Digidaw, buatkan gambar kucing astronot"         -> Antigravity's generate_image tool
"Digidaw, buatkan file Excel anggaran bulanan"     -> Antigravity writes the file (docx/xlsx/pptx/pdf/...)

Every job runs in its own scratch folder; when the agent is done, the finished files are moved to the
folder chosen in the Control Panel (GENERATE_DIR, default Documents\\Digidaw Generated), into
"Gambar" or "File", then opened and announced. Images that Antigravity keeps in its own conversation
folder (~/.gemini/antigravity-cli/brain/<id>/) are collected from there too.
"""
import os
import re
import json
import time
import shutil
import datetime
import threading
import subprocess
from pathlib import Path

import config
import events
import oscompat

T = config.USER_TITLE
ANNOUNCE = None                   # set by main (core.announce)
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
KIND_FOLDER = {"image": "Gambar", "file": "File"}
MAX_PARALLEL = 2
BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"

IMAGE_PROMPT = """Generate an image for the user with your generate_image tool.

Request (transcribed from speech, in Indonesian): {request}

- Write a rich, detailed English prompt for the image. {aspect}
- Use a short descriptive ImageName in English snake_case.
- After it is generated, copy the image file into the CURRENT working directory (keep its file name).
- {count}
- Do not create any other files. Do not ask questions; choose sensible defaults.
- Finish with ONE short sentence in Indonesian describing what you made."""

FILE_PROMPT = """Create the file the user asked for, in the CURRENT working directory.

Request (transcribed from speech, in Indonesian): {request}

- Produce a real, complete, well-formatted file in the right format: Word -> .docx (python-docx),
  Excel -> .xlsx (openpyxl, with formulas/formatting where useful), PowerPoint -> .pptx (python-pptx),
  PDF -> .pdf, plus .md/.txt/.csv/.html/code files as appropriate. Write the content in Indonesian unless
  asked otherwise, with real, useful content (not placeholders).
- Python 3.12 is installed; install missing packages with `pip install --user <package>`.
- Put any helper scripts or temporary files in a subfolder named `_work`; the current directory itself must
  contain ONLY the final file(s), with clear descriptive file names.
- If pictures would clearly improve the file, you may create them with generate_image and embed them.
- Do not ask questions; choose sensible defaults. Finish with ONE short sentence in Indonesian describing the result."""

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_counter = [0]


# ───────────────────────────── folders & index ─────────────────────────────

def output_root() -> Path:
    if config.GENERATE_DIR:
        root = Path(config.GENERATE_DIR)
    else:
        try:
            import system_control
            root = system_control._known_folder("documents") / "Digidaw Generated"
        except Exception:
            root = Path.home() / "Documents" / "Digidaw Generated"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path() -> Path:
    return output_root() / ".digidaw-index.json"


def results(limit: int = 30) -> list[dict]:
    """Recent generated files that still exist, newest first."""
    try:
        items = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        items = []
    return [i for i in items if os.path.exists(i["path"])][:limit]


def _remember(entries: list[dict]):
    items = entries + results(200)
    try:
        _index_path().write_text(json.dumps(items[:200], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def find_result(result_id: str) -> dict | None:
    return next((r for r in results(200) if r["id"] == result_id), None)


def counts() -> dict:
    root = output_root()
    out = {}
    for kind, sub in KIND_FOLDER.items():
        folder = root / sub
        out[kind] = sum(1 for f in folder.iterdir() if f.is_file()) if folder.exists() else 0
    return out


# ───────────────────────────── jobs ─────────────────────────────

def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("proc", "prompt", "files", "work")} | {"files": len(job["files"])}


def _emit(job: dict):
    events.emit("generate", **_public(job))


def _say(text: str, level: str = "info"):
    if ANNOUNCE:
        ANNOUNCE(text, level)
    else:
        print(f"[DIGIDAW] {text}")


def _submit(kind: str, request: str, prompt: str) -> str:
    import project_builder
    agent = "antigravity"
    if not project_builder.agent_exe(agent):
        if kind == "image":
            return f"Antigravity CLI tidak ditemukan, {T}. Pembuatan gambar membutuhkan Antigravity."
        agent = "claude" if project_builder.agent_exe("claude") else ""
        if not agent:
            return f"Antigravity CLI tidak ditemukan di komputer ini, {T}."
    with _lock:
        active = [j for j in _jobs.values() if j["status"] == "working"]
        if len(active) >= MAX_PARALLEL:
            return f"Masih ada {len(active)} pembuatan yang berjalan, {T}. Tunggu salah satunya selesai dulu."
        _counter[0] += 1
        job_id = f"g{int(time.time())}{_counter[0]}"
        work = output_root() / ".work" / job_id
        work.mkdir(parents=True, exist_ok=True)
        job = {"id": job_id, "kind": kind, "request": request, "status": "working", "started": time.time(),
               "step": "Menyiapkan Antigravity…", "steps": 0, "files": set(), "agent": agent, "prompt": prompt,
               "work": str(work), "conversation_id": "", "results": [], "error": "", "summary": ""}
        _jobs[job_id] = job
        for old in [j for j in _jobs.values() if j["status"] != "working"][:-8]:
            _jobs.pop(old["id"], None)
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"generate-{job_id}").start()
    _emit(job)
    return ""


def _run(job: dict):
    import project_builder
    work = Path(job["work"])
    deadline = time.time() + config.GENERATE_TIMEOUT_MINUTES * 60
    try:
        cmd = project_builder.agent_command(job["agent"], job["prompt"])
        with open(work.parent / f"{job['id']}.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    creationflags=oscompat.NO_WINDOW)
            job["proc"] = proc
            last = 0.0
            for line in proc.stdout:
                log.write(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    project_builder._handle_event(json.loads(line), job)
                except ValueError:
                    pass
                if time.time() > deadline:
                    proc.kill()
                    job["error"] = "melebihi batas waktu"
                    break
                if time.time() - last > 1.0:
                    last = time.time()
                    _emit(job)
            proc.wait()
    except Exception as e:
        job.update(status="failed", error=str(e))
        _emit(job)
        _say(f"Maaf {T}, pembuatan {_label(job)} gagal dimulai: {e}", "warning")
        return
    if job["status"] == "cancelled":
        shutil.rmtree(work, ignore_errors=True)
        return
    moved = _collect(job)
    shutil.rmtree(work, ignore_errors=True)
    if not moved:
        job.update(status="failed", error=job["error"] or (job.get("agent_summary") or "tidak ada file yang dihasilkan")[:200])
        _emit(job)
        _say(f"Maaf {T}, {_label(job)} tidak berhasil dibuat. {job['error']}", "warning")
        return
    job.update(status="done", results=moved, summary=(job.get("agent_summary") or "").strip()[:300],
               step=f"Selesai: {', '.join(r['name'] for r in moved[:3])}")
    _emit(job)
    if config.GENERATE_OPEN_RESULT:
        try:
            oscompat.open_path(moved[0]["path"])
        except OSError:
            pass
    took = int(time.time() - job["started"])
    names = ", ".join(r["name"] for r in moved[:3]) + (f" dan {len(moved) - 3} lainnya" if len(moved) > 3 else "")
    what = "Gambar" if job["kind"] == "image" else ("File" if len(moved) == 1 else f"{len(moved)} file")
    _say(f"{what} sudah jadi dalam {took} detik, {T}: {names}. Tersimpan di folder {KIND_FOLDER[job['kind']]}.", "info")


def _label(job: dict) -> str:
    return "gambar" if job["kind"] == "image" else "file"


def _unique(dest: Path) -> Path:
    n, candidate = 2, dest
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1
    return candidate


def _collect(job: dict) -> list[dict]:
    """Move the finished files out of the scratch folder (and generated images out of Antigravity's own
    folder) into the output folder. Returns index entries."""
    work = Path(job["work"])
    candidates = [p for p in work.iterdir() if not p.name.startswith((".", "_work", "__pycache__"))]
    if job["kind"] == "image":
        candidates = [p for p in candidates if p.suffix.lower() in IMAGE_EXT]
        seen = {p.stat().st_size for p in candidates}
        brain = BRAIN_DIR / job["conversation_id"] if job["conversation_id"] else None
        if brain and brain.is_dir():
            for img in brain.iterdir():
                if img.suffix.lower() in IMAGE_EXT and img.stat().st_mtime >= job["started"] - 5 \
                        and img.stat().st_size not in seen:
                    candidates.append(img)          # the agent didn't copy it: take it from its folder
                    seen.add(img.stat().st_size)
    target_dir = output_root() / KIND_FOLDER[job["kind"]]
    target_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in candidates:
        name = re.sub(r"_\d{10,}(?=\.\w+$)", "", src.name)       # drop Antigravity's timestamp suffix
        dest = _unique(target_dir / name)
        try:
            if src.is_dir():
                shutil.move(str(src), str(dest))
            elif BRAIN_DIR in src.parents:
                shutil.copy2(src, dest)
            else:
                shutil.move(str(src), str(dest))
        except OSError:
            continue
        moved.append({"id": f"{job['id']}-{len(moved)}", "name": dest.name, "path": str(dest), "kind": job["kind"],
                      "is_image": dest.suffix.lower() in IMAGE_EXT, "request": job["request"][:200],
                      "time": time.time(), "size": dest.stat().st_size if dest.is_file() else 0})
    _remember(moved)
    return moved


# ───────────────────────────── skills ─────────────────────────────

def generate_image(description: str, aspect_ratio: str = "", count: int = 1) -> str:
    """Create an image with AI (Antigravity) from a description: 'buatkan gambar kucing astronot',
    'bikin logo kedai kopi', 'generate wallpaper pemandangan gunung'. aspect_ratio e.g. '16:9', '1:1',
    '9:16' when the user says wallpaper/landscape/portrait/square; count = how many variations (max 4).
    The result is saved in the Control Panel's generate folder and opened."""
    count = max(1, min(int(count or 1), 4))
    aspect = f"Aspect ratio: {aspect_ratio}." if aspect_ratio else \
        "Pick the aspect ratio that suits the request (e.g. 16:9 for wallpapers/banners, 1:1 for logos/icons)."
    many = f"Generate {count} different variations." if count > 1 else "Generate exactly one image."
    error = _submit("image", description, IMAGE_PROMPT.format(request=description, aspect=aspect, count=many))
    return error or (f"Baik, {T}. Antigravity sedang membuat gambarnya, biasanya kurang dari satu menit. "
                     f"Saya buka begitu jadi.")


def create_file(description: str) -> str:
    """Create a document or file with Antigravity: Word/Excel/PowerPoint/PDF/CSV/text/code, e.g.
    'buatkan file Excel anggaran bulanan', 'bikin presentasi tentang AI 5 slide', 'buat surat lamaran
    kerja dalam Word', 'buat PDF jadwal belajar'. Not for software projects (use create_project) or
    quick notes (write_note). The file is saved in the Control Panel's generate folder and opened."""
    error = _submit("file", description, FILE_PROMPT.format(request=description))
    return error or (f"Baik, {T}. Antigravity sedang membuat file-nya, biasanya satu sampai tiga menit. "
                     f"Saya kabari begitu selesai.")


def generate_status() -> str:
    """Progress of images / files being generated."""
    working = [j for j in _jobs.values() if j["status"] == "working"]
    if not working:
        done = [j for j in _jobs.values() if j["status"] == "done"]
        return (f"Tidak ada yang sedang dibuat, {T}." + (f" Terakhir: {done[-1]['results'][0]['name']}." if done else ""))
    return "; ".join(f"{_label(j)} '{j['request'][:40]}' ({int(time.time() - j['started'])} detik, {j['step']})"
                     for j in working) + f", {T}."


def cancel_generate() -> str:
    """Stop the image / file that is being generated."""
    working = [j for j in _jobs.values() if j["status"] == "working"]
    if not working:
        return f"Tidak ada yang sedang dibuat, {T}."
    for job in working:
        job["status"] = "cancelled"
        if job.get("proc") and job["proc"].poll() is None:
            oscompat.kill_tree(job["proc"].pid)
        _emit(job)
    return f"Pembuatan dibatalkan, {T}."


def open_generated(which: str = "") -> str:
    """Open the generate folder, or the latest generated result ('buka gambar tadi', 'buka hasil generate')."""
    items = results(5)
    if which.lower().strip() in ("", "folder", "hasil", "semua") or not items:
        oscompat.open_folder(output_root())
        return f"Membuka folder hasil generate, {T}."
    oscompat.open_path(items[0]["path"])
    return f"Membuka {items[0]['name']}, {T}."


def snapshot() -> dict:
    return {"jobs": [_public(j) for j in _jobs.values()], "results": results(8), "root": str(output_root())}


AI_TOOLS = [generate_image, create_file, generate_status, cancel_generate, open_generated]
