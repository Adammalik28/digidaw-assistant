"""
Talking to systemd when Digidaw runs as digidaw.service (settings.install_service): "ready", then a heartbeat while
it is healthy. When the heartbeat stops (Digidaw hangs), systemd restarts it after WatchdogSec; a crash restarts it
anyway (Restart=on-failure). Started any other way (a terminal, the app menu), all of this does nothing.
"""
import os
import socket
import time


def notify(message: str) -> bool:
    """sd_notify(): send one message ("READY=1", "WATCHDOG=1", "STATUS=…") to systemd. False when not under it."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]               # an abstract socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(address)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def heartbeat(alive, stop=None):
    """Runs on its own thread: "READY=1", then "WATCHDOG=1" every WATCHDOG_USEC/3 while alive(max_silence) is true.
    stop: an Event that ends the loop (tests)."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not os.environ.get("NOTIFY_SOCKET") or not usec \
            or os.environ.get("WATCHDOG_PID", str(os.getpid())) != str(os.getpid()):
        return
    notify("READY=1")
    every = max(0.05, int(usec) / 1e6 / 3)
    while not (stop is not None and stop.is_set()):
        if alive(max_silence=every * 2.5):
            notify("WATCHDOG=1")
        time.sleep(every)
