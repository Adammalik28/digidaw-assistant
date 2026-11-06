"""
For the next round of teaching: what really reached the local brain and isn't in teaching.py yet, with what it did. Label the ones that matter and add them to teaching.TAUGHT,
then measure with tests/teach_eval.py. Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=.:tests ~/.local/share/digidaw/venv/bin/python scripts/teach_review.py
"""
import re

import brain
import core
import golden
import routines
import teaching

KNOWN = {t for t, _ in teaching.TAUGHT}


def main():
    lines = []
    for path in ("logs/digidaw.log.old", "logs/digidaw.log"):
        try:
            lines += open(path, errors="replace").read().split("\n")
        except OSError:
            pass
    found, current = {}, None
    for line in lines:
        said = re.search(r"(?:Anda: (.*?)  \(dikenali|You \((?:HUD|HP)\): (.*)$)", line)
        if said:
            current = (said[1] or said[2]).lower().strip(" .,!?")
            continue
        picked = re.search(r"pilihan otak lokal: (\w+)", line)
        if picked and current:
            found.setdefault(current, picked[1])
    for text, choice in found.items():
        if text in KNOWN or len(text) < 2 or golden.decide(text) or core.detect_wake(text)[0] or routines.RUN_RE.fullmatch(text) or brain.ALARM_WORDS.search(text):
            continue
        print(f"{text[:60]:60s} {choice}")


if __name__ == "__main__":
    main()
