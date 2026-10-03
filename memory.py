"""Persistent long-term semantic memory: facts the user asks DIGIDAW to remember.

Uses Gemini Embeddings to store facts as vectors, enabling semantic search
(finding facts by meaning, not just exact keywords).
Falls back to keyword matching when embeddings are unavailable.
"""
import json
import math
import logging
import datetime
from pathlib import Path

import config
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
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return []


def _save(items: list[dict]):
    SEMANTIC_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


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
    kept = [i for i in items if keyword.lower() not in i["fact"].lower()]
    removed = len(items) - len(kept)
    _save(kept)
    if removed:
        return f"{removed} ingatan tentang '{keyword}' sudah saya hapus, {config.USER_TITLE}."
    return f"Saya tidak menemukan ingatan tentang '{keyword}', {config.USER_TITLE}."


def all_facts() -> list[str]:
    return [i["fact"] for i in _load()]


def as_text() -> str:
    facts = all_facts()
    if not facts:
        return f"Memori saya masih kosong, {config.USER_TITLE}."
    return "Yang saya ingat: " + "; ".join(facts)
