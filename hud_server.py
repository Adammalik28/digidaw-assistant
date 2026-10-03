"""
Holographic HUD server.

Serves hud/index.html on http://127.0.0.1:<HUD_PORT> and streams live events to it with
Server-Sent Events; the page can send typed commands and button actions back.

Security: bound to localhost only, the Host header is checked (blocks DNS-rebinding), and every
API call needs a random per-session token that is only embedded in the page itself, so other
websites open in your browser cannot send commands to DIGIDAW
"""
import json
import time
import queue
import secrets
import threading
import os
import subprocess
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import psutil

import config
import events
import oscompat

TOKEN = secrets.token_urlsafe(24)
HUD_FILE = config.BASE_DIR / "hud" / "index.html"
ALLOWED_HOSTS = {f"127.0.0.1:{config.HUD_PORT}", f"localhost:{config.HUD_PORT}"}

_handlers = {"command": None, "action": None}
_latest = {"stats": {}, "media": {}, "weather": {}}
_server = None


def url() -> str:
    return f"http://127.0.0.1:{config.HUD_PORT}/"


# ───────────────────────────── live data ─────────────────────────────

_net_prev = None


def _stats() -> dict:
    global _net_prev
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(oscompat.system_disk())
    battery = psutil.sensors_battery()
    net = psutil.net_io_counters()
    now = time.time()
    down = up = 0.0
    if _net_prev:
        dt = max(now - _net_prev[0], 0.1)
        down = (net.bytes_recv - _net_prev[1]) / dt / 1024
        up = (net.bytes_sent - _net_prev[2]) / dt / 1024
    _net_prev = (now, net.bytes_recv, net.bytes_sent)
    return {
        "cpu": psutil.cpu_percent(None), "ram": mem.percent,
        "ram_used": round(mem.used / 1024 ** 3, 1), "ram_total": round(mem.total / 1024 ** 3, 1),
        "disk": disk.percent, "disk_free": round(disk.free / 1024 ** 3),
        "battery": battery.percent if battery else None,
        "plugged": battery.power_plugged if battery else None,
        "down": round(down, 1), "up": round(up, 1),
        "uptime": int(now - psutil.boot_time()), "processes": len(psutil.pids()),
    }


def _broadcast_loop():
    import media
    import ai_skills
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    tick = 0
    last_weather = 0.0
    psutil.cpu_percent(None)
    while True:
        time.sleep(2)
        tick += 1
        try:
            _latest["stats"] = _stats()
            if events.has_subscribers():
                events.emit("stats", **_latest["stats"])
            if tick % 2 == 0 and events.has_subscribers():
                snap = media.snapshot()
                if snap != _latest["media"]:
                    _latest["media"] = snap
                    events.emit("media", **snap)
            if time.time() - last_weather > 1800 or (not _latest["weather"] and time.time() - last_weather > 120):
                last_weather = time.time()
                data = ai_skills.weather_data()
                if data:
                    _latest["weather"] = data
                    events.emit("weather", **data)
        except Exception:
            continue


LOCATIONS = ("projects", "generate", "images", "files", "notes", "screenshots", "focus")


def _location(key: str):
    import project_builder
    import system_control
    import generator
    if key == "projects":
        return project_builder._projects_root()
    if key == "generate":
        return generator.output_root()
    if key in ("images", "files"):
        folder = generator.output_root() / generator.KIND_FOLDER["image" if key == "images" else "file"]
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    if key == "notes":
        return system_control._notes_dir()
    if key == "screenshots":
        from pathlib import Path
        return Path(config.SCREENSHOT_DIR) if config.SCREENSHOT_DIR else system_control._known_folder("pictures") / "Screenshots"
    if key == "focus":
        return system_control._focus_project_dir()
    return None


def workspace() -> dict:
    """Everything the HUD's Lokasi / Project / Generate / Keamanan panels show."""
    import time as _time
    import project_builder
    import generator
    import security_scan
    import project_versions
    out = {}
    try:
        projects = project_builder.all_projects()
        running = {f for f, p in project_builder._servers.items() if p.poll() is None}
        out["projects"] = [{"name": p.get("name") or os.path.basename(p["folder"]), "folder": p["folder"],
                            "url": p.get("url") or "", "status": p.get("status") or "", "updated": p.get("updated") or 0,
                            "running": p["folder"] in running, "versions": len(project_versions.load(p["folder"]))}
                           for p in projects[:6]]
        out["project_count"] = len(projects)
    except Exception:
        out["projects"], out["project_count"] = [], 0
    locations = {}
    for key in ("projects", "generate", "notes", "screenshots", "focus"):
        try:
            path = _location(key)
            locations[key] = str(path) if path else ""
        except Exception:
            locations[key] = ""
    out["locations"] = locations
    try:
        out["generate"] = generator.snapshot() | {"counts": generator.counts()}
    except Exception:
        out["generate"] = {"jobs": [], "results": [], "counts": {}}
    state = security_scan._load_state()
    days = config.SECURITY_AUTO_SCAN_DAYS
    out["security"] = {"score": state.get("score"), "last_scan": state.get("last_scan") or 0,
                       "issues": len(state.get("keys") or []), "running": security_scan._state["running"],
                       "auto_days": days, "download_guard": config.DOWNLOAD_GUARD,
                       "next_scan": (state.get("last_scan") or _time.time()) + days * 86400 if days else 0,
                       "has_report": bool(state.get("report"))}
    return out


def snapshot() -> dict:
    import system_control
    return {
        "state": events.state, "history": list(events.history), "stats": _latest["stats"],
        "media": _latest["media"], "weather": _latest["weather"],
        "timers": system_control.timers_snapshot(), "user_title": config.USER_TITLE,
        "build": __import__("project_builder").snapshot(),
        "workspace": workspace(),
        "hotkey": config.PUSH_TO_TALK_HOTKEY.upper(),
        "ai": __import__("llm").health(),
        "esc_to_talk": config.ESC_TO_TALK,
    }


# ───────────────────────────── HTTP handler ─────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "DIGIDAW-HUD"

    def log_message(self, *args):
        pass

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS

    def _token_ok(self, query: dict | None = None) -> bool:
        token = self.headers.get("X-Digidaw-Token") or (query or {}).get("token", [""])[0]
        return secrets.compare_digest(token, TOKEN)

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json")

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            page = HUD_FILE.read_text(encoding="utf-8").replace("__DIGIDAW_TOKEN__", TOKEN)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if not self._token_ok(query):
            return self._json(401, {"error": "unauthorized"})
        if parsed.path == "/api/events":
            return self._stream()
        if parsed.path == "/api/state":
            return self._json(200, snapshot())
        if parsed.path == "/api/settings":
            import settings
            return self._json(200, {"fields": settings.schema(), "base_dir": str(config.BASE_DIR)})
        if parsed.path == "/api/workspace":
            return self._json(200, workspace())
        if parsed.path == "/api/generated-file":
            import generator
            item = generator.find_result(query.get("id", [""])[0])
            if not item or not item.get("is_image") or not os.path.isfile(item["path"]):
                return self._json(404, {"error": "not found"})
            ext = os.path.splitext(item["path"])[1].lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                    "gif": "image/gif", "bmp": "image/bmp"}.get(ext, "application/octet-stream")
            with open(item["path"], "rb") as f:
                return self._send(200, f.read(), mime)
        if parsed.path == "/api/project-preview":
            import project_builder
            job = project_builder._jobs.get(query.get("id", [""])[0])
            image = project_builder.preview_path(job["folder"]) if job else None
            if not image or not image.is_file():
                return self._json(404, {"error": "no preview"})
            return self._send(200, image.read_bytes(), "image/png")
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._token_ok():
            return self._json(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20_000:
            return self._json(413, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if self.path == "/api/command":
            text = str(data.get("text", "")).strip()[:2000]
            if text and _handlers["command"]:
                _handlers["command"](text, "hud")
            return self._json(200, {"ok": True})
        if self.path == "/api/action":
            action = str(data.get("action", ""))[:80]
            if action.startswith("open_location:"):
                path = _location(action.split(":", 1)[1]) if action.split(":", 1)[1] in LOCATIONS else None
                if path:
                    os.makedirs(path, exist_ok=True)
                    oscompat.open_folder(path)
                return self._json(200, {"ok": bool(path)})
            if action.startswith(("open_result:", "reveal_result:")):
                import generator
                item = generator.find_result(action.split(":", 1)[1])
                if item and os.path.exists(item["path"]):
                    if action.startswith("open_result:"):
                        oscompat.open_path(item["path"])
                    else:
                        oscompat.reveal(item["path"])
                return self._json(200, {"ok": bool(item)})
            if action in ("open_logs", "open_folder"):
                folder = config.LOG_DIR if action == "open_logs" else config.BASE_DIR
                folder.mkdir(exist_ok=True)
                oscompat.open_folder(folder)
            elif _handlers["action"]:
                threading.Thread(target=_handlers["action"], args=(action,), daemon=True).start()
            return self._json(200, {"ok": True})
        if self.path == "/api/settings":
            import settings
            try:
                result = settings.save(data.get("values") or {})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            events.emit("settings", changed=result["changed"])
            return self._json(200, {"ok": True, **result, "fields": settings.schema()})
        if self.path == "/api/wake-alias":
            import settings
            alias = str(data.get("alias", "")).strip()[:30]
            if not alias:
                return self._json(400, {"error": "alias kosong"})
            aliases = settings.add_wake_alias(alias)
            events.emit("settings", changed=["WAKE_ALIASES"])
            return self._json(200, {"ok": True, "aliases": aliases})
        if self.path == "/api/pick-folder":
            import settings
            return self._json(200, {"path": settings.pick_folder(str(data.get("initial") or ""))})
        self._json(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = events.subscribe()
        try:
            self._event({"type": "hello", **snapshot()})
            while True:
                try:
                    self._event(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (OSError, ValueError):
            pass   # browser closed / reconnecting
        finally:
            events.unsubscribe(q)

    def _event(self, event: dict):
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows' SO_REUSEADDR lets a second program steal the port, so it stays off there. On Linux it only lets
    # Digidaw rebind right after a restart (the old connections linger in TIME_WAIT for up to a minute).
    allow_reuse_address = os.name != "nt"


def start(on_command, on_action) -> bool:
    """Start the HUD server in the background. Returns False if the port is taken."""
    global _server
    _handlers["command"], _handlers["action"] = on_command, on_action
    try:
        _server = _Server(("127.0.0.1", config.HUD_PORT), _Handler)
    except OSError:
        return False
    threading.Thread(target=_server.serve_forever, daemon=True, name="hud-server").start()
    threading.Thread(target=_broadcast_loop, daemon=True, name="hud-broadcast").start()
    return True


def running_elsewhere() -> bool:
    """Is another Digidaw already serving the HUD on this port?"""
    import urllib.request
    try:
        with urllib.request.urlopen(url(), timeout=1.5) as r:
            return b"DIGIDAW HUD" in r.read(4000)
    except Exception:
        return False


def _hud_window():
    import window_manager
    return next((w for w in window_manager._windows() if w["title"].startswith("DIGIDAW HUD")), None)


def open_window():
    """Show the HUD: focus the existing window, or open it as a borderless app window."""
    import media
    import window_manager
    existing = _hud_window()
    if existing:
        window_manager._force_foreground(existing["hwnd"])
        return
    exe = media.browser_exe()
    if exe:
        subprocess.Popen([exe, f"--app={url()}", "--window-size=1440,900", "--start-maximized"],
                         **oscompat.detached())
    else:
        webbrowser.open(url())


def show_hud() -> str:
    """Open / show the DIGIDAW holographic HUD dashboard window ('buka HUD', 'tampilkan Digidaw')."""
    if not _server:
        return f"HUD sedang nonaktif, {config.USER_TITLE}."
    existed = _hud_window() is not None
    open_window()
    return f"{'Menampilkan' if existed else 'Membuka'} HUD, {config.USER_TITLE}."


def hide_hud() -> str:
    """Minimize the DIGIDAW HUD window ('sembunyikan HUD', 'tutup HUD'). Digidaw keeps listening."""
    import window_manager
    window = _hud_window()
    if not window:
        return f"HUD sedang tidak terbuka, {config.USER_TITLE}."
    window_manager.minimize(window["hwnd"])
    return f"HUD saya sembunyikan, {config.USER_TITLE}. Saya tetap mendengarkan."


AI_TOOLS = [show_hud, hide_hud]
