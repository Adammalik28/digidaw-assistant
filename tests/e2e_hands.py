"""
Do Digidaw's hands really work on this desktop? Opens a small test window, runs the real screen job with a pretend AI
(nothing goes online, nothing outside the test window is touched) and checks what the window received: click at the
right spot, right click, double click, wheel, a key; then text pasted into a real terminal. Restores the pointer, the
focus and the clipboard. Takes ~15 s.

Not part of tests/run_all.sh: it needs the desktop and moves the pointer. Worth running after a Hyprland update.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/e2e_hands.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TITLE = "digidaw-uji-tangan"


def window(record: str):
    """The test window: writes every mouse and key event it receives to a file."""
    import pygame
    pygame.init()
    screen = pygame.display.set_mode((400, 300))
    pygame.display.set_caption(TITLE)
    screen.fill((20, 60, 90))
    out = open(record, "a", buffering=1)
    end = time.time() + 30
    while time.time() < end:
        for ev in pygame.event.get():
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 2, 3):
                out.write(json.dumps({"event": "button", "button": ev.button, "pos": ev.pos}) + "\n")
            elif ev.type == pygame.MOUSEWHEEL:
                out.write(json.dumps({"event": "wheel", "y": ev.y}) + "\n")
            elif ev.type == pygame.KEYDOWN:
                ctrl = bool((ev.mod | pygame.key.get_mods()) & pygame.KMOD_CTRL)
                out.write(json.dumps({"event": "key", "key": pygame.key.name(ev.key), "ctrl": ctrl}) + "\n")
            elif ev.type == pygame.QUIT:
                end = 0
        pygame.display.flip()
        time.sleep(0.01)


def _client() -> dict | None:
    import linux_desktop as ld
    return next((c for c in json.loads(ld.out(["hyprctl", "clients", "-j"]) or "[]") if c.get("title") == TITLE), None)


def _open(command: str) -> dict | None:
    """A test window, floating in the middle, once it is up and in front (and any earlier one is really gone)."""
    import linux_desktop as ld
    for _ in range(100):
        if not _client():
            break
        time.sleep(0.05)
    ld.run(["hyprctl", "dispatch", "exec", command])
    for _ in range(100):
        client = _client()
        if client and client.get("mapped") and client.get("size", [0])[0] > 0:
            time.sleep(0.8)                          # (let it take the focus and settle)
            return _client()
        time.sleep(0.05)
    return None


def _in_front(client: dict) -> bool:
    import linux_desktop as ld
    return json.loads(ld.out(["hyprctl", "activewindow", "-j"]) or "{}").get("address") == client["address"]


def typing() -> bool:
    """Pasting text and Enter, in a real terminal (the same keys a real app gets; pygame can't read this Ctrl)."""
    import linux_desktop as ld
    import system_control as sc
    terminal = shutil.which("kitty")
    if not terminal:
        print("· tidak ada kitty: mengetik tidak diuji")
        return True
    out = Path(tempfile.mkdtemp()) / "typed.txt"
    before_active = json.loads(ld.out(["hyprctl", "activewindow", "-j"]) or "{}").get("address")
    previous = ld.clipboard_get()
    client = _open(f"[float;size 500 200;center] {terminal} --title {TITLE} sh -c 'cat > {out}'")
    if not client:
        print("✗ terminal uji tidak muncul")
        return False
    try:
        if not _in_front(client):
            print("✗ terminal uji tidak di depan, tidak ada yang diketik")
            return False
        sc.type_text("uji tangan digidaw", True)
        time.sleep(0.5)
    finally:
        ld.dispatch("closewindow", f"address:{client['address']}")
        if before_active:
            ld.dispatch("focuswindow", f"address:{before_active}")
    good = out.exists() and out.read_text().strip() == "uji tangan digidaw"
    restored = ld.clipboard_get() == previous
    print(("✓ " if good else "✗ ") + f"teks tertempel + Enter (terminal menerima: {out.read_text().strip() if out.exists() else None!r})")
    print(("✓ " if restored else "✗ ") + "clipboard kembali seperti semula")
    return good and restored


def main(driver: str) -> bool:
    import linux_desktop as ld
    import metrics
    import pc_control
    import screen_agent
    metrics.DIR = Path(tempfile.mkdtemp())
    record = Path(tempfile.mkdtemp()) / "events.jsonl"
    record.touch()
    before_active = json.loads(ld.out(["hyprctl", "activewindow", "-j"]) or "{}").get("address")
    before_pointer = [p.strip() for p in ld.out(["hyprctl", "cursorpos"]).split(",")]
    env = f"SDL_AUDIODRIVER=dummy PYGAME_HIDE_SUPPORT_PROMPT=1 SDL_VIDEODRIVER={driver}"
    client = _open(f"[float;size 400 300;center] env {env} {sys.executable} {os.path.abspath(__file__)} window {record}")
    if not client:
        print(f"✗ jendela uji ({driver}) tidak muncul")
        return False
    address = client["address"]
    try:
        monitor = pc_control.focused_monitor()
        if client["workspace"]["id"] != monitor["activeWorkspace"]["id"] or not _in_front(client) or not client.get("floating"):
            print("✗ jendela uji tidak di depan, tidak ada yang diklik")
            return False
        (wx, wy), (ww, wh) = client["at"], client["size"]

        def at(px: int, py: int) -> dict:
            return {"x": (wx + px - monitor["x"]) / monitor["width"] * 1000, "y": (wy + py - monitor["y"]) / monitor["height"] * 1000}
        steps = [{"aksi": "klik", **at(100, 80), "langkah": "klik kiri"},
                 {"aksi": "klik_kanan", **at(ww // 2, wh // 2), "langkah": "klik kanan"},
                 {"aksi": "klik_dobel", **at(300, 220), "langkah": "klik dobel"},
                 {"aksi": "gulir", "jumlah": -2, "langkah": "gulir"},
                 {"aksi": "tekan", "teks": "f5", "langkah": "tekan tombol"},
                 {"aksi": "selesai", "langkah": "", "teks": "beres"}]
        screen_agent._decide = lambda task, history, image: steps.pop(0)
        screen_agent._aim = lambda step: None        # no AI at all: nothing leaves this PC
        screen_agent.SETTLE = 0.25
        ending = screen_agent._run("uji tangan")
        time.sleep(0.5)
    finally:
        ld.dispatch("closewindow", f"address:{address}")
        if len(before_pointer) == 2:
            ld.dispatch("movecursor", *before_pointer)
        if before_active:
            ld.dispatch("focuswindow", f"address:{before_active}")
    seen = [json.loads(line) for line in record.read_text().splitlines()]
    print(f"── {driver} ──  hasil tugas: {ending}")
    print("diterima jendela:", seen)
    near = lambda pos, x, y: abs(pos[0] - x) <= 3 and abs(pos[1] - y) <= 3
    buttons = [e for e in seen if e["event"] == "button"]
    keys = [e for e in seen if e["event"] == "key"]
    checks = {
        "klik kiri di titik yang tepat": len(buttons) >= 1 and buttons[0]["button"] == 1 and near(buttons[0]["pos"], 100, 80),
        "klik kanan di titik yang tepat": len(buttons) >= 2 and buttons[1]["button"] == 3 and near(buttons[1]["pos"], ww // 2, wh // 2),
        "klik dobel (dua kali)": len(buttons) == 4 and buttons[2]["button"] == buttons[3]["button"] == 1 and near(buttons[2]["pos"], 300, 220),
        "gulir ke bawah": any(e["event"] == "wheel" and e["y"] < 0 for e in seen),
        "tombol": any(e["key"] == "f5" for e in keys),
    }
    for name, good in checks.items():
        print(("✓ " if good else "✗ ") + name)
    return all(checks.values())


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "window":
        window(sys.argv[2])
        sys.exit(0)
    if not shutil.which("hyprctl"):
        print("butuh Hyprland")
        sys.exit(1)
    passed = all([main("wayland"), main("x11"), typing()])       # apps come in both kinds
    print("semua lulus" if passed else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if passed else 1)
