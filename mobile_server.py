"""
The HUD for a phone on the home Wi-Fi: music & volume, the agenda, timers, and a box to type to Digidaw.

Separate from the desktop HUD (which stays on 127.0.0.1) and deliberately small:
- only devices on the local network (private addresses) get an answer at all;
- a PIN opens a session (a cookie kept 30 days, also across Digidaw restarts); 5 wrong PINs lock that
  device out for 10 minutes;
- every change is a POST with a custom header, which a page on another site can't send (no CSRF), and the
  cookie is HttpOnly + SameSite=Strict.
"""
import hashlib
import ipaddress
import os
import ssl
import subprocess
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import config
import events

PAGE = Path(config.BASE_DIR) / "hud" / "mobile.html"
TLS_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "digidaw" / "tls"
MAX_AUDIO = 4 * 1024 * 1024                # a spoken command from the phone (~15 s of opus is far less)
SESSIONS_FILE = Path(config.LOG_DIR) / "mobile_sessions.json"
SESSION_DAYS = 30
COOKIE = "ddw_hp"
MAX_TRIES, LOCK_SECONDS = 5, 600

_server = None
_sessions: dict[str, float] = {}       # sha256(token) -> expiry
_tries: dict[str, list[float]] = {}     # ip -> times of wrong PINs
_lock = threading.Lock()


# ───────────────────────────── helpers ─────────────────────────────

def lan_ip() -> str:
    """This PC's address on the home network (what the phone types in)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))          # no packet is sent: it only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def url() -> str:
    return f"https://{lan_ip()}:{config.MOBILE_HUD_PORT}/"


# ───────────────────────────── HTTPS (phones only allow the microphone on https pages) ─────────────────────────────

def _certificate() -> tuple[Path, Path]:
    """A small local certificate authority (made once, 10 years) and a certificate for this PC's address on the
    home network (made again when the address changes). The phone accepts it once, or installs the CA (/ca.crt)."""
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    ca_key_file, ca_file = TLS_DIR / "ca.key", TLS_DIR / "ca.crt"
    key_file, cert_file = TLS_DIR / "server.key", TLS_DIR / "server.crt"
    now = dt.datetime.now(dt.timezone.utc)

    def write_key(path, key):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        os.chmod(path, 0o600)

    if not (ca_key_file.exists() and ca_file.exists()):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Digidaw Local CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
              .not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                           key_encipherment=False, data_encipherment=False, key_agreement=False,
                                           encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        write_key(ca_key_file, ca_key)
        ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_file.unlink(missing_ok=True)
    ca_key = serialization.load_pem_private_key(ca_key_file.read_bytes(), None)
    ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
    ip = lan_ip()
    if cert_file.exists() and key_file.exists():
        current = x509.load_pem_x509_certificate(cert_file.read_bytes())
        try:
            ips = [str(a) for a in current.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                   .value.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            ips = []
        if ip in ips and current.not_valid_after_utc > now + dt.timedelta(days=30):
            return cert_file, key_file
    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.IPAddress(ipaddress.ip_address(ip)), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Digidaw {ip}")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_key(key_file, key)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


def enabled() -> bool:
    return bool(config.MOBILE_HUD and str(config.MOBILE_PIN).strip())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_sessions():
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        now = time.time()
        _sessions.update({k: v for k, v in data.items() if v > now})
    except (OSError, ValueError):
        pass


def _save_sessions():
    try:
        SESSIONS_FILE.write_text(json.dumps(_sessions), encoding="utf-8")
    except OSError:
        pass


def logout_all():
    """Forget every phone that logged in (after changing the PIN)."""
    with _lock:
        _sessions.clear()
        _save_sessions()


def _state() -> dict:
    import agenda
    import hud_server
    import system_control
    stats = hud_server._latest.get("stats") or {}
    replies = [{"type": e.get("type"), "text": e.get("text", ""), "ts": e.get("ts")}
               for e in list(events.history) if e.get("type") in ("user", "digidaw", "alert")][-8:]
    try:
        volume = system_control.get_volume()
    except Exception:
        volume = None
    return {
        "title": config.USER_TITLE, "status": events.state.get("status", "idle"),
        "media": hud_server._latest.get("media") or {}, "weather": hud_server._latest.get("weather") or {},
        "battery": stats.get("battery"), "plugged": stats.get("plugged"), "volume": volume,
        "agenda": agenda.snapshot(), "timers": system_control.timers_snapshot(), "log": replies,
        # for notifications on the phone: recent alerts (reminders, agenda, battery...) and a ringing alarm
        "alerts": [{"text": e.get("text", ""), "ts": e.get("ts"), "level": e.get("level", "info")}
                   for e in list(events.history) if e.get("type") == "alert"][-5:],
        "alarm": events.state.get("alarm") or {"ringing": False},
    }


# ───────────────────────────── HTTP ─────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "DIGIDAW-HP"

    def log_message(self, *args):
        pass

    def _client_ok(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback

    def _session_ok(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and value:
                with _lock:
                    expiry = _sessions.get(_hash(value), 0)
                return expiry > time.time()
        return False

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 20000)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}

    def do_GET(self):
        if not self._client_ok():
            return self._send(403, b"", "text/plain")
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            if not self._session_ok():
                return self._json(401, {"login": True})
            return self._json(200, _state())
        if path in ("/api/remote", "/api/tts", "/api/screen") and not self._session_ok():
            return self._json(401, {"login": True})
        if path == "/api/screen.mjpg":
            if not self._session_ok():
                return self._json(401, {"login": True})
            if not config.MOBILE_SCREEN:
                return self._json(403, {"error": "Lihat layar dimatikan di Control Panel (HUD di HP)."})
            from urllib.parse import parse_qs
            query = parse_qs(urlparse(self.path).query)
            return self._stream_screen(int((query.get("w") or ["720"])[0] or 720),
                                       float((query.get("fps") or ["8"])[0] or 8))
        if path == "/api/screen":
            if not config.MOBILE_SCREEN:
                return self._json(403, {"error": "Lihat layar dimatikan di Control Panel (HUD di HP)."})
            from urllib.parse import parse_qs
            width = int((parse_qs(urlparse(self.path).query).get("w") or ["720"])[0] or 720)
            _screen_in_use(self.client_address[0], "melihat layar")
            jpeg = _screen_jpeg(width)
            return self._send(200, jpeg, "image/jpeg") if jpeg else self._send(503, b"", "text/plain")
        if path == "/api/remote":
            return self._json(200, _remote_state())
        if path == "/api/tts":
            from urllib.parse import parse_qs
            text = (parse_qs(urlparse(self.path).query).get("text") or [""])[0][:600]
            audio, kind = _voice_audio(text)
            return self._send(200, audio, kind) if audio else self._send(503, b"", "text/plain")
        if path == "/ca.crt":                     # optional: install once on the phone to drop the warning
            return self._send(200, (TLS_DIR / "ca.crt").read_bytes(), "application/x-x509-ca-cert",
                              {"Content-Disposition": 'attachment; filename="digidaw-ca.crt"'})
        if path == "/manifest.json":
            manifest = {"name": "Digidaw", "short_name": "Digidaw", "start_url": "/", "display": "standalone",
                        "background_color": "#030a12", "theme_color": "#030a12"}
            return self._json(200, manifest)
        self._send(404, b"", "text/plain")

    def do_POST(self):
        if not self._client_ok() or self.headers.get("X-Digidaw") != "1":
            return self._send(403, b"", "text/plain")
        path = urlparse(self.path).path
        if path in ("/api/voice", "/api/listen"):
            if not self._session_ok():
                return self._json(401, {"login": True})
            return self._voice() if path == "/api/voice" else self._listen()
        data = self._body()
        if path == "/api/login":
            return self._login(str(data.get("pin", "")))
        if not self._session_ok():
            return self._json(401, {"login": True})
        if path == "/api/logout":
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict"})
        if path == "/api/media":
            return self._json(200, {"result": _media(str(data.get("action", "")), data.get("value"))})
        if path == "/api/command":
            return self._json(200, {"reply": _command(str(data.get("text", ""))[:500], bool(data.get("pc_voice", True)))})
        if path == "/api/window":
            return self._json(200, {"result": _window_action(str(data.get("action", "")), str(data.get("id", "")),
                                                             str(data.get("workspace", "")))})
        if path == "/api/workspace":
            import window_manager
            return self._json(200, {"result": window_manager.switch_workspace(str(data.get("n", "")))})
        if path == "/api/launch":
            import system_control
            return self._json(200, {"result": system_control.open_app_or_web(str(data.get("name", ""))[:80])})
        if path in ("/api/pointer", "/api/key"):
            if not config.MOBILE_SCREEN:
                return self._json(403, {"error": "Kendali layar dimatikan di Control Panel (HUD di HP)."})
            _screen_in_use(self.client_address[0], "mengendalikan mouse/keyboard")
            if path == "/api/pointer":
                return self._json(200, {"ok": _pointer(data)})
            return self._json(200, {"result": _key(data)})
        if path == "/api/alarm":
            import alarms
            if str(data.get("action", "")) == "snooze":
                return self._json(200, {"result": alarms.snooze_alarm(config.ALARM_SNOOZE_MINUTES)})
            return self._json(200, {"result": alarms.stop_alarm()})
        if path == "/api/agenda":
            import agenda
            if data.get("delete"):
                return self._json(200, {"result": agenda.delete_event(str(data["delete"]))})
            return self._json(200, {"result": agenda.add_event(str(data.get("title", ""))[:120], str(data.get("date", "")),
                                                               str(data.get("time", "")))})
        if path == "/api/timer":
            import alarms
            with alarms._lock:
                alarms._items[:] = [i for i in alarms._items if i["id"] != str(data.get("cancel", ""))]
            alarms._changed()
            return self._json(200, {"ok": True})
        self._send(404, b"", "text/plain")

    def _voice(self):
        """A spoken command recorded on the phone: recognised here (Google, then Whisper on this PC) and run."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not 0 < length <= MAX_AUDIO:
            return self._json(400, {"error": "rekaman kosong atau terlalu panjang"})
        audio = self.rfile.read(length)
        started = time.time()
        heard = _transcribe(audio)
        print(f"[HUD HP] suara dikenali dalam {time.time() - started:.1f} dtk: {heard!r}")
        if not heard:
            return self._json(200, {"heard": "", "reply": "Maaf, suaranya tidak terdengar jelas. Coba lagi."})
        pc_voice = self.headers.get("X-PC-Voice", "1") != "0"
        return self._json(200, {"heard": heard, "reply": _command(heard, pc_voice)})

    def _listen(self):
        """Hands-free mode: one phrase the phone heard by itself. Only phrases that call "Digidaw" are acted on
        (checked here with the same local Whisper gate as the PC's own microphone); everything else is dropped.
        X-Armed: 1 = the phone just heard "Digidaw" alone, so this phrase is the command."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not 0 < length <= MAX_AUDIO:
            return self._json(400, {"error": "rekaman kosong atau terlalu panjang"})
        data = _audio_data(self.rfile.read(length))
        if data is None:
            return self._json(200, {"ignored": True})
        pc_voice = self.headers.get("X-PC-Voice", "1") != "0"
        if self.headers.get("X-Armed") == "1":
            heard = _recognize(data)
            if not heard:
                return self._json(200, {"ignored": True})
            return self._json(200, {"heard": heard, "reply": _command(heard, pc_voice, dedupe=True)})
        try:
            import local_stt
            gate = local_stt.transcribe(data, local_stt.GATE_MODEL)
            if not local_stt.mentions_wake(gate):
                return self._json(200, {"ignored": True})
        except Exception:
            pass                                 # no local Whisper: let Google decide
        heard = _recognize(data, prefer_wake=True)
        import core
        addressed, command, loose = core.detect_wake(heard) if heard else (False, "", False)
        if not addressed:
            return self._json(200, {"ignored": True})
        print(f"[HUD HP] dipanggil dari HP: {heard!r}")
        if not command:                          # just "Digidaw": the next phrase is the command
            return self._json(200, {"armed": True, "heard": heard})
        return self._json(200, {"heard": command, "reply": _command(command, pc_voice, dedupe=True)})

    def _stream_screen(self, width: int, fps: float):
        """The screen as a live MJPEG stream (one connection, a new frame whenever the screen changes, up to
        `fps` per second). Far smoother than asking for one picture at a time."""
        with _lock:
            if _streams["count"] >= MAX_STREAMS:
                return self._json(429, {"error": "Terlalu banyak tampilan layar terbuka."})
            _streams["count"] += 1
        _screen_in_use(self.client_address[0], "melihat layar")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ddwframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            interval = 1.0 / max(1.0, min(fps, 15.0))
            last, last_sent, until = b"", 0.0, time.time() + STREAM_MAX_SECONDS
            while time.time() < until and config.MOBILE_SCREEN:
                started = time.time()
                frame = _grim_jpeg(width)
                if frame and (frame != last or started - last_sent > 2.0):   # unchanged frames aren't resent
                    self.wfile.write(b"--ddwframe\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
                    last, last_sent = frame, started
                time.sleep(max(0.0, interval - (time.time() - started)))
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                                 # the phone left the Layar tab
        finally:
            with _lock:
                _streams["count"] -= 1

    def _login(self, pin: str):
        ip = self.client_address[0]
        now = time.time()
        with _lock:
            recent = [t for t in _tries.get(ip, []) if now - t < LOCK_SECONDS]
            _tries[ip] = recent
            if len(recent) >= MAX_TRIES:
                wait = int(LOCK_SECONDS - (now - recent[0]))
                return self._json(429, {"error": f"Terlalu banyak PIN salah. Coba lagi dalam {wait // 60 + 1} menit."})
        if not enabled() or not secrets.compare_digest(pin.strip().encode(), str(config.MOBILE_PIN).strip().encode()):
            with _lock:
                _tries.setdefault(ip, []).append(now)
                left = MAX_TRIES - len(_tries[ip])
            time.sleep(0.6)                        # slows guessing down further
            return self._json(403, {"error": f"PIN salah. Sisa {left} percobaan."})
        token = secrets.token_urlsafe(32)
        with _lock:
            _tries.pop(ip, None)
            _sessions[_hash(token)] = now + SESSION_DAYS * 86400
            _save_sessions()
        cookie = f"{COOKIE}={token}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Strict"
        events.emit("toast", text=f"HP {ip} masuk ke HUD Digidaw.", level="info")
        return self._json(200, {"ok": True}, {"Set-Cookie": cookie})


class _Server(ThreadingHTTPServer):
    """HTTPS; the TLS handshake happens in each request's own thread, so a slow phone never blocks the others."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, context: ssl.SSLContext):
        self.tls = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        request.settimeout(30)
        try:
            request = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return                          # plain http, or the phone hasn't accepted the certificate yet
        self.RequestHandlerClass(request, client_address, self)


# ───────────────────────────── actions ─────────────────────────────

def _media(action: str, value=None) -> str:
    import media
    import system_control as sc
    if action == "volume" and value is not None:
        return sc.set_volume(int(value))
    if action == "volume_up":
        return sc.volume_up(10)
    if action == "volume_down":
        return sc.volume_down(10)
    if action in ("play", "pause", "toggle", "next", "previous", "forward", "rewind"):
        if action == "toggle":
            action = "pause" if (media.snapshot() or {}).get("playing") else "play"
        return media.media_control(action)
    return "aksi tidak dikenal"


def _already_done(text: str) -> str:
    """The PC's own microphone may have heard the same sentence (phone and PC in one room): don't run it twice.
    Returns the earlier answer when the same command (by similarity) just ran or is running, else ''."""
    import difflib
    import hud_server
    core = getattr(hud_server._handlers.get("command"), "__self__", None)
    if core is None:
        return ""
    deadline = time.time() + 3
    while core.busy.is_set() and time.time() < deadline:     # it may be running right now
        time.sleep(0.1)
    last_text, finished = getattr(core, "_last_command", ("", 0.0))
    if time.time() - finished > 8 or not last_text:
        return ""
    if difflib.SequenceMatcher(None, last_text, text.lower().strip()).ratio() < 0.8:
        return ""
    replies = [e.get("text", "") for e in list(events.history) if e.get("type") == "digidaw"]
    return replies[-1] if replies else "Sudah dijalankan."


def _command(text: str, pc_voice: bool = True, dedupe: bool = False) -> str:
    """Run a typed command like the HUD's text box and wait (up to 40 s) for Digidaw's answer.
    pc_voice=False: the PC stays quiet (the phone reads the answer out itself). dedupe: hands-free mode, where
    the PC's own microphone may have heard the same sentence."""
    import hud_server
    text = text.strip()
    if not text or not hud_server._handlers.get("command"):
        return ""
    if dedupe:
        duplicate = _already_done(text)
        if duplicate:
            return duplicate
    sent = time.time()                     # history is a capped deque: find the answer by time, not by position
    done = hud_server._handlers["command"](text, "hp" if pc_voice else "hp-silent")
    deadline = sent + config.COMMAND_TIMEOUT_SECONDS
    # answer the phone as soon as the reply text exists; "done" only comes after the PC has finished speaking it
    while time.time() < deadline:
        replies = [e.get("text", "") for e in list(events.history)
                   if e.get("type") == "digidaw" and e.get("ts", 0) >= sent]
        if replies:
            return replies[-1]
        if hasattr(done, "is_set") and done.is_set():
            break
        time.sleep(0.1)
    return "Perintah dikirim."


# ───────────────────────────── remote (the same actions Digidaw already does by voice) ─────────────────────────────

def _remote_state() -> dict:
    """Open windows, workspaces and the user's favourite apps / sites, for the Remote tab."""
    import habits
    import linux_desktop as ld
    import persona
    import window_manager
    active = ld.active_address()
    windows = [{"id": w["hwnd"], "app": window_manager._app_label(w), "title": w["title"][:80],
                "workspace": w["workspace"], "active": w["hwnd"] == active} for w in window_manager.closable_windows()]
    apps = habits.favourites("app", 8) or ["firefox", "terminal", "file manager"]
    webs = habits.favourites("web", 6) or ["youtube", "tiktok", "instagram"]
    return {"windows": windows, "workspaces": ld.workspace_ids(), "active_workspace": ld.active_workspace_id(),
            "screen": bool(config.MOBILE_SCREEN),
            "favourites": [{"name": n, "label": persona.brand(n), "kind": "app"} for n in apps]
                          + [{"name": n, "label": persona.brand(n), "kind": "web"} for n in webs]}


def _window_action(action: str, address: str, workspace: str = "") -> str:
    import linux_desktop as ld
    import window_manager
    w = ld.window_exists(address)
    if not w:
        return "Jendela itu sudah tidak ada."
    label = window_manager._app_label(w)
    if action == "focus":
        ld.focus(address)
        return f"{label} di depan."
    if action == "close":
        ld.close(address)
        return f"{label} ditutup."
    if action == "move" and workspace:
        ld.move_to_workspace(address, workspace)
        return f"{label} dipindah ke workspace {workspace}."
    return "Aksi tidak dikenal."


# ───────────────────────────── screen & input (only with MOBILE_SCREEN on) ─────────────────────────────

_screen_notice: dict[str, float] = {}       # ip -> last time the PC showed "a phone is using the screen"
SCREEN_NOTICE_EVERY = 300


def _screen_in_use(ip: str, what: str):
    """Make it visible on the PC that a phone is watching / controlling it (once per 5 minutes per phone)."""
    now = time.time()
    with _lock:
        if now - _screen_notice.get(ip, 0) < SCREEN_NOTICE_EVERY:
            return
        _screen_notice[ip] = now
    text = f"HP {ip} sedang {what} PC ini lewat HUD Digidaw."
    events.emit("toast", text=text, level="warning")
    try:
        import linux_desktop as ld
        ld.notify("Digidaw: layar diakses dari HP", text, "normal")
    except Exception:
        pass
    print(f"[HUD HP] {text}")


_screen_cache = {"at": 0.0, "width": 0, "jpeg": b""}
_screen_lock = threading.Lock()


def _screen_jpeg(width: int) -> bytes:
    """The PC's screen, scaled down for the phone (cached 0.4 s: quick polls share one capture)."""
    import io
    import linux_desktop as ld
    width = max(320, min(width, 1600))
    with _screen_lock:
        if time.time() - _screen_cache["at"] < 0.4 and _screen_cache["width"] == width:
            return _screen_cache["jpeg"]
        try:
            img = ld.grab_screen()
        except Exception:
            return b""
        img.thumbnail((width, width * 2))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=62, optimize=True)
        _screen_cache.update(at=time.time(), width=width, jpeg=out.getvalue())
        return _screen_cache["jpeg"]


_mouse = {"dev": None}
_mouse_lock = threading.Lock()


def _virtual_mouse():
    """A virtual mouse (uinput): on Wayland no program may move the real pointer, but a new input device can."""
    with _mouse_lock:
        if _mouse["dev"] is None:
            from evdev import UInput, ecodes as e
            _mouse["dev"] = UInput({e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                                    e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]}, name="digidaw-remote-mouse")
            time.sleep(0.3)                     # let the compositor pick the new device up
        return _mouse["dev"]


def _pointer(data: dict) -> bool:
    from evdev import ecodes as e
    try:
        ui = _virtual_mouse()
    except Exception as err:
        print(f"[HUD HP] mouse virtual tidak tersedia: {err}")
        return False
    if "dx" in data or "dy" in data:
        ui.write(e.EV_REL, e.REL_X, int(max(-400, min(400, float(data.get("dx", 0))))))
        ui.write(e.EV_REL, e.REL_Y, int(max(-400, min(400, float(data.get("dy", 0))))))
    if "scroll" in data:
        ui.write(e.EV_REL, e.REL_WHEEL, int(max(-10, min(10, float(data["scroll"])))))
    click = str(data.get("click", ""))
    buttons = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
    if click in buttons or click == "double":
        button = buttons.get(click, e.BTN_LEFT)
        for _ in range(2 if click == "double" else 1):
            ui.write(e.EV_KEY, button, 1)
            ui.syn()
            ui.write(e.EV_KEY, button, 0)
            ui.syn()
            time.sleep(0.05)
    ui.syn()
    return True


KEYS = {"enter": "enter", "backspace": "backspace", "esc": "esc", "tab": "tab", "up": "up", "down": "down",
        "left": "left", "right": "right", "space": "space", "delete": "delete", "home": "home", "end": "end",
        "pageup": "pageup", "pagedown": "pagedown", "copy": "ctrl+c", "paste": "ctrl+v", "undo": "ctrl+z",
        "close_tab": "ctrl+w", "new_tab": "ctrl+t", "refresh": "f5", "fullscreen": "f11", "alt_tab": "alt+tab"}


def _key(data: dict) -> str:
    """Typed text (pasted into the focused window) or one named key / shortcut."""
    import system_control
    if data.get("text"):
        return system_control.type_text(str(data["text"])[:2000], bool(data.get("enter")))
    combo = KEYS.get(str(data.get("key", "")))
    return system_control.press_keys(combo) if combo else "tombol tidak dikenal"


def _voice_audio(text: str) -> tuple[bytes, str]:
    """Digidaw's own voice (Edge, or the local fallback) for the phone, so a reply sounds the same as on the PC."""
    import tts
    text = tts.clean_for_speech(text)
    if not text:
        return b"", ""
    path, cached = tts.tts_engine._synthesize(text)
    if not path:
        return b"", ""
    try:
        return path.read_bytes(), "audio/wav" if path.suffix == ".wav" else "audio/mpeg"
    finally:
        if not cached:
            path.unlink(missing_ok=True)


def _audio_data(audio: bytes):
    """Phone audio (webm/opus, mp4/aac, wav ...) -> speech_recognition.AudioData at 16 kHz mono (None if too short)."""
    import speech_recognition as sr
    try:
        raw = subprocess.run(["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000",
                              "pipe:1"], input=audio, capture_output=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(raw) < 16000:                        # under half a second
        return None
    return sr.AudioData(raw, 16000, 2)


def _recognize(data, prefer_wake: bool = False) -> str:
    """Google (Indonesian) first, Whisper on this PC as fallback. prefer_wake: among Google's alternatives take one
    that contains "Digidaw" (its top guess tends to "correct" the name to "digital")."""
    import speech_recognition as sr
    try:
        result = sr.Recognizer().recognize_google(data, language=config.STT_LANGUAGE, show_all=True)
        options = [a.get("transcript", "") for a in result.get("alternative", [])] if isinstance(result, dict) else []
        if options:
            name = config.WAKE_WORDS[0].lower()
            pick = next((o for o in options if name in o.lower()), options[0]) if prefer_wake else options[0]
            return pick.strip()
    except (sr.UnknownValueError, sr.RequestError):
        pass
    try:
        import local_stt
        return local_stt.transcribe(data, local_stt.OFFLINE_MODEL).strip()
    except Exception:
        return ""


def _transcribe(audio: bytes) -> str:
    data = _audio_data(audio)
    return _recognize(data) if data is not None else ""


MAX_STREAMS = 2                 # live screen streams at once (each costs a little CPU on the PC)
STREAM_MAX_SECONDS = 15 * 60    # a forgotten open tab doesn't stream forever
_streams = {"count": 0}
_monitor = {"width": 0}


def _grim_jpeg(width: int) -> bytes:
    """One frame of the screen as a small JPEG, mouse pointer included. grim scales and encodes it itself
    (~80 ms), several times faster than a full-size PNG resized in Python."""
    import shutil
    if not shutil.which("grim"):
        return _screen_jpeg(width)
    if not _monitor["width"]:
        try:
            import linux_desktop as ld
            _monitor["width"] = sum(m.get("width", 0) for m in (ld._hypr_json("monitors") or [])) or 1920
        except Exception:
            _monitor["width"] = 1920
    scale = max(0.2, min(1.0, width / _monitor["width"]))
    try:
        r = subprocess.run(["grim", "-c", "-t", "jpeg", "-q", "55", "-s", f"{scale:.3f}", "-"],
                           capture_output=True, timeout=5)
        return r.stdout if r.returncode == 0 else b""
    except (OSError, subprocess.TimeoutExpired):
        return b""


def start() -> bool:
    """Start the phone HUD when it is switched on and a PIN is set. Returns True when it runs."""
    global _server
    if not enabled():
        return False
    _load_sessions()
    try:
        cert, key = _certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        _server = _Server(("0.0.0.0", config.MOBILE_HUD_PORT), _Handler, context)
    except OSError as e:
        print(f"[HUD HP] port {config.MOBILE_HUD_PORT} tidak bisa dipakai: {e}")
        return False
    threading.Thread(target=_server.serve_forever, daemon=True, name="hud-hp").start()
    return True


def mobile_hud_info() -> str:
    """How to open Digidaw's HUD on a phone ('alamat HUD HP', 'buka Digidaw di HP', 'QR HUD HP'): opens a page
    with the address and a QR code to scan."""
    T = config.USER_TITLE
    if not config.MOBILE_HUD:
        return f"HUD untuk HP belum aktif, {T}. Nyalakan di Control Panel bagian HUD HP dan isi PIN-nya."
    if not str(config.MOBILE_PIN).strip():
        return f"HUD untuk HP butuh PIN dulu, {T}. Isi PIN di Control Panel bagian HUD HP."
    address = url()
    try:
        _open_qr_page(address)
        shown = " QR-nya saya tampilkan di layar."
    except Exception:
        shown = ""
    return f"Buka {address.removeprefix('http://').rstrip('/')} di browser HP yang tersambung ke Wi-Fi yang sama, {T}.{shown}"


def _open_qr_page(address: str):
    import segno
    import tempfile
    import webbrowser
    import report_theme as rt
    # dark on white: some phone cameras can't read an inverted (light-on-dark) code
    svg = segno.make(address, error="m").svg_inline(scale=8, dark="#030a12", light="#ffffff", border=3)
    body = (f'<section class="hero" style="grid-template-columns:auto 1fr"><div style="background:#ffffff;padding:6px;'
            f'border-radius:14px;line-height:0">{svg}</div><div><h2>Pindai dengan kamera HP</h2><p>Atau ketik alamat ini di browser HP '
            f'(harus di Wi-Fi yang sama):</p><p style="font:600 22px JetBrains Mono,monospace;color:#a6e9ff;margin-top:10px">'
            f'{rt.esc(address)}</p><p style="margin-top:12px">Pertama kali, browser memperingatkan sertifikatnya (wajar: '
            f'sertifikat buatan Digidaw sendiri untuk Wi-Fi rumah). Pilih <b>Lanjutan → Lanjutkan ke situs</b>. Lalu masuk '
            f'dengan PIN dari Control Panel, dan izinkan mikrofon saat tombol mic pertama kali ditekan. Pilih '
            f'"Tambahkan ke layar utama" agar Digidaw jadi seperti aplikasi.</p></div></section>')
    page = rt.page("HUD Digidaw di HP", "Digidaw · Remote", "Kendalikan musik, agenda, dan perintah dari kasur", body)
    path = Path(tempfile.gettempdir()) / "digidaw_hud_hp.html"
    path.write_text(page, encoding="utf-8")
    webbrowser.open(path.as_uri())


AI_TOOLS = [mobile_hud_info]
