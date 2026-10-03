"""
Understand the document the user has open (or names): "Digidaw, file apa ini?", "rangkum dokumen ini",
"siapa penulisnya", "di halaman berapa membahas X", "terjemahkan slide ini", "berapa total di kolom B".

Finding the open file — exact before guessed:
1. Word / Excel / PowerPoint: ask the running Office app (COM) for the document's full path; even an unsaved
   document can be read through it.
2. A PDF open in the browser: the file:/// address of the active tab.
3. Other viewers (Acrobat, Foxit, SumatraPDF, WPS, Notepad, VS Code...): the file in the program's command
   line, or Windows' "Recent files" entry with the name shown in the window title.
4. Only then a search by name in the usual folders.

Reading: PDF (text; scanned PDFs are read from page images), DOCX, XLSX/XLSM, PPTX, CSV, text & code, RTF and
images (read by the AI's vision). Extracted text is cached per file version, so follow-up questions are fast.
The spoken answer is kept short; the full answer is kept for "simpan hasil analisis".
"""
import os
import re
import json
import time
import zipfile
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import llm

T = config.USER_TITLE
TEXT_EXTS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".xml", ".html", ".htm", ".js", ".ts", ".tsx", ".jsx",
             ".css", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".bat", ".ps1", ".java", ".c", ".cpp", ".h", ".cs",
             ".php", ".go", ".rs", ".kt", ".sql", ".sh", ".tex", ".srt"}
DOC_EXTS = {".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".rtf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
SUPPORTED = TEXT_EXTS | DOC_EXTS | IMAGE_EXTS
KIND_NAMES = {".pdf": "dokumen PDF", ".docx": "dokumen Word", ".docm": "dokumen Word", ".xlsx": "spreadsheet Excel",
              ".xlsm": "spreadsheet Excel", ".pptx": "presentasi PowerPoint", ".csv": "tabel CSV", ".md": "dokumen Markdown",
              ".txt": "file teks", ".rtf": "dokumen RTF", ".json": "file JSON", ".py": "kode Python",
              ".html": "halaman HTML", ".js": "kode JavaScript", ".sql": "kode SQL"}
MAX_CHARS = 120_000                    # ~30k tokens: plenty for a summary, fine for the free tier
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_cache: dict[tuple, dict] = {}         # (path, mtime) -> extracted document
_last: dict = {}                       # last analysis: file, question, full answer


# ───────────────────────────── 1. which file is open? ─────────────────────────────

OFFICE = {"winword.exe": ("Word.Application", "ActiveDocument"), "excel.exe": ("Excel.Application", "ActiveWorkbook"),
          "powerpnt.exe": ("PowerPoint.Application", "ActivePresentation")}
TITLE_NOISE = re.compile(r"\s*(?:\[(?:read-only|hanya baca|compatibility mode|mode kompatibilitas|protected view|"
                         r"tampilan terproteksi)\]|\((?:read-only|hanya baca)\)|•.*$|\*|●)\s*", re.IGNORECASE)


def _title_name(title: str) -> str:
    """'laporan.pdf - Adobe Acrobat Reader (64-bit)' -> 'laporan.pdf'; 'Skripsi Bab 1 - Word' -> 'Skripsi Bab 1'."""
    parts = re.split(r"\s+[-–—]\s+", title)
    name = parts[0] if len(parts) > 1 else title
    return TITLE_NOISE.sub(" ", name).strip()


def _office_document(process: str, title: str) -> tuple[str | None, str | None]:
    """(path, text_if_unsaved) from a running Word / Excel / PowerPoint."""
    app_id, attr = OFFICE[process]
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        app = win32com.client.GetActiveObject(app_id)
        docs = {"ActiveDocument": "Documents", "ActiveWorkbook": "Workbooks", "ActivePresentation": "Presentations"}[attr]
        name = _title_name(title).lower()
        chosen = None
        for doc in getattr(app, docs):              # the one whose name is in the window title
            if doc.Name.lower() == name or os.path.splitext(doc.Name)[0].lower() == name:
                chosen = doc
                break
        chosen = chosen or getattr(app, attr)
        full = str(chosen.FullName)
        if os.path.isfile(full):
            return full, None
        if attr == "ActiveDocument":                # never saved: read it straight from Word
            return None, str(chosen.Content.Text)
        return None, None
    except Exception:
        return None, None


def _recent_xbel(name: str) -> str | None:
    """Linux: the freedesktop recently-used list (GTK, Qt, LibreOffice, PDF viewers write to it)."""
    xbel = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "recently-used.xbel"
    if not xbel.is_file() or not name:
        return None
    stem = name.lower()
    try:
        root = ET.parse(xbel).getroot()
    except (ET.ParseError, OSError):
        return None
    hits = []
    for bookmark in root.iter("bookmark"):
        href = bookmark.get("href", "")
        if not href.startswith("file://"):
            continue
        path = urllib.parse.unquote(urllib.parse.urlparse(href).path)
        base = os.path.basename(path).lower()
        if (base == stem or os.path.splitext(base)[0] == stem) and os.path.isfile(path):
            hits.append((bookmark.get("visited") or bookmark.get("modified") or "", path))
    return max(hits)[1] if hits else None


def _recent_items(name: str) -> str | None:
    """Windows keeps a shortcut to every recently opened file in Recent; resolve the one named like the window."""
    if os.name != "nt":
        return _recent_xbel(name)
    recent = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
    if not recent.is_dir() or not name:
        return None
    stem = name.lower()
    links = sorted(recent.glob("*.lnk"), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
    candidates = [l for l in links if l.stem.lower() == stem or os.path.splitext(l.stem)[0].lower() == stem]
    if not candidates:
        return None
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        for link in candidates:
            target = shell.CreateShortcut(str(link)).TargetPath
            if target and os.path.isfile(target):
                return target
    except Exception:
        pass
    return None


def _from_command_line(pid: int, name: str) -> str | None:
    try:
        import psutil
        args = psutil.Process(pid).cmdline()[1:]
    except Exception:
        return None
    files = [a.strip('"') for a in args if os.path.isfile(a.strip('"'))]
    if not files:
        return None
    named = [f for f in files if name and name.lower() in os.path.basename(f).lower()]
    return (named or files)[-1]


def locate_open_file() -> dict:
    """{'path', 'text' (unsaved Office doc), 'window', 'how'} for the file in the window the user is looking at."""
    import screen_context
    w = screen_context.current()
    if not w:
        return {}
    process, title = (w.get("process") or "").lower(), w.get("title") or ""
    name = _title_name(title)
    if process in OFFICE:
        path, text = _office_document(process, title)
        if path or text:
            return {"path": path, "text": text, "name": name, "window": w, "how": "office"}
    if screen_context.is_browser(w):
        url = screen_context.current_url(w)
        if url.lower().startswith("file:"):
            path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
            if os.name == "nt":
                path = path.lstrip("/")             # file:///C:/x.pdf -> C:/x.pdf (Linux keeps its leading /)
            if os.path.isfile(path):
                return {"path": path, "window": w, "how": "browser"}
        if re.search(r"\.pdf(?:$|[?#])", url, re.I):
            return {"url": url, "name": name, "window": w, "how": "browser-url"}
        return {"browser_page": True, "window": w}
    m = re.match(r"^((?:[A-Za-z]:\\|\\\\|/|~/).+?)(?:\s+[-–—]\s+|$)", title)      # full path in the title
    if m and m.group(1).startswith("~/"):
        m = re.match(r"^(.*)$", os.path.expanduser(m.group(1)))
    if m and os.path.isfile(m.group(1).strip()):
        return {"path": m.group(1).strip(), "window": w, "how": "title"}
    path = _from_command_line(w["pid"], name) or _recent_items(name)
    if path:
        return {"path": path, "window": w, "how": "recent"}
    if Path(name).suffix.lower() in SUPPORTED:
        found = search_file(name, timeout=4)
        if found:
            return {"path": found, "window": w, "how": "search"}
    return {"name": name, "window": w}


def search_file(name: str, timeout: float = 5.0) -> str | None:
    """Find a file by name in Recent, Documents, Downloads, Desktop, the project folder and home."""
    import system_control as sc
    name = name.strip().strip('"')
    if os.path.isfile(name):
        return name
    hit = _recent_items(name) or _recent_items(os.path.splitext(name)[0])
    if hit:
        return hit
    target = name.lower()
    has_ext = bool(Path(target).suffix)
    roots = []
    for folder in ("documents", "downloads", "desktop"):
        try:
            roots.append(sc._known_folder(folder))
        except Exception:
            pass
    if config.PROJECT_DIR:
        roots.append(Path(config.PROJECT_DIR))
    roots.append(Path.home())
    skip = {"appdata", "node_modules", ".git", "$recycle.bin", "windows", ".venv", "venv", "__pycache__", ".cache"}
    deadline = time.time() + timeout
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
            for f in filenames:
                low = f.lower()
                if low == target or (not has_ext and os.path.splitext(low)[0] == target and Path(low).suffix in SUPPORTED):
                    return os.path.join(dirpath, f)
            if time.time() > deadline:
                return None
    return None


# ───────────────────────────── 2. read it ─────────────────────────────

def _docx(path: str) -> dict:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        meta = {}
        if "docProps/core.xml" in z.namelist():
            core = ET.fromstring(z.read("docProps/core.xml"))
            for el in core:
                tag = el.tag.split("}")[-1]
                if tag in ("title", "creator", "lastModifiedBy", "created", "modified") and el.text:
                    meta[tag] = el.text
        if "docProps/app.xml" in z.namelist():
            app = ET.fromstring(z.read("docProps/app.xml"))
            for el in app:
                tag = el.tag.split("}")[-1]
                if tag in ("Pages", "Words") and el.text:
                    meta[tag.lower()] = el.text
    lines = []
    for p in root.iter(f"{_W}p"):
        text = "".join(t.text for t in p.iter(f"{_W}t") if t.text)
        if text.strip():
            style = p.find(f"{_W}pPr/{_W}pStyle")
            heading = style is not None and "heading" in (style.get(f"{_W}val") or "").lower()
            lines.append(("## " if heading else "") + text)
    return {"text": "\n".join(lines), "meta": meta}


def _pdf(path: str) -> dict:
    import pymupdf
    doc = pymupdf.open(path)
    pages, total = [], 0
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        pages.append(f"[Halaman {i + 1}]\n{text}")
        total += len(text)
        if total > MAX_CHARS:
            break
    meta = {k: v for k, v in (doc.metadata or {}).items() if v and k in ("title", "author", "subject", "creationDate")}
    meta["pages"] = str(doc.page_count)
    images = []
    if total < 40 * max(1, len(pages)):                     # (almost) no text layer: a scanned PDF
        for page in list(doc)[:4]:
            images.append(page.get_pixmap(dpi=110).tobytes("png"))
    return {"text": "\n\n".join(pages), "meta": meta, "images": images}


def _xlsx(path: str) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts, total = [], 0
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                rows.append(" | ".join("" if v is None else str(v) for v in row))
            if len(rows) >= 400:
                rows.append("… (baris berikutnya dipotong)")
                break
        block = f"[Sheet: {ws.title} — {ws.max_row} baris × {ws.max_column} kolom]\n" + "\n".join(rows)
        parts.append(block)
        total += len(block)
        if total > MAX_CHARS:
            break
    return {"text": "\n\n".join(parts), "meta": {"sheets": ", ".join(ws.title for ws in wb.worksheets)}}


def _pptx(path: str) -> dict:
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide and slide.notes_slide.notes_text_frame else ""
        parts.append(f"[Slide {i}]\n" + "\n".join(texts) + (f"\n(Catatan: {notes})" if notes.strip() else ""))
    return {"text": "\n\n".join(parts), "meta": {"slides": str(len(prs.slides))}}


def _rtf(path: str) -> dict:
    raw = Path(path).read_text(encoding="latin-1", errors="ignore")
    text = re.sub(r"\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]", " ", raw)
    return {"text": re.sub(r"\s+", " ", text), "meta": {}}


def read_document(path: str) -> dict:
    """{'text', 'meta', 'images'} for a file (cached per version)."""
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key in _cache:
        return _cache[key]
    ext = Path(path).suffix.lower()
    if ext in (".docx", ".docm"):
        doc = _docx(path)
    elif ext == ".pdf":
        doc = _pdf(path)
    elif ext in (".xlsx", ".xlsm"):
        doc = _xlsx(path)
    elif ext == ".pptx":
        doc = _pptx(path)
    elif ext == ".rtf":
        doc = _rtf(path)
    elif ext in IMAGE_EXTS:
        doc = {"text": "", "meta": {}, "images": [Path(path).read_bytes()]}
    else:
        doc = {"text": Path(path).read_text(encoding="utf-8", errors="ignore"), "meta": {}}
    doc.setdefault("images", [])
    doc["meta"].update(size=st.st_size, modified=datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d-%m-%Y %H:%M"))
    _cache.clear() if len(_cache) > 8 else None
    _cache[key] = doc
    return doc


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n > 1_048_576 else f"{max(1, n // 1024)} KB"


# ───────────────────────────── 3. answer ─────────────────────────────

INFO_RE = re.compile(r"^(?:ini )?(?:file|dokumen|berkas)(?: apa)?(?: ini)?$|^(?:file|dokumen|berkas) apa(?: ini)?$|"
                     r"^ini (?:file|dokumen|berkas) apa$|^apa (?:ini|file ini|dokumen ini)$|^info file(?: ini)?$", re.I)
DETAIL_RE = re.compile(r"\b(?:detail|lengkap|rinci|semua poin|per bab|per halaman|per slide|panjang)\b", re.I)


def _ask(question: str, label: str, kind: str, doc: dict, info_mode: bool) -> tuple[str, str]:
    """(spoken answer, full answer)."""
    from google.genai import types
    meta = ", ".join(f"{k}: {v}" for k, v in doc["meta"].items() if k not in ("size",))
    text = doc["text"][:MAX_CHARS]
    cut = len(doc["text"]) > MAX_CHARS
    task = ("Jelaskan file apa ini: jenisnya, topik/isinya secara umum, dan untuk apa kemungkinan dipakai (maks 3 kalimat)."
            if info_mode else question)
    prompt = (f"Dokumen: '{label}' ({kind}). Metadata: {meta or '-'}.\n"
              + (f"=== ISI DOKUMEN{' (sebagian, dokumen sangat panjang)' if cut else ''} ===\n{text}\n=== AKHIR ===\n" if text.strip() else
                 "Isi dokumen ada pada gambar terlampir.\n")
              + f"\nPermintaan user (dari suara): {task}\n\n"
              "Jawab berdasarkan dokumen saja; jangan mengarang. Format jawaban PERSIS seperti ini:\n"
              "LISAN: <jawaban untuk dibacakan: bahasa Indonesia natural, tanpa markdown/bullet/simbol, "
              f"{'maksimal 150 kata' if DETAIL_RE.search(question) else 'maksimal 70 kata'}>\n"
              "LENGKAP: <jawaban lengkap dalam markdown (boleh poin-poin, sebut halaman/slide/sheet bila relevan)>")
    contents = [prompt] + [types.Part.from_bytes(data=img, mime_type="image/png" if img[:4] == b"\x89PNG" else "image/jpeg")
                           for img in doc.get("images", [])[:4]]
    raw = llm.generate(contents, max_tokens=1800)
    spoken = re.search(r"LISAN:\s*(.+?)(?:\n\s*LENGKAP:|$)", raw, re.S)
    full = re.search(r"LENGKAP:\s*(.+)$", raw, re.S)
    spoken_text = (spoken.group(1) if spoken else raw).strip()
    return re.sub(r"[*#_`>]+", "", spoken_text), (full.group(1).strip() if full else raw.strip())


def analisis_dokumen(pertanyaan: str = "", path: str = "") -> str:
    """Understand a document: the one open in the active window (path empty) or a named / full-path file.
    'file apa ini', 'rangkum dokumen ini', 'apa kesimpulan PDF ini', 'siapa penulisnya', 'cari bagian tentang X',
    'terjemahkan slide ini', 'berapa total di sheet penjualan', 'rangkum file laporan.pdf'.
    pertanyaan = what the user wants (empty / 'file apa ini' = what is this file). Supports PDF (incl. scanned),
    Word, Excel, PowerPoint, CSV, text/code and images."""
    question = (pertanyaan or "").strip()
    info_mode = not question or bool(INFO_RE.match(question.strip(" ?.!").lower()))
    found = {}
    if path:
        resolved = path if os.path.isfile(path) else search_file(path)
        if not resolved:
            return f"Saya tidak menemukan file '{path}', {T}. Coba sebutkan nama lengkapnya, atau buka file-nya dulu."
        found = {"path": resolved, "how": "named"}
    else:
        found = locate_open_file()
        if found.get("browser_page"):
            import screen_context
            return screen_context.summarize_current_page(question)
    if found.get("url"):                                         # an online PDF in the browser
        import tempfile
        import requests
        try:
            data = requests.get(found["url"], timeout=20).content
            tmp = Path(tempfile.gettempdir()) / f"digidaw_{abs(hash(found['url']))}.pdf"
            tmp.write_bytes(data)
            found = {"path": str(tmp), "label": found.get("name") or "PDF dari browser", "how": "download"}
        except Exception as e:
            return f"PDF di browser tidak bisa saya unduh untuk dibaca, {T}: {e}"
    if not found.get("path") and not found.get("text"):
        what = found.get("name")
        if what:
            return (f"Saya melihat jendela '{what}', tetapi tidak bisa menemukan file-nya, {T}. "
                    f"Sebutkan nama file-nya, misalnya: rangkum file laporan.pdf.")
        return f"Saya tidak melihat dokumen yang sedang dibuka, {T}. Buka file-nya dulu, atau sebutkan namanya."

    if found.get("path"):
        file_path = found["path"]
        label = found.get("label") or os.path.basename(file_path)
        ext = Path(file_path).suffix.lower()
        if ext not in SUPPORTED:
            return (f"{label} adalah file {ext or 'tanpa ekstensi'} yang belum bisa saya baca, {T}. Yang bisa: PDF, Word, Excel, "
                    f"PowerPoint, CSV, teks, kode, dan gambar.")
        try:
            doc = read_document(file_path)
        except Exception as e:
            return f"Gagal membaca {label}, {T}: {e}"
        kind = KIND_NAMES.get(ext, "gambar" if ext in IMAGE_EXTS else f"file {ext}")
    else:                                                        # unsaved Word document
        file_path, label, kind = "", found.get("name") or "dokumen Word (belum disimpan)", "dokumen Word yang belum disimpan"
        doc = {"text": found["text"], "meta": {}, "images": []}

    if not doc["text"].strip() and not doc.get("images"):
        return f"{label} kosong atau isinya tidak bisa dibaca sebagai teks, {T}."
    try:
        spoken, full = _ask(question or "file apa ini", label, kind, doc, info_mode)
    except Exception as e:
        return f"Maaf {T}, analisis {label} gagal: {e}"
    _last.update(file=file_path, label=label, question=question or "file apa ini", full=full, when=time.time())

    if info_mode:
        meta = doc["meta"]
        facts = [kind]
        if meta.get("pages"):
            facts.append(f"{meta['pages']} halaman")
        if meta.get("slides"):
            facts.append(f"{meta['slides']} slide")
        if meta.get("sheets"):
            facts.append(f"sheet {meta['sheets']}")
        if meta.get("size"):
            facts.append(_size(int(meta["size"])))
        author = meta.get("author") or meta.get("creator")
        head = f"Ini {label}, {', '.join(facts)}" + (f", dibuat oleh {author}" if author else "") + "."
        return f"{head} {spoken}"
    return spoken


def simpan_hasil_analisis() -> str:
    """Save the full result of the last document analysis as a note (Markdown) and open it
    ('simpan hasil analisis', 'simpan rangkuman tadi')."""
    if not _last:
        return f"Belum ada hasil analisis dokumen yang bisa disimpan, {T}."
    import system_control as sc
    safe = re.sub(r'[\\/:*?"<>|]+', "", _last["label"])[:60]
    path = sc._notes_dir() / f"Analisis {safe} {datetime.datetime.now():%Y-%m-%d %H%M}.md"
    path.write_text(f"# Analisis: {_last['label']}\n\n**Permintaan:** {_last['question']}\n"
                    + (f"**File:** {_last['file']}\n" if _last.get("file") else "")
                    + f"\n{_last['full']}\n", encoding="utf-8")
    sc._open_note(path)
    return f"Hasil analisis {_last['label']} sudah saya simpan di catatan dan saya buka, {T}."


AI_TOOLS = [analisis_dokumen, simpan_hasil_analisis]
