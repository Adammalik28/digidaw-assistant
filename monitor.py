"""Proactive monitor: DIGIDAW speaks up on his own when the system needs attention."""
import os
import time
import threading

import psutil

import config
import oscompat

T = config.USER_TITLE
BATTERY_LEVELS = (5, 10, 20)
COOLDOWN = 600   # seconds between repeated CPU/RAM warnings


def start(announce):
    """announce(text, level) is called for every alert (speaks + shows on the HUD)."""
    if config.PROACTIVE_ALERTS:
        threading.Thread(target=_loop, args=(announce,), daemon=True, name="digidaw-monitor").start()


def _top_process(by: str) -> str:
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            if p.info["name"] in ("System Idle Process", "System") or p.pid == os.getpid():
                continue
            if by == "cpu":
                p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            continue
    if by == "cpu":
        time.sleep(1.0)
        scored = []
        for p in procs:
            try:
                scored.append((p.cpu_percent(None), p.info["name"]))
            except psutil.Error:
                continue
    else:
        scored = [(p.info["memory_info"].rss, p.info["name"]) for p in procs if p.info["memory_info"]]
    return max(scored)[1].removesuffix(".exe") if scored else "tidak diketahui"


def _remaining(battery) -> str:
    secs = battery.secsleft
    if secs in (psutil.POWER_TIME_UNKNOWN, psutil.POWER_TIME_UNLIMITED) or secs <= 0:
        return ""
    hours, minutes = divmod(secs // 60, 60)
    return f", perkiraan sisa {hours} jam {minutes} menit" if hours else f", perkiraan sisa {minutes} menit"


def _loop(announce):
    last_plugged = None
    alerted_levels: set[int] = set()
    start = psutil.sensors_battery()
    # already plugged in and (almost) full when Digidaw starts: this charge was announced before
    full_announced = bool(start and start.power_plugged and start.percent >= 95)
    cpu_high = ram_high = 0
    last_cpu_alert = last_ram_alert = 0.0
    disk_alerted = False
    psutil.cpu_percent(None)

    while True:
        time.sleep(config.MONITOR_INTERVAL)
        try:
            battery = psutil.sensors_battery()
            if battery:
                pct = battery.percent
                if last_plugged is not None and battery.power_plugged != last_plugged:
                    if battery.power_plugged:
                        announce(f"Pengisi daya terhubung. Baterai {pct:.0f} persen, {T}.", "info")
                        alerted_levels.clear()
                        full_announced = pct >= 95       # plugged in when already full: nothing to announce
                    else:
                        announce(f"Pengisi daya dicabut. Baterai {pct:.0f} persen{_remaining(battery)}, {T}.", "info")
                        full_announced = False
                last_plugged = battery.power_plugged
                if not battery.power_plugged:
                    for level in BATTERY_LEVELS:
                        if pct <= level and level not in alerted_levels:
                            alerted_levels.update(l for l in BATTERY_LEVELS if l >= level)
                            announce(f"Peringatan, {T}: baterai tinggal {pct:.0f} persen{_remaining(battery)}. "
                                     "Sebaiknya segera sambungkan pengisi daya.", "warning")
                            break
                elif pct >= 100 and not full_announced:
                    full_announced = True
                    announce(f"Baterai sudah terisi penuh, {T}.", "info")

            cpu = psutil.cpu_percent(None)
            cpu_high = cpu_high + 1 if cpu >= 90 else 0
            if cpu_high * config.MONITOR_INTERVAL >= 60 and time.time() - last_cpu_alert > COOLDOWN:
                last_cpu_alert = time.time()
                announce(f"{T}, CPU bekerja {cpu:.0f} persen lebih dari satu menit. "
                         f"Penyebab terbesar: {_top_process('cpu')}.", "warning")

            ram = psutil.virtual_memory().percent
            ram_high = ram_high + 1 if ram >= 92 else 0
            if ram_high * config.MONITOR_INTERVAL >= 40 and time.time() - last_ram_alert > COOLDOWN:
                last_ram_alert = time.time()
                announce(f"{T}, memori hampir penuh ({ram:.0f} persen). "
                         f"Pemakai terbesar: {_top_process('ram')}.", "warning")

            if not disk_alerted:
                free_gb = psutil.disk_usage(oscompat.system_disk()).free / 1024 ** 3
                if free_gb < 5:
                    disk_alerted = True
                    announce(f"{T}, ruang {oscompat.SYSTEM_DISK_NAME} tinggal {free_gb:.1f} GB. "
                             "Saya sarankan menjalankan protokol bersih.", "warning")
        except Exception:
            continue
