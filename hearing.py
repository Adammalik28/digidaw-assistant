"""
Which of Google's guesses was meant. Google answers with up to 5 of them, and its first choice is often not the command:
with a noisy voice "tutup Brave" comes back as "tutup bread" first and "tutup Brave" fifth. Digidaw knows what is open and
what it can do, which Google doesn't, so a short phrase takes the guess Digidaw understands best. The first guess always
wins a tie, and sentences of talk are left as they are.
"""
import difflib
import re
import time

NOTHING, SOMETHING, SURE = 0, 1, 2
MAX_WORDS = 6             # a command is short; a longer phrase is talk or dictation, whose first guess is the best one
# skills whose route takes any words: that a route exists says nothing about them
FREE_TEXT = {"play_youtube", "search_youtube", "google_search", "web_search", "type_text", "say", "write_note", "set_clipboard",
             "generate_image", "create_file", "create_project", "navigate"}
NAMED = {"open_application": ("app", "site", "window"), "open_website": ("site",), "close_application": ("window", "app", "site"),
         "focus_window": ("window", "site", "app"), "close_tab": ("site", "window")}
QUESTION_OPEN_SECONDS = 60
MIN_ALIKE = 0.6


def level(text: str) -> int:
    """How well Digidaw understands this phrase by itself. SURE: a command with a known target."""
    import agenda
    import brain
    import habits
    import pc_control
    import resolver
    low = text.strip(" .,!?").lower()
    if agenda.OPEN_PAGE.fullmatch(low) or agenda.CLOSE_PAGE.fullmatch(low):
        return SURE
    route = brain._route(habits.apply_aliases(text))
    if not route:
        return NOTHING
    tool, args = route
    if tool == "_ask" or tool in FREE_TEXT:
        return SOMETHING
    if tool == "run_script":
        return SURE if pc_control.is_my_script(str(args.get("name", ""))) else SOMETHING
    if tool in NAMED:
        name = str(args.get("name") or args.get("site") or "")
        verdict, _ = resolver.resolve(name, NAMED[tool], sure_only=True)
        return SURE if verdict == "sure" else SOMETHING
    return SURE


def _answer_awaited() -> bool:
    """Digidaw asked something: "ya" must stay "ya", even when Google's next guess happens to be a command."""
    import brain
    import notifications
    import routines
    import safety
    if routines.teaching():              # "Lalu?" is answered with commands: their words are what the guesses are for
        return False
    if safety.has_pending() or brain._pending_pick or notifications.offered() is not None:
        return True
    turn = brain._dialog[-1] if brain._dialog else None
    return bool(turn and time.time() - turn.get("at", 0) < QUESTION_OPEN_SECONDS and turn["reply"].rstrip().endswith("?"))


def _sounds_alike(first: str, other: str) -> bool:
    """Guesses for the same sound look alike ("tutup bread", "tutup Brave"). "lagi dong" and "IG" don't: a stray guess that
    happens to be a command, which once turned a call into a timer named IG."""
    return difflib.SequenceMatcher(None, first.lower(), other.lower()).ratio() >= MIN_ALIKE


def best_guess(guesses: list[str]) -> str:
    """The guess to use (the first one unless another is understood better)."""
    import core
    first = guesses[0]
    if len(guesses) < 2 or len(re.findall(r"\S+", first)) > MAX_WORDS or _answer_awaited() \
            or core.detect_wake(first)[0] or core.sounds_like_call(first):     # the name, however it was written: a call stays one
        return first
    best, best_level = first, level(first)
    if best_level == SURE:
        return first
    for guess in guesses[1:]:
        if not _sounds_alike(first, guess):
            continue
        found = level(guess)
        if found > best_level:
            best, best_level = guess, found
            if found == SURE:
                break
    return best
