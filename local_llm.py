"""
The local brain: a small model served by Ollama (http://127.0.0.1:11434), used for everyday commands and
light questions so they work instantly, offline and without spending Gemini quota.

Only the HTTP API is used (no extra package). Tool schemas are built from the same Python skills Gemini uses
(signature + docstring), so a skill works the same whichever brain calls it.
"""
import inspect
import re
import threading
import time
import typing

import requests

import config

_status = {"ok": None, "checked": 0.0, "reason": ""}
_lock = threading.Lock()
STATUS_TTL = 30               # seconds a "running / not running" answer is trusted
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _url(path: str) -> str:
    return config.OLLAMA_URL.rstrip("/") + path


def available(refresh: bool = False) -> bool:
    """Is Ollama running with the configured model installed? (cached for STATUS_TTL seconds)"""
    with _lock:
        if not refresh and _status["ok"] is not None and time.time() - _status["checked"] < STATUS_TTL:
            return _status["ok"]
    ok, reason = False, ""
    try:
        models = requests.get(_url("/api/tags"), timeout=2).json().get("models", [])
        names = {m.get("name", "") for m in models} | {m.get("model", "") for m in models}
        wanted = config.LOCAL_MODEL
        ok = wanted in names or f"{wanted}:latest" in names
        reason = "" if ok else f"model {wanted} belum diunduh (ollama pull {wanted})"
    except requests.RequestException:
        reason = "Ollama tidak berjalan"
    with _lock:
        _status.update(ok=ok, checked=time.time(), reason=reason)
    return ok


def status_reason() -> str:
    return _status["reason"]


def mark_down(reason: str):
    with _lock:
        _status.update(ok=False, checked=time.time(), reason=reason)


def chat(messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 400) -> dict:
    """One /api/chat request. Returns the assistant message ({'content', 'tool_calls'?})."""
    body = {
        "model": config.LOCAL_MODEL, "messages": messages, "stream": False,
        "keep_alive": config.LOCAL_KEEP_ALIVE,
        "options": {"temperature": 0.8, "num_ctx": config.LOCAL_CONTEXT, "num_predict": max_tokens},
    }
    if tools:
        body["tools"] = tools
    r = requests.post(_url("/api/chat"), json=body, timeout=config.LOCAL_TIMEOUT_SECONDS if max_tokens > 1 else 120)
    r.raise_for_status()
    message = r.json().get("message") or {}
    message["content"] = strip_thinking(message.get("content", ""))
    return message


def strip_thinking(text: str) -> str:
    """Drop a <think>...</think> block some models still write into the answer."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    return text.replace("*", "").replace("#", "").strip()


def warm_up(system_prompt: str = ""):
    """Load the model in the background, with the same context size real requests use (a different size makes
    Ollama load it again) and the system prompt already processed, so the first command is fast too."""
    def run():
        if available(refresh=True):
            try:
                chat([{"role": "system", "content": system_prompt}, {"role": "user", "content": "halo"}], max_tokens=1)
            except requests.RequestException:
                pass
    threading.Thread(target=run, daemon=True, name="ollama-warmup").start()


# ───────────────────────────── tool schemas ─────────────────────────────

def _json_type(annotation) -> dict:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _json_type(args[0]) if args else {"type": "string"}
    if origin is list or annotation is list:
        item = (typing.get_args(annotation) or (str,))[0]
        return {"type": "array", "items": _json_type(item)}
    return {"type": _JSON_TYPES.get(annotation, "string")}


def tool_schema(fn) -> dict:
    """Ollama/OpenAI-style function schema from a skill's signature and docstring."""
    props, required = {}, []
    hints = typing.get_type_hints(fn)
    for name, param in inspect.signature(fn).parameters.items():
        if name in ("self", "args", "kwargs"):
            continue
        props[name] = _json_type(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    doc = " ".join((inspect.getdoc(fn) or fn.__name__).split())
    return {"type": "function", "function": {
        "name": fn.__name__, "description": doc[:300],
        "parameters": {"type": "object", "properties": props, "required": required}}}
