"""
Confirmation gate for dangerous / irreversible actions.

A dangerous skill calls guard(...) instead of executing directly. The action is
parked as "pending" and DIGIDAW asks the user to confirm with "ya"/"tidak".
brain.process_command() resolves the pending action on the next user input.
"""
import config

CONFIRMATION_TOKEN = "MENUNGGU_KONFIRMASI"

YES_WORDS = {"ya", "iya", "yes", "yup", "betul", "benar", "lanjut", "lanjutkan", "oke", "ok",
             "setuju", "boleh", "silakan", "confirm", "konfirmasi", "do it", "lakukan", "gas"}
NO_WORDS = {"tidak", "nggak", "gak", "enggak", "jangan", "no", "batal", "batalkan", "cancel",
            "stop", "tunggu"}

_pending = None  # dict(description=str, fn=callable, kwargs=dict)


def guard(description: str, fn, **kwargs) -> str:
    """Park an action until the user confirms it (or run it now if confirmations are off)."""
    global _pending
    if not config.CONFIRM_DANGEROUS_ACTIONS:
        return fn(**kwargs)
    _pending = {"description": description, "fn": fn, "kwargs": kwargs}
    return (f"{CONFIRMATION_TOKEN}: Aksi '{description}' butuh konfirmasi. "
            f"Tanyakan ke user apakah yakin, minta jawab 'ya' atau 'tidak'.")


def has_pending() -> bool:
    return _pending is not None


def pending_description() -> str:
    return _pending["description"] if _pending else ""


def classify_answer(text: str) -> str:
    """Return 'yes', 'no', or 'other'."""
    words = text.lower().strip(" .!?,").split()
    if not words or len(words) > 5:
        return "other"
    joined = " ".join(words)
    if joined in YES_WORDS or words[0] in YES_WORDS:
        return "yes"
    if joined in NO_WORDS or words[0] in NO_WORDS:
        return "no"
    return "other"


def confirm() -> str:
    global _pending
    action, _pending = _pending, None
    if not action:
        return ""
    try:
        return action["fn"](**action["kwargs"])
    except Exception as e:
        return f"Gagal menjalankan {action['description']}: {e}"


def cancel() -> str:
    global _pending
    action, _pending = _pending, None
    return f"Baik, {config.USER_TITLE}. Aksi '{action['description']}' dibatalkan." if action else ""
