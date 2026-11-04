"""
Confirmation gate for dangerous / irreversible actions.

A dangerous skill calls guard(...) instead of executing directly. The action is
parked as "pending" and DIGIDAW asks the user to confirm with "ya"/"tidak".
brain.process_command() resolves the pending action on the next user input.
"""
import time

import config

CONFIRMATION_TOKEN = "MENUNGGU_KONFIRMASI"

YES_WORDS = {"ya", "iya", "yes", "yup", "betul", "benar", "lanjut", "lanjutkan", "oke", "ok",
             "setuju", "boleh", "silakan", "confirm", "konfirmasi", "do it", "lakukan", "gas"}
NO_WORDS = {"tidak", "nggak", "gak", "enggak", "jangan", "no", "batal", "batalkan", "cancel",
            "stop", "tunggu"}
# words that may follow the answer without changing it ("ya, matikan saja", "tidak usah dulu")
FILLER_WORDS = {"saja", "aja", "dong", "deh", "sih", "sekarang", "yakin", "tolong", "please", "digidaw", "tuan",
                "bener", "betul", "benar", "jadi", "usah", "dulu", "mau", "terus", "jalankan", "kerjakan", "sudah"}
PENDING_SECONDS = 60          # an unanswered question expires: much later, "ya" means something else

_pending = None  # dict(description=str, fn=callable, kwargs=dict, at=float)


def guard(description: str, fn, always: bool = False, asking: str = "", **kwargs) -> str:
    """Park an action until the user confirms it (or run it now if confirmations are off). always: ask even then
    (sending a message in the user's name). asking: the question, when "Yakin ...?" isn't what is meant."""
    global _pending
    if not config.CONFIRM_DANGEROUS_ACTIONS and not always:
        return fn(**kwargs)
    _pending = {"description": description, "fn": fn, "kwargs": kwargs, "at": time.time(), "asking": asking}
    return (f"{CONFIRMATION_TOKEN}: Aksi '{description}' butuh konfirmasi. "
            f"Tanyakan ke user apakah yakin, minta jawab 'ya' atau 'tidak'.")


def has_pending() -> bool:
    global _pending
    if _pending is not None and time.time() - _pending["at"] > PENDING_SECONDS:
        _pending = None               # nobody answered: the action is dropped, not run later by a stray "ya"
    return _pending is not None


def pending_description() -> str:
    return _pending["description"] if _pending else ""


QUESTION_START = "Yakin "


def question() -> str:
    """The yes / no question for the action that waits for an answer, short, as a person asks it: 'Yakin mengirim
    WhatsApp ke Rina: "oke", Tuan?' (it used to be 'Apakah Anda yakin ingin …, Tuan? Jawab ya atau tidak.': twice as
    long to say, and "Anda" even when Digidaw speaks with "kamu")."""
    if not _pending:
        return ""
    return _pending.get("asking") or f"{QUESTION_START}{pending_description()}, {config.USER_TITLE}?"


def classify_answer(text: str) -> str:
    """Return 'yes', 'no', or 'other'. The whole answer must be a yes / no: "ya, matikan saja" is yes, but
    "ya ampun" or "oke nanti aku telepon" (someone talking nearby) is not."""
    import re
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    if not words or len(words) > 5:
        return "other"
    joined = " ".join(words)
    action = pending_description().lower()

    def only(answer_words: set) -> bool:
        return words[0] in answer_words and all(
            w in answer_words or w in FILLER_WORDS or w in config.USER_TITLE.lower().split()
            or (len(w) >= 4 and w in action) for w in words[1:])        # "ya, matikan komputer"
    if joined in YES_WORDS or only(YES_WORDS):
        return "yes"
    if joined in NO_WORDS or only(NO_WORDS):
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
