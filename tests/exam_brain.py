"""
How smart is the local brain, really? Runs every sentence of exam_cases.py through the real model (needs Ollama; no
tool is run: they only record), and grades what would have happened:
  benar = the right tool / the right kind of answer; aman = handed to the online AI when a local tool would do (slower,
  but right); salah = a wrong action or claiming to have done something without doing it (the bad one).

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/exam_brain.py [model]
"""
import functools
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
import brain   # noqa: E402
import golden  # noqa: E402
from exam_cases import CASES, HOLDOUT  # noqa: E402

# tests/exam_brain.py [model] [holdout]: the second exam is the one never tuned for
if len(sys.argv) > 1 and sys.argv[1] != "holdout":
    config.LOCAL_MODEL = sys.argv[1]
if sys.argv[-1] == "holdout":
    CASES = HOLDOUT
ran: list[tuple[str, dict]] = []


def _recorder(fn, name):
    @functools.wraps(fn)
    def record(*args, **kwargs):
        ran.append((name, kwargs))
        return f"Selesai: {name}."
    return record


brain.TOOL_MAP.update({n: _recorder(f, n) for n, f in list(brain.TOOL_MAP.items())})
brain._local_search_answer = lambda text: None
results, wrong, times, args_bad = [], [], [], []
for case in CASES:
    text, expect, needs = (*case, "")[:3]
    if golden.decide(text):
        continue
    want = expect.split("|")
    ran.clear()
    brain._system_notes.clear()
    brain._dialog.clear()
    brain._brain_used = ""
    t0 = time.time()
    reason = brain.gemini_reason(text)
    reply = None if reason else brain.ask_local(text)
    times.append(time.time() - t0)
    claims = bool(reply and (brain.ACTION_CLAIM_RE.search(reply) or brain.DONE_CLAIM_RE.search(reply)))
    if ran:
        got = ran[0][0]
        grade = "benar" if got in want else "salah"
        if grade == "benar" and needs and needs.lower() not in str(ran[0][1]).lower():
            args_bad.append((text, ran[0][1]))
    elif reply is None:
        got = "ke-AI"
        grade = "benar" if "AI" in want or "ASK" in want else "aman"
    else:
        got = "bicara"
        talk_ok = ("CHAT" in want and not claims) or ("ASK" in want and "?" in reply and not claims)
        grade = "benar" if talk_ok else "salah"
    results.append(grade)
    if grade != "benar":
        wrong.append(f"  {grade:5} {text[:44]:44} butuh {expect[:32]:32} dapat {got:18} {(reply or '')[:60]}")
n = len(results)
times.sort()
print(f"== {config.LOCAL_MODEL}: benar {results.count('benar')}/{n} ({100 * results.count('benar') // n}%), aman {results.count('aman')}, "
      f"SALAH {results.count('salah')} | argumen keliru {len(args_bad)} | median {times[len(times) // 2]:.2f} dtk, p90 {times[int(len(times) * .9)]:.2f} dtk")
print("\n".join(wrong))
for t, a in args_bad:
    print(f"  argumen {t[:44]:44} {a}")
sys.stdout.flush()
os._exit(0)
