"""The HUD pages against the servers that serve them: what the Python side relies on is still in the page, every element the script looks up exists,
every address it calls is one the server answers, and the buttons that matter are there. No browser, no network, nothing opened."""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUD = ROOT / "hud"
SERVER = (ROOT / "hud_server.py").read_text(encoding="utf-8")
PHONE_SERVER = (ROOT / "mobile_server.py").read_text(encoding="utf-8")


def page(name: str) -> str:
    return (HUD / name).read_text(encoding="utf-8")


def script(html: str) -> str:
    return html[html.index("<script>"):html.rindex("</script>")]


def ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([\w-]+)"', html))


def looked_up(html: str) -> set[str]:
    return set(re.findall(r'\$\("([\w-]+)"\)', script(html)))


def called(html: str) -> set[str]:
    """The addresses the page asks for (a query string or a ${...} is not part of it)."""
    return {re.split(r"[?$]", p)[0] for p in re.findall(r"""["'`](/api/[\w./-]+)""", html)}


def test_the_pc_page_is_recognised():
    html = page("index.html")
    assert b"DIGIDAW HUD" in html.encode()[:4000], "hud_server.running_elsewhere reads the first 4000 bytes"
    assert "<title>DIGIDAW HUD" in html, "the window is found by its title (hud_server._hud_window)"
    assert '"__DIGIDAW_TOKEN__"' in html, "the server puts the session's token here"


def test_every_element_the_script_uses_exists():
    for name in ("index.html", "mobile.html"):
        html = page(name)
        missing = looked_up(html) - ids(html)
        assert not missing, f"{name}: {sorted(missing)}"


def test_every_address_is_answered_by_its_server():
    for name, server in (("index.html", SERVER), ("agenda.html", SERVER), ("mobile.html", PHONE_SERVER)):
        for path in called(page(name)):
            family = path.rsplit("/", 1)[0] + "/"        # (/api/share/link: the server takes everything under /api/share/)
            assert f'"{path}' in server or f'"{family}"' in server, f"{name} calls {path}"


def test_the_pc_page_keeps_every_panel():
    have = ids(page("index.html"))
    for part in ("reactor", "stateLabel", "digidawLine", "cmdInput", "micBtn", "stopBtn", "log", "npToggle", "timers", "agendaList",
                 "lampPower", "secScore", "locList", "gallery", "buildJobs", "projList", "cpOverlay", "alarmBanner", "alarmStop", "curtain", "pauseBtn"):
        assert part in have, part


def test_the_phone_page_keeps_every_control():
    html = page("mobile.html")
    have = ids(html)
    for part in ("pin", "micBtn", "cmd", "vol", "ws", "wins", "fav", "shFile", "shText", "shClip", "shFiles", "screenImg", "pad", "typeText", "agenda", "timers",
                 "addEvent", "addAlarm", "pushBtn", "installBtn", "certGuide", "logoutBtn", "ring", "alarmStop", "alarmSnooze"):
        assert part in have, part
    assert "data-mic" in html and "data-listen" in html
    for tab in ("talk", "screen", "agenda"):             # the shortcuts on the home-screen icon (mobile_server.MANIFEST)
        assert f"/?tab={tab}" in PHONE_SERVER and f'id="tab-{tab}"' in html, tab


def test_an_alarm_is_not_cancelled_by_one_stray_touch():
    phone, pc = page("mobile.html"), page("index.html")
    assert 'kind === "timer" ? cancel() : askSheet(' in phone
    assert re.search(r'kind === "timer" \|\| kind === "action" \|\| confirm\(', pc)


def test_the_phone_can_set_an_alarm_in_words_the_router_understands():
    assert "setel alarm jam ${time.value" in page("mobile.html")


def test_no_private_address_or_secret_in_the_pages():
    for name in ("index.html", "mobile.html", "agenda.html"):
        html = page(name)
        assert not re.search(r"192\.168\.\d+\.\d+:\d+", html), name
        assert "AIza" not in html, name


if __name__ == "__main__":
    ok = True
    for test in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as e:
            ok = False
            print(f"✗ {test.__name__}: {e!r}")
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
