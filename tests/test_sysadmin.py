"""Root commands and the Arch update (sysadmin.py). Nothing here runs as root or changes the system: pkexec, pacman and friends are stand-ins."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import brain
import config
import safety
import sysadmin
import system_control


class FakeProc:
    def __init__(self, lines=(), code=0):
        self.stdout, self.code, self.killed = iter(lines), code, False
        self.text = "".join(lines)
        self.returncode = code

    def communicate(self, timeout=None):
        return self.text, None

    def wait(self):
        return self.code

    def kill(self):
        self.killed = True


class Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


said, started = [], []


def setup(proc=None):
    said.clear()
    started.clear()
    safety._pending = None
    sysadmin.ANNOUNCE = lambda text, level="info": said.append((text, level))
    sysadmin._update.update(running=False, done=0, total=0, phase="", last="", started=time.time())
    sysadmin._unit_active = lambda: False
    sysadmin.UPDATE_LOG = Path(tempfile.gettempdir()) / f"ddw-tes-update-{os.getpid()}.log"
    if proc is not None:
        sysadmin.subprocess.Popen = lambda argv, **k: started.append(argv) or proc


def test_a_sudo_sentence_waits_for_yes():
    setup(FakeProc())
    reply = brain.fast_route("sudo systemctl restart Bluetooth.service")
    assert not started, "nothing runs before the answer"
    assert safety.has_pending() and "systemctl restart Bluetooth.service" in reply and "Yakin" in reply, reply
    safety.cancel()
    for sentence in ("jalankan sudo whoami", "Sudo whoami.", "tolong jalankan perintah pseudo whoami"):
        reply = brain.fast_route(sentence)
        assert safety.has_pending() and "root: whoami." in reply, (sentence, reply)
        safety.cancel()


def test_it_always_asks():
    setup(FakeProc())
    kept, config.CONFIRM_DANGEROUS_ACTIONS = config.CONFIRM_DANGEROUS_ACTIONS, False
    try:
        sysadmin.run_as_root("whoami")
        assert safety.has_pending() and not started, "also with confirmations off, also when an AI chose the command"
    finally:
        config.CONFIRM_DANGEROUS_ACTIONS = kept
        safety.cancel()


def test_the_disk_is_not_for_voice():
    setup(FakeProc())
    for command in ("rm -rf /", "rm -rf /*", "rm -rf /home", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/nvme0n1", "fdisk /dev/sda", "chmod -R 777 /"):
        reply = sysadmin.run_as_root(command)
        assert not safety.has_pending() and "tidak saya jalankan" in reply.lower(), command
    assert "Perintahnya apa" in sysadmin.run_as_root("sudo   ")
    sysadmin.run_as_root("rm -rf /tmp/sisa")
    assert safety.has_pending(), "an ordinary folder only asks"
    safety.cancel()


def test_the_command_runs_after_yes_and_the_result_is_said():
    proc = FakeProc([f"root\n{sysadmin.MARK}0\n"])
    setup(proc)
    sysadmin.run_as_root("whoami")
    reply = safety.confirm()
    assert "kata sandi" in reply
    for _ in range(100):
        if said:
            break
        time.sleep(0.01)
    assert started[0][:3] == ["pkexec", "bash", "-c"] and started[0][3].startswith("whoami\n"), started
    assert said[0][0].startswith("Perintah root selesai") and "root" in said[0][0], said


def test_root_results():
    setup(FakeProc([f"error: target not found: htopp\n{sysadmin.MARK}1\n"], 0))
    sysadmin._root_job("pacman -S htopp --noconfirm")
    assert "gagal, kode 1" in said[0][0] and "target not found" in said[0][0] and said[0][1] == "warning", said
    setup(FakeProc(["Error executing command as another user: Request dismissed\n"], 126))
    sysadmin._root_job("whoami")
    assert "tidak diisi" in said[0][0], said
    setup(FakeProc([f"bash: nothing: command not found\n{sysadmin.MARK}127\n"], 0))
    sysadmin._root_job("nothing")
    assert "gagal, kode 127" in said[0][0], "127 from inside isn't a refused password"
    setup(FakeProc([f"{'x' * 900}\n{sysadmin.MARK}0\n"]))
    sysadmin._root_job("cat besar")
    assert "x" * 100 not in said[0][0], "a long output isn't read aloud"


def test_pacman_doesnt_wait_for_an_answer():
    assert sysadmin._unattended("pacman -S htop") == "pacman -S htop --noconfirm"
    assert sysadmin._unattended("pacman -Rns htop --noconfirm") == "pacman -Rns htop --noconfirm"
    assert sysadmin._unattended("systemctl restart bluetooth") == "systemctl restart bluetooth"


def test_run_command_hands_sudo_over():
    setup(FakeProc())
    reply = system_control.run_command("sudo systemctl restart bluetooth")
    assert "root: systemctl restart bluetooth" in safety.pending_description(), reply
    safety.cancel()
    assert system_control.ACCESS_DENIED_RE.search("sudo: a terminal is required to read the password")


def _with_waiting(names, aur="", code=0, plugged=True, percent=90):
    class Battery:
        power_plugged, percent_ = plugged, percent

    Battery.percent = percent
    kept = sysadmin.ld.run, sysadmin.psutil.sensors_battery, sysadmin.shutil.which
    sysadmin.ld.run = lambda args, timeout=5, input=None: Done("".join(f"{n} 1 -> 2\n" for n in names), code) if args[0] == "checkupdates" else Done(aur)
    sysadmin.psutil.sensors_battery = lambda: Battery
    sysadmin.shutil.which = lambda name: f"/usr/bin/{name}"
    try:
        return sysadmin.system_update()
    finally:
        sysadmin.ld.run, sysadmin.psutil.sensors_battery, sysadmin.shutil.which = kept


def test_update_asks_with_the_numbers():
    setup(FakeProc())
    assert "sudah terbaru" in _with_waiting([], code=2)
    assert "AUR" in _with_waiting([], "foo 1 -> 2\n", code=2)
    assert "tidak bisa diambil" in _with_waiting([], code=1), "offline isn't \"up to date\""
    assert "charger" in _with_waiting(["vim"], plugged=False, percent=20) and not safety.has_pending()
    reply = _with_waiting(["vim", "firefox"], plugged=False, percent=80)
    assert safety.has_pending() and "2 pembaruan" in safety.question() and "restart" not in safety.question(), reply
    safety.cancel()
    _with_waiting(["vim", "linux-zen"])
    assert "perlu restart" in safety.question()
    safety.cancel()
    assert not started, "nothing was installed by asking"


# (as pacman really writes it without a terminal: 6 Nov, /var/log of a real update)
LINES = [":: Synchronizing package databases...\n", "Package (2)   Old Version  New Version\n", "Total Download Size:   3999.20 MiB\n", ":: Retrieving packages...\n", " vim-1-x86_64 downloading...\n", "upgrading vim...\n",
         "warning: /etc/pacman.conf installed as /etc/pacman.conf.pacnew\n", "upgrading zsh...\n", ":: Running post-transaction hooks...\n", "( 1/25) Arming ConditionNeedsUpdate...\n"]


def _run_update(lines, code=0, aur=0, unit=None):
    """The job with a stand-in pkexec: "authorised", it starts the unit, which writes these lines to its log."""
    setup()
    sysadmin.POLL_SECONDS = 0.01

    def popen(argv, **k):
        started.append(argv)
        with open(sysadmin.UPDATE_LOG, "a") as log:
            log.writelines(lines)
        return FakeProc([], code)

    sysadmin.subprocess.Popen = popen
    sysadmin.UPDATE_LOG.unlink(missing_ok=True)
    sysadmin._update.update(running=True, started=time.time() - 300)
    sysadmin._update_job(aur)


def test_update_runs_as_a_system_unit_and_reports():
    sysadmin.needs_restart = lambda: True
    _run_update(LINES + [f"{sysadmin.MARK}0\n"], aur=17)
    argv = started[0]
    assert argv[:2] == ["pkexec", "systemd-run"] and f"--unit={sysadmin.UNIT}" in argv and "systemd-inhibit" in argv, argv
    assert "--pipe" not in argv and "--wait" not in argv, "the unit reads and needs nobody: Digidaw may restart meanwhile"
    assert "--noconfirm" in argv[-1] and "archlinux-keyring" in argv[-1] and str(sysadmin.UPDATE_LOG) in argv[-1] and sysadmin.MARK in argv[-1], argv[-1]
    text = said[0][0]
    assert "2 paket" in text and "5 menit" in text and "Kernel baru" in text and "17 paket AUR" in text and "1 berkas pengaturan" in text, text
    assert not sysadmin._update["running"] and sysadmin.update_status() == text


def test_update_failure_and_no_password():
    _run_update([":: Synchronizing...\n", "error: failed to commit transaction (conflicting files)\n", f"{sysadmin.MARK}1\n"])
    assert "gagal" in said[0][0] and "conflicting files" in said[0][0] and "lewat terminal" in said[0][0] and said[0][1] == "warning", said
    _run_update([], code=126)
    assert "tidak diisi" in said[0][0] and not sysadmin._update["running"], said
    _run_update([], code=1)
    assert "tidak bisa dimulai" in said[0][0] and not sysadmin._update["running"], said


def test_nobody_types_the_password():
    class Waiting(FakeProc):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("pkexec", timeout)

    proc = Waiting()
    setup(proc)
    sysadmin._update_job(0)
    assert proc.killed and "tidak diisi" in said[0][0], said


def test_the_unit_stopped_without_a_word():
    other = Path(tempfile.gettempdir()) / f"ddw-tes-pacman-{os.getpid()}.log"
    sysadmin.PACMAN_LOG = other
    other.write_text("[PACMAN] starting full system upgrade\n")
    _run_update([":: Synchronizing...\n"])
    assert "berhenti tanpa laporan" in said[0][0] and said[0][1] == "warning", said
    other.write_text("[PACMAN] starting full system upgrade\n[ALPM] transaction started\n[ALPM] transaction completed\n")
    sysadmin.needs_restart = lambda: False
    _run_update([":: Synchronizing...\n"])
    assert "selesai" in said[0][0], "pacman's own log says it reached the end"
    other.unlink()


def test_digidaw_comes_back_to_a_running_update():
    setup()
    sysadmin.POLL_SECONDS = 0.01
    sysadmin.needs_restart = lambda: False
    sysadmin.UPDATE_LOG.write_text(f"{sysadmin.START_MARK}lama =====\nupgrading a...\n{sysadmin.MARK}0\n{sysadmin.START_MARK}baru =====\nPackages (350) a b\nupgrading firefox...\nupgrading vim...\n")
    sysadmin._unit_active = lambda: True
    sysadmin.start()
    for _ in range(200):
        if sysadmin._update["done"] == 2:
            break
        time.sleep(0.01)
    assert sysadmin._update["running"] and "2 dari 350" in sysadmin.update_status(), sysadmin._update
    assert not said, "still running: nothing to report yet"
    with open(sysadmin.UPDATE_LOG, "a") as log:
        log.write(f"upgrading zsh...\n{sysadmin.MARK}0\n")
    for _ in range(200):
        if said:
            break
        time.sleep(0.01)
    assert said and "3 paket" in said[0][0], said
    setup()
    sysadmin.start()
    assert not sysadmin._update["running"], "nothing running: nothing to follow"


def test_the_huds_see_the_progress():
    import events
    setup()
    sysadmin._update.update(running=True, phase="mengunduh", size=3999.2, done=0, total=350)
    assert "3,9 GB" in sysadmin.update_status(), sysadmin.update_status()
    for n in range(10):
        sysadmin._note_line(f"upgrading paket{n}...")
    assert events.state["update"] == {"running": True, "done": 10, "total": 350, "phase": "memasang"}
    sysadmin._update["running"] = False


def test_offering_to_install():
    setup(FakeProc())
    kept = sysadmin.shutil.which
    sysadmin.shutil.which = lambda name: f"/usr/bin/{name}"
    try:
        reply = sysadmin.offer_install("wf-recorder", "Perekam layar")
        assert safety.has_pending() and "Perekam layar belum terpasang" in safety.question() and not started, reply
        safety.cancel()
    finally:
        sysadmin.shutil.which = kept


def test_progress_while_it_runs():
    setup()
    sysadmin._update.update(running=True, phase="menunggu kata sandi", total=350)
    assert "menunggu kata sandi" in sysadmin.update_status()
    sysadmin._update["done"] = 119
    sysadmin._note_line("upgrading firefox...")
    assert "120 dari 350" in sysadmin.update_status()
    assert sysadmin.system_update() == sysadmin.update_status(), "a second \"update sistem\" doesn't start another"
    assert "Update" in sysadmin.update_in_terminal() and not started
    sysadmin._note_line("(5/10) checking keys in keyring...")
    sysadmin._note_line("( 5/25) Reloading system manager configuration...")
    sysadmin._note_line(" firefox-1-x86_64 downloading...")
    assert sysadmin._update["done"] == 120, "pacman's checks aren't packages"
    sysadmin._update["running"] = False


def test_no_power_off_during_an_update():
    setup()
    sysadmin._update["running"] = True
    called = []
    kept = system_control.ld.run
    system_control.ld.run = lambda *a, **k: called.append(a)
    try:
        for action in ("shutdown", "restart", "sleep"):
            assert "update" in system_control._do_power_linux(action).lower()
        assert not called, "it would break the system halfway"
    finally:
        system_control.ld.run = kept
        sysadmin._update["running"] = False


def test_routes():
    route = brain._route
    assert route("update sistem") == ("system_update", {})
    assert route("perbarui arch") == ("system_update", {})
    assert route("update lewat terminal") == ("update_in_terminal", {})
    assert route("update aur") == ("update_aur", {})
    assert route("status update") == ("update_status", {})
    assert route("update sudah sampai mana") == ("update_status", {})
    assert route("cek update")[0] == "check_updates"


if __name__ == "__main__":
    real_popen = subprocess.Popen
    ok = True
    for test in [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]:
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as e:
            ok = False
            import traceback
            print(f"✗ {test.__name__}: {e!r}")
            traceback.print_exc()
    print("semua lulus" if ok else "ADA YANG GAGAL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)
