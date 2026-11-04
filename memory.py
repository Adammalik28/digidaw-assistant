"""Persistent long-term semantic memory: facts about the user, told on purpose ("ingat ya ...") or said in passing.

Uses Gemini Embeddings to store facts as vectors, enabling semantic search
(finding facts by meaning, not just exact keywords).
Falls back to keyword matching when embeddings are unavailable.

learnable(): "aku alergi kacang", said to Digidaw in passing, is worth keeping. A quick check of the words first, then
the local model writes the fact; brain.memory_question asks before it is saved, because speech gets misheard. Nothing
is saved on silence, and passwords and the like never are.
"""
import json
import math
import logging
import datetime
import re
from pathlib import Path

import config
import storage
import llm

log = logging.getLogger("digidaw.memory")

MEMORY_FILE = config.BASE_DIR / "memory.json"
EMBED_MODEL = "gemini-embedding-001"   # text-embedding-004 was retired by Google (404)
EMBED_DIMS = 768
ALL_IN_PROMPT = 25                     # up to this many facts simply all go into the prompt (no lookup needed)
SEMANTIC_FILE = config.BASE_DIR / "memory_semantic.json"


# ───────────────────────────── storage ─────────────────────────────

def _load() -> list[dict]:
    """Load from semantic file first, fall back to legacy file."""
    for path in (SEMANTIC_FILE, MEMORY_FILE):
        data = storage.load_json(path)
        if data is not None:
            return data
    return []


def _save(items: list[dict]):
    storage.save_json(SEMANTIC_FILE, items, backup=True, mode=storage.PRIVATE, indent=2)


# ───────────────────────────── embeddings ─────────────────────────────

def _get_embedding(text: str) -> list[float]:
    """Get a vector representation of text using Gemini Embeddings."""
    try:
        client = llm.get_client()
        if not client:
            return []
        from google.genai import types
        response = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIMS),
        )
        # response.embeddings is a list; each element has a .values attribute
        values = response.embeddings[0].values
        # Convert to plain Python list of floats for JSON serialisation
        return [float(v) for v in values]
    except Exception as e:
        log.warning("Embedding gagal, memakai mode tanpa vektor: %s", e)
        return []


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity between two vectors (0.0 – 1.0)."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    mag1 = math.sqrt(sum(a * a for a in v1))
    mag2 = math.sqrt(sum(b * b for b in v2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


# ───────────────────────────── public API ─────────────────────────────

DUPLICATE_SIMILARITY = 0.9
TOO_GENERAL = {"saya", "aku", "gue", "tuan", "yang", "itu", "ini", "semua"}


def _plain(text: str) -> str:
    import re
    text = re.sub(r"^(?:saya|aku|pengguna|user)\s+", "", text.lower().strip())
    return re.sub(r"[^\w ]", "", text).strip()


def add(fact: str) -> str:
    """Remember a fact about the user."""
    items = _load()
    embedding = _get_embedding(fact)
    for item in items:          # "saya alergi makanan manis" vs "Pengguna alergi makanan manis": already known
        same_meaning = embedding and _cosine_similarity(embedding, item.get("embedding", [])) >= DUPLICATE_SIMILARITY
        if same_meaning or _plain(item["fact"]) == _plain(fact):
            return f"Itu sudah saya ingat, {config.USER_TITLE}."
    entry = {
        "fact": fact.strip(),
        "saved": datetime.datetime.now().isoformat(timespec="minutes"),
    }
    if embedding:
        entry["embedding"] = embedding
    items.append(entry)
    _save(items)
    return f"Tersimpan di memori saya, {config.USER_TITLE}: {fact.strip()}"


def needs_lookup() -> bool:
    """Only with many facts is a semantic lookup worth an extra request per turn."""
    return len(_load()) > ALL_IN_PROMPT


def _backfill(items: list[dict]):
    """Facts saved while embeddings failed get their vector now (once)."""
    changed = False
    for item in items:
        if not item.get("embedding") or len(item["embedding"]) != EMBED_DIMS:
            vector = _get_embedding(item["fact"])
            if vector:
                item["embedding"] = vector
                changed = True
    if changed:
        _save(items)


def retrieve_relevant(query: str, top_k: int = 5, threshold: float = 0.45) -> str:
    """Find memories semantically relevant to the query. Used to enrich the AI's context."""
    items = _load()
    if not items:
        return ""
    _backfill(items)

    query_emb = _get_embedding(query)

    # If embedding works, do semantic search
    if query_emb:
        scored = []
        for item in items:
            item_emb = item.get("embedding", [])
            if item_emb:
                score = _cosine_similarity(query_emb, item_emb)
                if score >= threshold:
                    scored.append((score, item["fact"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = [fact for _, fact in scored[:top_k]]
        if best:
            return "Fakta memori relevan: " + "; ".join(best)

    # Fallback: keyword match
    query_words = {w for w in query.lower().split() if len(w) >= 3}
    if query_words:
        keyword_hits = [
            item["fact"] for item in items
            if any(w in item["fact"].lower() for w in query_words)
        ]
        if keyword_hits:
            return "Fakta memori relevan: " + "; ".join(keyword_hits[:top_k])

    return ""


def remove(keyword: str) -> str:
    items = _load()
    keyword = re.sub(r"^(?:kalau|bahwa|tentang|soal)\s+", "", keyword.strip().lower())
    if len(keyword) < 3 or keyword in TOO_GENERAL:     # every fact says "saya": "lupakan saya" must not wipe them all
        return f"Yang mana yang perlu saya lupakan, {config.USER_TITLE}? Sebutkan katanya."
    kept = [i for i in items if keyword not in i["fact"].lower()]
    removed = len(items) - len(kept)
    _save(kept)
    if removed:
        return f"{removed} ingatan tentang '{keyword}' sudah saya hapus, {config.USER_TITLE}."
    return f"Saya tidak menemukan ingatan tentang '{keyword}', {config.USER_TITLE}."


def forget_latest() -> str:
    """Forget the fact saved last ("lupakan yang tadi")."""
    items = _load()
    if not items:
        return f"Memori saya masih kosong, {config.USER_TITLE}."
    fact = items.pop()["fact"]
    _save(items)
    return f"Baik {config.USER_TITLE}, sudah saya lupakan: {fact}."


def all_facts() -> list[str]:
    return [i["fact"] for i in _load()]


def as_text() -> str:
    facts = all_facts()
    if not facts:
        return f"Memori saya masih kosong, {config.USER_TITLE}."
    return "Yang saya ingat: " + "; ".join(facts)


# ───────────────────────────── learning from what is said in passing ─────────────────────────────

_ME = r"(?:aku|saya|gue|gua)"
_PEOPLE = (r"(?:ibu|ayah|bapak|mama|mamah|papa|papah|adik|adek|kakak|istri|suami|pacar|cewek|cowok|anak|teman|sahabat|bos|"
           r"atasan|dosen|nenek|kakek)")
STATEMENT_RE = re.compile(
    rf"\b{_ME}\b.{{0,30}}\b(?:suka|doyan|gemar|hobi|benci|alergi|pantang|kerja|bekerja|kuliah|sekolah|tinggal|lahir|"
    rf"punya|biasanya|selalu|jarang|sering|takut)\b"
    rf"|\b(?:nama|hobi|pekerjaan|kerjaan|kampus|sekolah|rumah|alamat|ulang tahun|ultah|jurusan)(?:ku| {_ME})\b"
    rf"|\b(?:favorit|kesukaan)(?:ku| {_ME})\b|\b{_PEOPLE}(?:ku| {_ME})\b|\bpanggil {_ME}\b", re.IGNORECASE)
# something that passes, or a request: not a fact about the user
PASSING_RE = re.compile(r"\b(?:lagi|sedang|barusan|tadi|kemarin|besok|nanti|capek|lapar|ngantuk|bosan|sakit|pusing|marah|"
                        r"sedih|pengen|ingin|mau|butuh|tolong|coba|bisa|kalau|kalo)\b", re.IGNORECASE)
ASKING_RE = re.compile(r"\?\s*$|^(?:apa|apakah|siapa|kenapa|mengapa|bagaimana|gimana|berapa|kapan|dimana|di mana|kok|mana)\b",
                       re.IGNORECASE)
SECRET_RE = re.compile(r"\b(?:password|kata sandi|sandi|pin|otp|cvv|kartu kredit|rekening|ktp|nik)\b", re.IGNORECASE)
FACT_SCHEMA = {"type": "object", "required": ["ada", "fakta"],
               "properties": {"ada": {"type": "boolean"}, "fakta": {"type": "string"}}}
FACT_SYSTEM = """Dari kalimat user, ambil satu fakta yang berlaku lama tentang dirinya atau orang dekatnya: selera, alergi, pekerjaan, kuliah, tempat tinggal, rutinitas, nama orang dekat, cara dipanggil.
Bukan fakta: keadaan sesaat, perintah, pertanyaan, pendapat tentang asisten, kalimat yang kacau, atau yang tidak jelas apa maksudnya ("aku suka ini").
fakta ditulis singkat memakai kata "saya", hanya dari kata-kata user, tanpa menambah apa pun.

Contoh:
"aku alergi kacang" -> ada=true, fakta="saya alergi kacang"
"kopi favoritku americano" -> ada=true, fakta="kopi favorit saya americano"
"rina itu adikku" -> ada=true, fakta="rina adik saya"
"aku kuliah jurusan sistem informasi" -> ada=true, fakta="saya kuliah jurusan sistem informasi"
"panggil aku bos" -> ada=true, fakta="saya dipanggil bos"
"aku suka film horor" -> ada=true, fakta="saya suka film horor"
"aku nggak suka durian" -> ada=true, fakta="saya nggak suka durian"
"gue doyan bakso" -> ada=true, fakta="saya doyan bakso"
"aku biasanya tidur jam sepuluh" -> ada=true, fakta="saya biasanya tidur jam sepuluh"
"ulang tahun ibuku 5 mei" -> ada=true, fakta="ulang tahun ibu saya 5 mei"
"aku suka ini" -> ada=false, fakta=""
"saya kerja dulu ya" -> ada=false, fakta=""
"kamu suka aku nggak" -> ada=false, fakta=""
"bapak kamu sibuk chord" -> ada=false, fakta="" """


def _known(fact: str) -> bool:
    plain = set(_plain(fact).split())
    for known in all_facts():
        other = set(_plain(known).split())
        if plain and other and len(plain & other) / len(plain | other) >= 0.75:
            return True
    return False


def learnable(sentence: str) -> str:
    """The lasting fact in what the user just said, or "". Instant when the words can't be one (almost always)."""
    import local_llm
    import local_router
    text = sentence.strip()
    if not 2 <= len(text.split()) <= 20 or not STATEMENT_RE.search(text) or PASSING_RE.search(text) \
            or ASKING_RE.search(text) or SECRET_RE.search(text) or not local_llm.available():
        return ""
    answer = json.loads(local_llm.generate(FACT_SYSTEM, text, schema=FACT_SCHEMA, max_tokens=60, temperature=0.0))
    fact = re.sub(r"^saya (\w{3,})ku\b", r"\1 saya", str(answer.get("fakta") or "").strip(" ."))   # "saya hobiku main gitar"
    if not answer.get("ada") or not fact or SECRET_RE.search(fact) or len(fact.split()) > 16 \
            or not local_router._grounded(fact, text) or _known(fact):
        return ""
    return fact


def about_you(fact: str) -> str:
    """The fact as said to the user: "saya alergi kacang" -> "Tuan alergi kacang"."""
    return re.sub(r"\b(?:saya|aku)\b", config.USER_TITLE, fact, flags=re.IGNORECASE)
