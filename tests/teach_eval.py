"""
How well the small model chooses: what really reached it (teaching_cases.py), and how much teaching helps. Nothing is run: only local_router.pick and the check on it.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/teach_eval.py [nama hasil]
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
import golden  # noqa: E402
import local_router as lr  # noqa: E402
import teaching  # noqa: E402
from teaching import TAUGHT as REAL_LABELLED, part  # noqa: E402
from teaching_cases import HOLDOUT_2, HOLDOUT_3, HOLDOUT_4  # noqa: E402


THINGS_SAID = (lr.AI, lr.CHAT, lr.KNOW, lr.ASK, "do_on_screen", "_ask")     # (a job on the screen goes to the online AI first)


def grade(cases, use_teaching):
    """What really happens to a sentence: a fast route first; only the rest reaches the model. A wrong tool is the bad result: asking back or handing over only costs time."""
    good, wrong, actions, times, routed = 0, [], 0, [], 0
    for text, expect in cases:
        want = {{"AI": lr.AI, "CHAT": lr.CHAT, "KNOW": lr.KNOW, "ASK": lr.ASK}.get(w, w) for w in expect.split("|")}
        decided = golden.decide(text)
        if decided:
            routed += 1
            choice = lr.CHAT if decided[0] == "small_talk" else decided[0]      # (answered by rule: small talk all the same)
        else:
            similar = teaching.similar(text, only="ajar", leave_out=text) if use_teaching else None
            started = time.time()
            choice = lr.settle(lr.pick(text, None, similar), text, "", online=True)
            times.append(time.time() - started)
        if choice in want:
            good += 1
        else:
            wrong.append((text, expect, choice))
            if choice not in THINGS_SAID:
                actions += 1
    times.sort()
    return good, len(cases), wrong, actions, routed, times[len(times) // 2] if times else 0


def main(label):
    teach_on = os.environ.get("TEACH", "1") == "1"
    uji = [c for c in REAL_LABELLED if part(c[0]) == "uji"]
    ajar = [c for c in REAL_LABELLED if part(c[0]) == "ajar"]
    report = {}
    for name, cases in (("nyata-uji", uji), ("nyata-ajar", ajar), ("baru", HOLDOUT_2), ("baru-3", HOLDOUT_3), ("baru-4", HOLDOUT_4)):
        good, total, wrong, actions, routed, median = grade(cases, teach_on)
        report[name] = {"benar": good, "dari": total, "tindakan_salah": actions, "rute_cepat": routed, "salah": wrong, "median": round(median, 2)}
        print(f"{name:12s} {good}/{total} ({100 * good // total}%), tindakan salah {actions}, lewat rute cepat {routed}, median {median:.2f} dtk", flush=True)
        for text, expect, choice in wrong:
            print(f"   {text[:42]:42s} butuh {expect[:30]:30s} dapat {choice}")
    with open(os.path.join(os.path.dirname(__file__), "..", "logs", f"teach_eval_{label}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=0)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "x")
