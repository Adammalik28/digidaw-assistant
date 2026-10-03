"""
Shared Gemini client, model router and a one-shot generation helper.

The free tier allows ~15 requests per minute *per model*. The router keeps its own count so it can
switch to the next model before hitting the limit, rests a model that answered 429 for exactly as
long as Google asks, drops models that no longer exist (404), and always starts again from the
fastest model — so one busy minute never leaves Digidaw stuck on a slower model.

It also keeps an honest health status for the HUD and for error messages: every failure is classified
(quota, server busy, no internet, API key rejected, a bad request or an error inside Digidaw itself), so
"the AI doesn't work" is never reported as "quota full" when the real cause is something else.
"""
import re
import time
import logging
import threading
from collections import deque

import config

logging.getLogger("google").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

_client = None
_lock = threading.RLock()
_calls: dict[str, deque] = {}          # model -> timestamps of recent requests
_resting_until: dict[str, float] = {}  # model -> time it may be used again
_rest_reason: dict[str, str] = {}      # model -> "quota" / "busy" (why it rests)
_retired: set[str] = set()             # models Google no longer serves to this key
_last_error: dict = {}                 # kind, detail, model, at, per_day
_health = {"state": "unknown", "detail": "", "model": "", "since": 0.0, "last_ok": 0.0}
# failures that are the same for every model: trying the next model would only fail again
GLOBAL_FAILURES = {"network", "key", "request", "internal"}
_NETWORK_ERRORS = {"ConnectError", "ConnectTimeout", "NetworkError", "gaierror", "ProxyError", "RemoteProtocolError",
                   "ConnectionError", "ConnectionResetError", "ConnectionRefusedError"}
_NETWORK_TEXT = re.compile(r"getaddrinfo|Name or service not known|No address associated|Errno 1100[1-4]|"
                           r"WinError 100(?:5[014]|6[05])|network is unreachable|connection refused|"
                           r"Failed to establish|forcibly closed", re.IGNORECASE)


def get_client():
    global _client
    if _client is None and config.GEMINI_API_KEY:
        from google import genai
        from google.genai import types
        _client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=int(config.AI_TIMEOUT_SECONDS * 1000)),
        )
    return _client


def thinking_config(model: str):
    from google.genai import types
    level = config.MODEL_THINKING.get(model)
    return types.ThinkingConfig(thinking_level=level) if level else None


def clean(text: str) -> str:
    return (text or "").replace("*", "").replace("#", "").strip()


# ───────────────────────────── model router ─────────────────────────────

def _used_last_minute(model: str) -> int:
    q = _calls.setdefault(model, deque())
    while q and time.time() - q[0] > 60:
        q.popleft()
    return len(q)


def candidates() -> list[str]:
    """Models to try, fastest first, skipping ones that are resting or at their per-minute budget."""
    with _lock:
        now = time.time()
        ready = [m for m in config.FALLBACK_MODELS
                 if m not in _retired and _resting_until.get(m, 0) <= now and _used_last_minute(m) < config.MODEL_RPM]
    return ready


def seconds_until_available() -> int:
    """How long until some model can be used again (for the "quota full" message)."""
    with _lock:
        now = time.time()
        waits = []
        for m in config.FALLBACK_MODELS:
            if m in _retired:
                continue
            q = _calls.get(m) or deque()
            budget_free = (q[0] + 60 - now) if len(q) >= config.MODEL_RPM and q else 0
            waits.append(max(_resting_until.get(m, 0) - now, budget_free, 0))
    return int(min(waits)) + 1 if waits else 60


def record_call(model: str):
    with _lock:
        _calls.setdefault(model, deque()).append(time.time())
        _health["model"] = model
    _emit_health()


def classify(error: Exception) -> str:
    """quota | busy | network | key | request | internal | gone"""
    text = str(error)
    name = type(error).__name__
    code = getattr(error, "code", None)
    code = code if isinstance(code, int) else None
    if code == 429 or "RESOURCE_EXHAUSTED" in text:
        return "quota"
    if code in (401, 403) or re.search(r"API_KEY_INVALID|API key not valid|PERMISSION_DENIED|UNAUTHENTICATED|"
                                       r"location is not supported", text):
        return "key"
    if code == 404 or "NOT_FOUND" in text:
        return "gone"
    if code == 400 or re.search(r"INVALID_ARGUMENT|FAILED_PRECONDITION", text):
        return "request"
    if (code and code >= 500) or re.search(r"UNAVAILABLE|\bINTERNAL\b|DEADLINE_EXCEEDED|overloaded", text):
        return "busy"
    if name in _NETWORK_ERRORS or _NETWORK_TEXT.search(text):
        return "network"
    if "Timeout" in name or "timed out" in text.lower():
        return "busy"
    if code is None and isinstance(error, (ValueError, TypeError, AttributeError, KeyError, IndexError, NameError)):
        return "internal"          # raised by Digidaw / the SDK before anything was sent
    return "busy"


def _short(error: Exception) -> str:
    text = re.sub(r"\s+", " ", str(error)).strip()
    m = re.search(r"'message': '([^']+)'", text)
    return (m.group(1) if m else text)[:160]


def report_error(model: str, error: Exception) -> str:
    """Remember what went wrong with a model. Returns a short label for the log."""
    kind = classify(error)
    text = str(error)
    with _lock:
        _last_error.update(kind=kind, detail=_short(error), model=model, at=time.time(), per_day=False)
        if kind == "quota":
            m = re.search(r"retryDelay'?: '?(\d+(?:\.\d+)?)s", text)
            per_day = "PerDay" in text
            rest = 3600 if per_day else (float(m.group(1)) + 1 if m else 60)
            _resting_until[model] = time.time() + rest
            _rest_reason[model] = "quota"
            _last_error["per_day"] = per_day
            return f"kuota {'harian' if per_day else 'per menit'} habis, istirahat {int(rest)} dtk"
        if kind == "gone":
            _retired.add(model)
            return "model tidak tersedia lagi"
        if kind == "busy":
            _resting_until[model] = time.time() + 15     # timeouts / 5xx: skip it briefly
            _rest_reason[model] = "busy"
            return f"server sibuk: {_last_error['detail'][:80]}"
        # network / key / request / internal: not this model's fault, so it doesn't rest
        label = {"network": "tidak ada koneksi", "key": "API key ditolak", "request": "permintaan ditolak Google",
                 "internal": "error di dalam Digidaw"}[kind]
        return f"{label}: {_last_error['detail'][:100]}"


# ───────────────────────────── health (HUD + error messages) ─────────────────────────────

def _emit_health():
    try:
        import events
        events.emit("ai", **health())
    except Exception:
        pass


def mark_ok(model: str):
    with _lock:
        now = time.time()
        _health.update(state="ok", detail="", model=model, since=now, last_ok=now)
    _emit_health()


def mark_checking():
    with _lock:
        _health.update(state="checking", detail="", since=time.time())
    _emit_health()


def mark_failed(tried: bool = True):
    """A whole request failed: every usable model was tried, or none was available."""
    with _lock:
        now = time.time()
        if tried and _last_error and now - _last_error.get("at", 0) < 120:
            kind, detail = _last_error["kind"], _last_error["detail"]
            if kind == "gone":
                kind, detail = "busy", "model AI yang dicoba tidak tersedia lagi"
        else:
            resting = [m for m in config.FALLBACK_MODELS if _resting_until.get(m, 0) > now]
            kind = "quota" if any(_rest_reason.get(m) == "quota" for m in resting) or not resting else "busy"
            detail = ""
        _health.update(state=kind, detail=detail, since=now)
    _emit_health()


def health() -> dict:
    """{state, detail, model, used, limit, retry_at, last_ok, per_day} for the HUD.
    state: unknown | checking | ok | quota | busy | network | key | request | internal | nokey"""
    with _lock:
        now = time.time()
        state, detail = _health["state"], _health["detail"]
        retry_at = 0.0
        if not config.GEMINI_API_KEY:
            state, detail = "nokey", "Kunci API Gemini belum diisi"
        elif state not in GLOBAL_FAILURES and state != "checking":
            if candidates():
                if state in ("quota", "busy"):
                    state = "ok"                 # a model is free again
            else:
                state = state if state in ("quota", "busy") else "quota"   # every model at its limit
                retry_at = now + seconds_until_available()
        model = _health.get("model") or ""
        return {"state": state, "detail": detail, "model": model, "used": _used_last_minute(model) if model else 0,
                "limit": config.MODEL_RPM, "retry_at": retry_at, "last_ok": _health["last_ok"],
                "per_day": bool(_last_error.get("per_day")) and state == "quota"}


def _failure_view() -> dict:
    """health(), but right after a failed request its cause wins over "a model is free again"."""
    h = health()
    with _lock:
        recent = time.time() - _health["since"] < 15 and _health["state"] not in ("ok", "unknown", "checking")
        if recent and h["state"] != "nokey":
            h["state"], h["detail"] = _health["state"], _health["detail"]
            if h["state"] == "quota":
                h["per_day"] = bool(_last_error.get("per_day"))
    return h


def short_reason() -> str:
    """Why the AI can't answer, as a short phrase (for 'Maaf, analisis gagal: <reason>')."""
    h = _failure_view()
    wait = max(1, int(h["retry_at"] - time.time())) if h["retry_at"] else seconds_until_available()
    return {
        "nokey": "kunci API Gemini belum diatur (Control Panel → AI)",
        "quota": "kuota harian AI gratis sudah habis" if h["per_day"] else f"batas AI gratis per menit penuh, coba lagi ±{wait} detik",
        "busy": "server AI Google sedang sibuk, coba lagi sebentar",
        "network": "tidak ada koneksi internet ke server AI",
        "key": f"Google menolak kunci API Gemini ({h['detail'][:80]})",
        "request": f"Google menolak permintaannya ({h['detail'][:80]})",
        "internal": f"ada error di dalam Digidaw ({h['detail'][:80]})",
    }.get(h["state"], "AI tidak merespons")


def explain(title: str) -> str:
    """Full spoken sentence for when a free-form request can't be answered."""
    h = _failure_view()
    basic = "Perintah dasar seperti musik, volume, alarm, dan buka aplikasi tetap bisa."
    wait = max(1, int(h["retry_at"] - time.time())) if h["retry_at"] else seconds_until_available()
    if h["state"] == "nokey":
        return f"Maaf {title}, kunci API Gemini belum diatur, jadi saya belum bisa memahami perintah bebas. Isi di Control Panel, bagian AI. {basic}"
    if h["state"] == "quota":
        if h["per_day"]:
            return f"Maaf {title}, kuota harian AI gratis sudah habis. Coba lagi nanti. {basic}"
        return f"Maaf {title}, batas pemakaian AI gratis per menit sedang penuh. Coba lagi sekitar {wait} detik lagi. {basic}"
    if h["state"] == "busy":
        return f"Maaf {title}, server AI Google sedang sibuk. Coba lagi sebentar lagi. {basic}"
    if h["state"] == "network":
        return f"Maaf {title}, saya tidak bisa menghubungi AI karena koneksi internet bermasalah. {basic}"
    if h["state"] == "key":
        return f"Maaf {title}, Google menolak kunci API Gemini. Periksa kuncinya di Control Panel, bagian AI. {basic}"
    if h["state"] in ("request", "internal"):
        return (f"Maaf {title}, ada error saat menyiapkan permintaan ke AI, jadi bukan karena kuota. "
                f"Detailnya ada di log dan di indikator AI pada HUD. {basic}")
    return f"Maaf {title}, AI sedang tidak merespons. {basic}"


def status() -> dict:
    with _lock:
        now = time.time()
        return {m: {"used": _used_last_minute(m), "resting": max(0, int(_resting_until.get(m, 0) - now)),
                    "retired": m in _retired} for m in config.FALLBACK_MODELS}


def generate(contents, system_instruction: str | None = None, max_tokens: int = 600) -> str:
    """One-shot generation without tools, using the router."""
    from google.genai import types
    client = get_client()
    if not client:
        mark_failed(tried=False)
        raise RuntimeError(short_reason())
    tried, slow_failures = False, 0
    for model in candidates():
        tried = True
        try:
            record_call(model)
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=max_tokens,
                    temperature=0.4,
                    thinking_config=thinking_config(model),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            if response.text:
                mark_ok(model)
                return clean(response.text)
        except Exception as e:
            print(f"[AI] {model}: {report_error(model, e)}")
            kind = classify(e)
            if kind in GLOBAL_FAILURES:
                break
            if kind == "busy":
                slow_failures += 1
                if slow_failures >= 3:
                    break
    mark_failed(tried)
    raise RuntimeError(short_reason())
