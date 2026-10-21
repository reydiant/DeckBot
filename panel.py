"""Local control panel for a trading bot: a web dashboard plus a small JSON API.

The bot starts this as a background thread, so it lives and dies with the bot. It listens on
127.0.0.1 only, unless LAN access is switched on. Every API call needs the panel token
(Authorization: Bearer <token>), so a web page you visit or another device on the network can't
read or change anything without it. The HTML page itself contains no data.

Shared by DeckBot and PumpBot. Each bot supplies a snapshot() and an action() function.
"""
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("panel")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8765


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def panel_settings(path, default_port=DEFAULT_PORT):
    """Returns {token, port, lan}. Creates the token on first use."""
    s = _read(path, {})
    created = not s.get("token")
    if created:
        s["token"] = secrets.token_urlsafe(24)
    s.setdefault("port", default_port)
    s.setdefault("lan", False)
    if created:
        _write(path, s)
    return s


def set_panel_settings(path, **changes):
    s = panel_settings(path)
    s.update(changes)
    _write(path, s)
    return s


def run_cli(script, args, timeout=120):
    """Runs the bot's own command line, so the panel and the terminal share one code path."""
    try:
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "That took too long."
    out = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    return r.returncode == 0, (out[0] if out else "Done.")


def run_detached(script, args, label):
    """For commands that stop or restart the bot itself (go-live, sell-all). Run them in their own
    systemd unit so stopping the bot doesn't kill them halfway. Falls back to a plain child process."""
    cmd = [sys.executable, script, *args]
    if shutil.which("systemd-run"):
        unit = f"bot-panel-{label}-{int(time.time())}"
        r = subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}", *cmd],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            return True, f"Started. The bot will update in a few seconds."
        log.warning(f"systemd-run failed, running {label} directly: {r.stderr.strip()}")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True, "Started. The bot will update in a few seconds."


class _Handler(BaseHTTPRequestHandler):
    server_version = "BotPanel"

    def log_message(self, fmt, *args):  # keep the bot log clean
        pass

    def _cors(self):
        # Allows the Decky Quick Access panel (and other local tools) to call the API.
        # The token is still required, so this doesn't open anything to a random web page.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, default=str))

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        return bool(given) and hmac.compare_digest(given, self.server.token)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            # Any other address shows the dashboard, so a mistyped link still works.
            return self._send(200, self.server.html, "text/html; charset=utf-8")
        if path == "/api/state":
            if not self._authed():
                return self._json(401, {"error": "Wrong or missing token."})
            try:
                snap = self.server.snapshot()
                # other bots' summaries; skipped when a sibling asks, so two panels never loop
                if "siblings=0" not in parsed.query and self.server.others:
                    snap["others"] = self.server.others()
                return self._json(200, snap)
            except Exception as e:  # never let a bad file take the panel down
                log.exception("snapshot failed")
                return self._json(500, {"error": f"Could not read the bot's state yet: {e}"})
        return self._json(404, {"error": "Not found."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/action":
            return self._json(404, {"error": "Not found."})
        if not self._authed():
            return self._json(401, {"error": "Wrong or missing token."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self._json(413, {"error": "Request too large."})
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "Bad request."})
        try:
            ok, msg = self.server.action(str(req.get("action", "")), req.get("value"), req.get("confirm"))
        except Exception as e:
            log.exception("panel action failed")
            ok, msg = False, f"Something went wrong: {e}"
        return self._json(200 if ok else 400, {"ok": ok, "message": msg})


class Panel(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host, port, token, html, snapshot, action, others=None):
        self.token, self.html, self.snapshot, self.action, self.others = token, html, snapshot, action, others
        super().__init__((host, port), _Handler)


def _sibling_summary(path, own_name):
    """Reads another bot's panel from the same home folder and returns a short summary of it."""
    info = _read(path, None)
    if not info:
        return None
    port = int(info.get("port", DEFAULT_PORT))
    folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
    entry = {"bot": {"deckbot": "DeckBot", "pumpbot": "PumpBot"}.get(folder, folder), "port": port}
    if folder == own_name:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state?siblings=0",
                                     headers={"Authorization": f"Bearer {info.get('token', '')}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        for k in ("bot", "currency", "decimals", "mode", "paused", "halted", "service", "equity",
                  "pnl_day", "pnl_total", "open_count", "max_open", "risk_profile", "health"):
            entry[k] = data.get(k)
    except Exception:
        entry["error"] = "Not running right now."
    return entry


def sibling_summaries(settings_path):
    """Summaries of the other bots installed next to this one (for the 'Other bots' card)."""
    bot_root = os.path.dirname(os.path.dirname(os.path.abspath(settings_path)))
    own = os.path.basename(bot_root)
    home = os.path.dirname(bot_root)
    out = []
    for name in ("deckbot", "pumpbot"):
        if name == own:
            continue
        entry = _sibling_summary(os.path.join(home, name, "data", "panel.json"), own)
        if entry is None:
            entry = {"bot": name.capitalize(), "error": "Not set up on this Deck."}
        out.append(entry)
    return out


def start_panel(settings_path, snapshot_fn, action_fn, default_port=DEFAULT_PORT):
    """Starts the dashboard in a background thread. Returns the server, or None if it can't start."""
    try:
        s = panel_settings(settings_path, default_port)
        host = "0.0.0.0" if s["lan"] else "127.0.0.1"
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        srv = Panel(host, int(s["port"]), s["token"], html, snapshot_fn, action_fn,
                    others=lambda: sibling_summaries(settings_path))
    except OSError as e:
        log.warning(f"Dashboard not started: {e}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="panel").start()
    log.info(f"Dashboard running on port {s['port']} ({'LAN' if s['lan'] else 'this device only'})")
    return srv


def _ago(seconds):
    m = int(seconds // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    return f"{m // 60} h {m % 60} min ago"


def health(last_check_iso, interval_seconds, now=None):
    """Turns the bot's last-check time into {status, text, age}. status: running | stale | starting."""
    from datetime import datetime, timezone
    now = time.time() if now is None else now
    try:
        ts = datetime.fromisoformat(str(last_check_iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = max(0.0, now - ts.timestamp())
    except (TypeError, ValueError):
        return {"status": "starting", "text": "Starting up · no market check yet", "age": None}
    limit = max(3 * interval_seconds + 60, 120)
    if age <= limit:
        return {"status": "running", "text": f"Running · last check {_ago(age)}", "age": age}
    return {"status": "stale", "text": f"No market check for {_ago(age).replace(' ago', '')} · may be stuck, check the logs",
            "age": age}


def reset_token(settings_path):
    return set_panel_settings(settings_path, token=secrets.token_urlsafe(24))


def lan_address():
    """This device's address on the local network, for the link to open from a phone."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # UDP connect sends nothing; it just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def dashboard_links(settings_path, default_port=DEFAULT_PORT):
    """Returns (this-device link, LAN link or None), both with the token in the fragment."""
    s = panel_settings(settings_path, default_port)
    local = f"http://127.0.0.1:{s['port']}/#t={s['token']}"
    lan = f"http://{lan_address()}:{s['port']}/#t={s['token']}" if s["lan"] else None
    return local, lan
