"""
What the user teaches Digidaw by talking to it, without any AI:

- routines: "buat rutinitas kuliah", then the steps one at a time ("buka Firefox", "lampu putih", "tunggu 3 detik"), then "selesai".
  Later "mode kuliah" runs them. Each step is kept as the words that were said (protocols.py runs them as if said again), so what
  is open at that moment counts, not what was open while teaching. Only words Digidaw understands by itself are accepted: a
  routine must do the same thing every time.
- words: "kalau aku bilang ruang dua maksudnya pindah ke workspace 2". From then on the one means the other (habits.py aliases).
"""
import re
import time

import config
import events
import difflib

import habits
import protocols
import safety

T = config.USER_TITLE
TEACH_SECONDS = 120               # no step for this long: the routine being taught is dropped
LISTEN_SECONDS = 25               # how long Digidaw keeps listening for the next step without being called
MAX_STEPS = 20
_WORD = r"(?:rutinitas|protokol|protocol|mode)"
START_RE = re.compile(rf"(?:tolong |coba )?(?:buat|buatkan|bikin|bikinkan|tambah|tambahkan|ajari|ajarkan|rekam)(?: saya| aku)? (?P<word>{_WORD})"
                      r"(?: baru)?(?: (?:namanya|bernama|nama))?(?: (?P<rest>.+))?")
RUN_RE = re.compile(r"(?:tolong |coba )?(?:(?:jalankan|aktifkan|mulai|pakai|nyalakan|masuk(?: ke)?) )?(?P<word>rutinitas|protokol|protocol|mode) (?P<name>.{2,40})|"
                    r"(?P<name2>.{2,30}) (?:protocol|protokol)")
DELETE_RE = re.compile(rf"(?:tolong |coba )?(?:hapus|buang|batalkan) {_WORD} (?P<name>.{{2,40}})")
LIST_RE = re.compile(rf"(?:apa saja|apa aja|daftar|lihat|sebutkan|cek)(?: semua)? {_WORD}(?: (?:saya|aku|ku))?(?: apa saja| apa aja)?|{_WORD}(?: saya| aku|ku)? apa saja")
SHOW_RE = re.compile(rf"(?:apa )?isi {_WORD} (?P<name>.{{2,40}})|{_WORD} (?P<name2>.{{2,40}}) isinya apa")
DONE_RE = re.compile(r"(?:oke |ok |ya )?(?:selesai|sudah|udah|cukup|simpan|itu saja|itu aja|segitu(?: saja| aja)?)(?: (?:saja|aja|dong|deh|ya|itu))?")
CANCEL_RE = re.compile(r"(?:batal|batalkan|tidak jadi|nggak jadi|gak jadi|enggak jadi|ga jadi)(?: (?:saja|aja|dulu|deh))?")
UNDO_RE = re.compile(r"(?:hapus|batalkan|ulangi|buang)(?: (?:langkah|yang))?(?: (?:terakhir|tadi))|yang tadi salah|salah")
SPLIT_RE = re.compile(r"\s*(?:,|;|\blalu\b|\bterus\b|\bkemudian\b|\bhabis itu\b|\bsetelah itu\b|\bsesudah itu\b)\s*", re.IGNORECASE)
STEP_START_RE = re.compile(r"\b(?:buka|tutup|pindah|pindahkan|putar|ubah|ganti|matikan|nyalakan|hidupkan|ketik|cari|setel|atur|jalankan|"
                           r"lampu|volume|kecerahan|kipas|timer|alarm|tunggu)\b")
OPENING_RE = re.compile(r"(?P<verb>(?:buka|bukakan|jalankan|open)\s+)?(?:(?P<kind>situs|website|web|laman|halaman|aplikasi)\s+)?(?P<names>.+)")
NAMES_SPLIT_RE = re.compile(r"\s*(?:,|\bdan\b|\bsama\b)\s*")
WAIT_RE = re.compile(r"tunggu (\d{1,2})(?: detik)?")
TAUGHT_RE = re.compile(r"(?:tolong |coba )?(?:ajari |ingat |catat )?(?:kalau|kalo|jika|bila) (?:aku|saya|gue|gua) (?:bilang|ngomong|ucap|ucapkan|sebut)\s+"
                       r"(?P<heard>.{2,40}?)\s+(?:maksudnya|maksud(?:ku| saya| aku)|artinya|berarti|itu berarti|itu maksudnya|itu artinya|"
                       r"sama dengan|jadi|yaitu)\s+(?P<meant>.{2,80})")
FORGET_RE = re.compile(r"(?:tolong |coba )?lupakan (?:kata|arti|ucapan|koreksi) (?P<heard>.{2,40})")
# these words keep their meaning: teaching them something else would break the commands themselves
PROTECTED_WORDS = {"ya", "iya", "tidak", "batal", "stop", "sudah", "selesai", "buka", "tutup", "pindah", "putar", "digidaw", "jam",
                   "lampu", "volume", "matikan", "nyalakan", "hapus", "mode", "rutinitas"}
# a routine doesn't send a message for the user, start another routine, or click around by itself
NOT_IN_ROUTINE = {"run_protocol", "create_protocol", "delete_protocol", "do_on_screen", "click_on_screen", "reply_message", "_repeat", "_ask"}

_teaching: dict = {}              # name, steps (the words of each), at


def teaching() -> bool:
    if _teaching and time.time() - _teaching["at"] > TEACH_SECONDS:
        _teaching.clear()
    return bool(_teaching)


def _low(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip(" .,!?")).lower()


def _steps_said(steps: list[dict]) -> list[str]:
    return [s["say"] for s in steps]


def _target(name: str, site_first: bool) -> tuple[str, str] | None:
    """What a name is: ("known", the name Digidaw knows it by) or ("address", its URL). None: not found."""
    import resolver
    import site_finder
    import system_control as sc
    if site_finder.is_address(name):
        return "address", site_finder.find(name)
    key = sc._match_key(name, sc.WEBSITES)
    if key:
        return "known", key
    if not site_first:
        verdict, matches = resolver.resolve(name, ("app", "site"), sure_only=True)
        if verdict == "sure":
            return "known", matches[0].key
    found = site_finder.find(name, online=site_first)      # ("buka situs X": certainly a site)
    return ("address", found) if found else None


def parse_step(phrase: str) -> tuple[list[dict], str]:
    """The steps in one phrase, and what wasn't understood ("" when all was). "buka whatsapp di workspace 1", "firefox di workspace
    2", "buka situs medanpedia", "buka situs medanpedia dan uneedpanel di workspace 3", "volume 30"."""
    import brain
    import site_finder
    text = re.sub(r"\s+titik\s+", ".", phrase.strip(" .,"), flags=re.IGNORECASE)
    text = re.sub(r"\s+dan$", "", text, flags=re.IGNORECASE)
    low = brain.WS_GLUED.sub(r"\1 ps \2", _low(text))
    tail = re.search(rf"\s+(?:(?:di|ke|pada)\s+)?{brain.WORKSPACE}\s+({brain.WS_NUM})$", low)
    body = low[:tail.start()] if tail else low
    opening = OPENING_RE.fullmatch(body)
    workspace = brain._ws(tail[1]) if tail else ""
    explicit = bool(opening and opening["kind"] and opening["kind"] != "aplikasi")
    route = brain._route(habits.apply_aliases(low))
    if tail and route and route[0] != "open_on_workspace":      # "pindah ke workspace 2", "pindahkan firefox ke workspace 3"
        workspace = ""
    if not workspace and not explicit and not site_finder.is_address(body if not opening else opening["names"]) and understood(text):
        return [{"say": text}], ""
    if not opening:
        return [], text
    if not workspace and not explicit and not opening["verb"]:
        return [], text                  # a word with no "buka" isn't asked to open anything
    steps = []
    for name in NAMES_SPLIT_RE.split(opening["names"]):
        name = OPENING_RE.fullmatch(name.strip())["names"].strip() if OPENING_RE.fullmatch(name.strip()) else name.strip()
        found = _target(name, site_first=explicit)
        if not found:
            return steps, (f"buka situs {name}" if explicit else text)
        kind, value = found
        where = f" di workspace {workspace}" if workspace else ""
        shown = site_finder.host_of(value) if kind == "address" else name
        if workspace:
            steps.append({"say": f"buka {shown}{where}", "tool": "open_on_workspace", "args": {"name": value, "workspace": workspace}})
        elif kind == "address":
            steps.append({"say": f"buka {shown}", "tool": "open_website", "args": {"site": value}})
        else:
            steps.append({"say": f"buka {name}"})
    return steps, ""


def understood(phrase: str) -> bool:
    """Does Digidaw know by itself what this means, the same way each time? (No AI, nothing is run.)"""
    import brain
    import resolver
    fixed = _low(habits.apply_aliases(phrase))
    if WAIT_RE.fullmatch(fixed):
        return True
    if brain.ALARM_WORDS.search(fixed) and re.search(r"\d", fixed):
        return True
    route = brain._route(fixed)
    if route:
        return route[0] not in NOT_IN_ROUTINE
    closing = re.fullmatch(r"(?:tutup|close) (.+)", fixed)     # not open while teaching, it may be when the routine runs
    return bool(closing) and resolver.resolve(closing[1], ("app", "site"), sure_only=True)[0] == "sure"


def _split(text: str) -> list[str]:
    import brain
    if len(re.findall(rf"\b{brain.WORKSPACE}\s+{brain.WS_NUM}\b", text.lower())) >= 2:     # "whatsapp di workspace 1 dan firefox di workspace 2"
        text = re.sub(r"\s+dan\s+", ", ", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s+dan\s+(?={STEP_START_RE.pattern})", ", ", text, flags=re.IGNORECASE)
    return [part.strip(" .,") for part in SPLIT_RE.split(text) if part.strip(" .,")]


def _take(phrases: list[str]) -> tuple[list[dict], str]:
    """All the steps of these phrases, and the first one not understood."""
    steps = []
    for phrase in phrases:
        found, missed = parse_step(phrase)
        steps += found
        if missed:
            return steps, missed
    return steps, ""


def _clean_name(words: str) -> str:
    name = re.sub(rf"^(?:{_WORD}|baru|namanya|bernama|nama)\s+", "", _low(words))
    return re.sub(rf"^(?:{_WORD}|baru|namanya|bernama|nama)\s+", "", name)[:30].strip()


def _name_problem(name: str) -> str:
    """Another command already answers to "mode <name>" ("mode hemat" is the power mode)."""
    import brain
    if len(name) < 2 or len(name.split()) > 3:
        return f"Namanya pendek saja, satu atau dua kata, {T}. Namanya apa?"
    route = brain._route(f"mode {name}")
    if route and route[0] != "run_protocol":
        return f"Mode {name} sudah dipakai untuk hal lain, {T}. Pilih nama lain, namanya apa?"
    return ""


def save(name: str, steps: list) -> str:
    key = _clean_name(name)
    steps = [{"say": step} if isinstance(step, str) else step for step in steps]
    all_routines = protocols.load()
    all_routines[key] = {"aliases": [f"mode {key}", f"rutinitas {key}"], "description": ", ".join(_steps_said(steps)), "custom": True,
                         "lamp_added": True, "steps": steps}
    protocols.save(all_routines)
    events.emit("routines", items=protocols.for_hud())
    return key


def _start(rest: str | None) -> str:
    _teaching.clear()
    _teaching.update(name="", steps=[], at=time.time())
    rest = (rest or "").strip()
    if not rest:
        return f"Nama rutinitasnya apa, {T}?"
    begin = STEP_START_RE.search(rest)
    name = _clean_name(rest[:begin.start()] if begin else rest)
    if not name:
        return f"Nama rutinitasnya apa, {T}?"
    problem = _name_problem(name)
    if problem:
        return problem
    _teaching["name"] = name
    said = _split(rest[begin.start():]) if begin else []
    good, missed = _take(said)
    _teaching["steps"] = good[:MAX_STEPS]
    if said and not missed:
        return _finish()
    if said:
        return _not_understood(missed)
    replaced = " Langkah lamanya saya ganti kalau sudah selesai." if name in protocols.load() else ""
    return f"Rutinitas {name}. Langkah pertamanya apa, {T}? Sebutkan satu per satu, lalu bilang selesai.{replaced}"


def _not_understood(phrase: str) -> str:
    if re.match(r"buka situs ", phrase):
        return f"Alamat {phrase.removeprefix('buka situs ')} belum ketemu, {T}. Sebutkan alamatnya, misalnya contoh titik com. Lalu?"
    return f"'{phrase}' belum saya pahami, {T}. Sebutkan dengan kata lain, atau bilang selesai. Lalu?"


def _finish() -> str:
    name, steps = _teaching["name"], list(_teaching["steps"])
    _teaching.clear()
    key = save(name, steps)
    return f"Rutinitas {key} tersimpan, {len(steps)} langkah: {', '.join(_steps_said(steps))}. Ucapkan mode {key} untuk menjalankannya, {T}."


def _teach_step(text: str, low: str) -> str:
    _teaching["at"] = time.time()
    if CANCEL_RE.fullmatch(low):
        name = _teaching.get("name")
        _teaching.clear()
        return f"Baik, {T}, rutinitas {name} tidak jadi dibuat." if name else f"Baik, {T}, tidak jadi."
    if not _teaching["name"]:
        name = _clean_name(low)
        problem = _name_problem(name)
        if problem:
            return problem
        _teaching["name"] = name
        return f"Rutinitas {name}. Langkah pertamanya apa, {T}? Sebutkan satu per satu, lalu bilang selesai."
    if DONE_RE.fullmatch(low):
        if not _teaching["steps"]:
            return f"Belum ada langkahnya, {T}. Langkah pertamanya apa?"
        return _finish()
    if UNDO_RE.fullmatch(low):
        if not _teaching["steps"]:
            return f"Belum ada langkah, {T}. Langkah pertamanya apa?"
        gone = _teaching["steps"].pop()["say"]
        return f"{gone[:1].upper()}{gone[1:]} saya hapus. Lalu?"
    good, missed = _take(_split(text))
    _teaching["steps"] = (_teaching["steps"] + good)[:MAX_STEPS]
    if missed:
        taken = ", ".join(_steps_said(good))
        return f"{taken[:1].upper()}{taken[1:]}. {_not_understood(missed)}" if taken else _not_understood(missed)
    if len(_teaching["steps"]) >= MAX_STEPS:
        return _finish()
    heard = ", ".join(_steps_said(good))
    return f"{heard[:1].upper()}{heard[1:]}. Lalu?"


def _named(name: str, explicit: bool) -> str | None:
    """The routine with exactly this name. With "mode" only its own name counts: "mode malam" isn't the built-in routine "tidur"
    whose alias is "malam", it may well be the lamp. With "rutinitas" or "protokol" the aliases count too ("house party protocol")."""
    all_routines = protocols.load()
    name = _clean_name(name)
    for key, routine in all_routines.items():
        if name == key or (explicit and name in [a.lower() for a in routine.get("aliases", [])]):
            return key
    # a name cut off or a letter off ("mode ga", "mode gabot"), when only one routine can be meant
    starts = [key for key in all_routines if len(name) >= 2 and key.startswith(name)]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(name, list(all_routines), n=2, cutoff=0.8)
    return close[0] if len(close) == 1 else None


def _run(key: str) -> str:
    import brain
    return str(brain.TOOL_MAP["run_protocol"](name=key))


def teach_word(heard: str, meant: str) -> str:
    """"kalau aku bilang X maksudnya Y"."""
    import resolver
    heard, meant = _low(heard), _low(meant)
    if heard in PROTECTED_WORDS or len(heard.split()) > 5:
        return f"'{heard}' sudah dipakai untuk perintah, {T}, jadi tidak saya ubah."
    known = understood(meant) or RUN_RE.fullmatch(meant) is not None and _named(RUN_RE.fullmatch(meant)["name"] or "", True) is not None \
        or resolver.resolve(meant, ("app", "site"), sure_only=True)[0] == "sure"
    if not known:
        return f"Saya belum paham maksud '{meant}', {T}, jadi belum saya simpan."
    if not habits.learn_alias(heard, meant, max_words=5):
        return f"Itu tidak bisa saya simpan, {T}."
    events.emit("routines", items=protocols.for_hud())
    return f"Siap, {T}. Kalau bilang {heard}, artinya {meant}."


def handle(text: str) -> str | None:
    """A sentence about teaching, or part of it. None: it has nothing to do with this."""
    low = _low(text)
    if teaching():
        return _teach_step(text.strip(" .!?"), low)
    m = TAUGHT_RE.fullmatch(low)
    if m:
        return teach_word(m["heard"], m["meant"])
    m = FORGET_RE.fullmatch(low)
    if m:
        forgot = habits.forget_alias(m["heard"])
        if forgot:
            events.emit("routines", items=protocols.for_hud())
        return f"Baik, {T}, '{m['heard']}' saya lupakan." if forgot else f"Tidak ada kata '{m['heard']}' yang diajarkan, {T}."
    # "buat rutini", "gambar rutinitas baru", "modul jam terbang", "model ga": the words as they really came out (log)
    low = re.sub(r"^(?:(?:tolong|coba) )?gambar (?=rutin)", "tambah ", re.sub(r"\b(?:rutinit|rutini|rutin)\b", "rutinitas", low))
    low = re.sub(r"\b(?:modul|model|mote)\b(?= \S+$| \S+ \S+$)", "mode", low) if _named(re.sub(r"^.*?\b(?:modul|model|mote) ", "", low), False) else low
    m = START_RE.fullmatch(low)
    if m and not (m["word"] == "mode" and re.match(r"(?:hemat|seimbang|performa|gaming|game|gelap|terang)\b", m["rest"] or "")):
        return _start(m["rest"])                         # ("buat mode hemat" is the power mode)
    m = DELETE_RE.fullmatch(low)
    if m:
        key = _named(m["name"], True)
        if key:
            return safety.guard(f"menghapus rutinitas {key}", delete, always=True, asking=f"Hapus rutinitas {key}, {T}?", key=key)
        return None
    if LIST_RE.fullmatch(low):
        names = list(protocols.load())
        return f"Rutinitas yang ada: {', '.join(names)}, {T}." if names else f"Belum ada rutinitas, {T}."
    m = SHOW_RE.fullmatch(low)
    if m:
        key = _named(m["name"] or m["name2"], True)
        if key:
            steps = protocols.for_hud_one(key)
            return f"Rutinitas {key}: {', '.join(steps)}, {T}."
        return None
    m = RUN_RE.fullmatch(low)
    if m:
        key = _named(m["name"] or m["name2"], m["word"] != "mode")
        if key:
            return _run(key)
    return None


def delete(key: str) -> str:
    all_routines = protocols.load()
    if key not in all_routines:
        return f"Rutinitas {key} sudah tidak ada, {T}."
    del all_routines[key]
    protocols.save(all_routines)
    events.emit("routines", items=protocols.for_hud())
    return f"Rutinitas {key} dihapus, {T}."


def save_typed(name: str, lines: list[str]) -> dict:
    """From the Control Panel: what the voice conversation does, all at once."""
    name = _clean_name(name)
    problem = _name_problem(name)
    if problem:
        return {"ok": False, "error": problem}
    phrases = [line.strip(" .,") for line in lines if line.strip(" .,")]
    if not phrases:
        return {"ok": False, "error": "Isi langkahnya dulu, satu per baris."}
    steps, missed = _take(phrases)
    if missed:
        return {"ok": False, "error": f"Belum saya pahami: {missed}. Pakai kata seperti saat memberi perintah, atau tulis alamat situsnya."}
    return {"ok": True, "key": save(name, steps[:MAX_STEPS])}


def page_data() -> dict:
    return {"routines": protocols.for_hud(), "words": [{"heard": k, "meant": v} for k, v in habits.learned_aliases().items()]}
