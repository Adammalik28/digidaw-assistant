"""
Real commands the user said (tests/golden_commands.json, from the log by make_golden.py) must still be understood the
same way: a change that makes Digidaw do something else with them fails here (like "jam" opening the clock app
instead of telling the time). After an intended change: run make_golden.py again and read the diff.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/test_golden.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import golden  # noqa: E402


def test_golden():
    with open(os.path.join(os.path.dirname(__file__), "golden_commands.json"), encoding="utf-8") as f:
        cases = json.load(f)
    wrong = []
    for case in cases:
        got = golden.decide(case["said"])
        expected = (case["tool"], case["args"]) if case["tool"] else None
        if (list(got) if got else None) != (list(expected) if expected else None):
            wrong.append(f'"{case["said"]}": dulu {expected}, sekarang {got}')
    assert not wrong, f"{len(wrong)} dari {len(cases)} perintah berubah:\n  " + "\n  ".join(wrong[:40])
    return len(cases)


if __name__ == "__main__":
    try:
        n = test_golden()
        print(f"✓ {n} perintah asli tetap dipahami sama\nsemua lulus")
        code = 0
    except AssertionError as e:
        print(f"✗ {e}\nADA YANG GAGAL")
        code = 1
    sys.stdout.flush()
    os._exit(code)
