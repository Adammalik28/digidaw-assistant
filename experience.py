"""
Digidaw learns from what worked, without any training run: when the online AI (the smart teacher) handled a sentence
with one skill, that pair is kept. The next time something similar is said, the local brain gets those pairs as
examples (local_router.pick), so more and more goes right on this laptop, at once and without the internet.

A correction ("bukan, maksudnya …") removes the pair again. Messages are never kept. Stays on this PC, for this user
only (logs/experience.json, MAX_ITEMS newest).
"""
import re
import threading
import time

import config
import storage

FILE = config.LOG_DIR / "experience.json"
MAX_ITEMS = 800
MIN_SIMILARITY = 0.32             # (character trigrams) below this a sentence isn't really like the new one
NEVER = {"reply_message", "type_text", "set_clipboard", "write_note", "append_to_last_note", "remember", "run_command",
         "web_search", "google_search", "say", "ai_status"}   # private content, or a choice that teaches nothing

_lock = threading.Lock()
_items: list[dict] | None = None


def _load() -> list[dict]:
    global _items
    if _items is None:
        data = storage.load_json(FILE, [])
        _items = data if isinstance(data, list) else []
    return _items


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _grams(text: str) -> set[str]:
    padded = f"  {_norm(text)} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def note(text: str, tool: str, args: dict | None = None):
    """The online AI answered this sentence with this one skill, and it worked."""
    key = _norm(text)
    if tool in NEVER or len(key.split()) < 2 or len(key) > 120:
        return                            # (one word is too often a mishearing to learn from)
    with _lock:
        items = _load()
        for item in items:
            if item["text"] == key:
                item.update(tool=tool, args=args or {}, at=time.time())
                break
        else:
            items.append({"text": key, "tool": tool, "args": args or {}, "at": time.time()})
        del items[:-MAX_ITEMS]
        try:
            storage.save_json(FILE, items, mode=storage.PRIVATE)
        except OSError:
            pass


def forget(text: str):
    """The user corrected what happened for this sentence: it taught something wrong."""
    key = _norm(text)
    with _lock:
        items = _load()
        kept = [i for i in items if i["text"] != key]
        if len(kept) != len(items):
            items[:] = kept
            try:
                storage.save_json(FILE, items, mode=storage.PRIVATE)
            except OSError:
                pass


def similar(text: str, tool: str = "", limit: int = 3) -> list[dict]:
    """The sentences most like this one that worked before (of this tool only, if given), best first."""
    grams = _grams(text)
    scored = []
    with _lock:
        for item in _load():
            if tool and item["tool"] != tool:
                continue
            other = _grams(item["text"])
            score = len(grams & other) / max(1, len(grams | other))
            if score >= MIN_SIMILARITY:
                scored.append((score, item))
    scored.sort(key=lambda s: -s[0])
    return [item for _, item in scored[:limit]]


def count() -> int:
    with _lock:
        return len(_load())
