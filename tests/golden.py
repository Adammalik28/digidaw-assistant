"""The router's decision for one sentence, the same every time (fake desktop, no recent lamp change, no chat just
in, nothing to repeat): shared by make_golden.py and test_golden.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import brain          # noqa: E402
import notifications  # noqa: E402
import smarthome      # noqa: E402
import watchers       # noqa: E402
import test_router    # noqa: E402

# questions and requests that must keep going to the AI (no fast route may grab them)
STAYS_WITH_AI = ["kenapa langit biru", "buatkan pantun", "harga bitcoin hari ini", "siapa juara liga inggris musim lalu",
                 "berita terbaru tentang teknologi", "buatkan saya cerita singkat", "siapa anda", "kucing mau makan apa"]
_ready = False


def _setup():
    global _ready
    if _ready:
        return
    test_router.setup_module()
    smarthome.has_lamps = lambda: True
    smarthome.changed_by_user_since = lambda t, name="": False
    _ready = True


def decide(text: str):
    """(tool, args) or None, like fast_route() would choose (without running anything)."""
    _setup()
    brain._last_fast.update(tool="", kwargs={}, at=0.0)
    notifications._last_chat.update(at=0.0)
    if brain._reply_route(text):
        return "reply_message", {}
    watch = watchers.parse(text)
    if watch:
        return "watch_for", {}
    action = brain._route(text)
    if not action:
        return None
    name, kwargs = action
    if name == "_ask":
        return "_ask", {"options": [label for label, _, _ in kwargs["options"]]}
    return name, kwargs
