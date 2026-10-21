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
# Actions that carry secrets: only accepted from this device (LAN traffic is plain HTTP).
LOCAL_ONLY_ACTIONS = {"keys", "remove_keys", "generate_wallet", "backup", "restore", "update"}


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

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

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
        if path == "/api/trades.csv":
            if not self._authed():
                return self._json(401, {"error": "Wrong or missing token."})
            if not self.server.export:
                return self._json(404, {"error": "Not available."})
            try:
                return self._send(200, self.server.export(), "text/csv; charset=utf-8")
            except Exception as e:
                log.exception("export failed")
                return self._json(500, {"error": f"Could not build the file: {e}"})
        return self._json(404, {"error": "Not found."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/action":
            return self._json(404, {"error": "Not found."})
        if not self._authed():
            return self._json(401, {"error": "Wrong or missing token."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16384:
            return self._json(413, {"error": "Request too large."})
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "Bad request."})
        name = str(req.get("action", ""))
        if name in LOCAL_ONLY_ACTIONS and not self._is_local():
            return self._json(403, {"ok": False, "message": "For safety, keys can only be entered on the Deck itself "
                                                             "(or in the Quick Access panel), not over Wi-Fi."})
        data = None
        try:
            res = self.server.action(name, req.get("value"), req.get("confirm"))
            ok, msg = res[0], res[1]
            data = res[2] if len(res) > 2 else None
        except Exception as e:
            log.exception("panel action failed")
            ok, msg = False, f"Something went wrong: {e}"
        out = {"ok": ok, "message": msg}
        if data is not None:
            out["data"] = data
        return self._json(200 if ok else 400, out)


class Panel(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host, port, token, html, snapshot, action, others=None, export=None):
        self.token, self.html, self.snapshot, self.action, self.others = token, html, snapshot, action, others
        self.export = export
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
        entry["token"] = info.get("token")  # for the "Open its dashboard" button (same owner, same Deck)
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


def start_panel(settings_path, snapshot_fn, action_fn, default_port=DEFAULT_PORT, export_fn=None):
    """Starts the dashboard in a background thread. Returns the server, or None if it can't start."""
    try:
        s = panel_settings(settings_path, default_port)
        host = "0.0.0.0" if s["lan"] else "127.0.0.1"
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        srv = Panel(host, int(s["port"]), s["token"], html, snapshot_fn, action_fn,
                    others=lambda: sibling_summaries(settings_path), export=export_fn)
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


_LOG_LINE = None
_REDACTIONS = None


def _redact(text):
    """Blanks anything that looks like a key, so the log is safe to show on the dashboard."""
    import re
    global _REDACTIONS
    if _REDACTIONS is None:
        _REDACTIONS = [
            (re.compile(r"-----BEGIN[^-]*-----.*?(-----END[^-]*-----|$)"), "***"),
            (re.compile(r"(api[-_]?key[=:]\s*)[^&\s\"',]+", re.I), r"\1***"),
            (re.compile(r"(secret|private[-_]?key|password)([\"']?\s*[=:]\s*[\"']?)[^\s\"',]+", re.I), r"\1\2***"),
            (re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,}\b"), "***"),  # Solana private keys are 87-88 characters
        ]
    for rx, sub in _REDACTIONS:
        text = rx.sub(sub, text)
    return text


def tail_log(path, n=100):
    """Last n log lines, newest last, as {time, level, text}. Reads only the end of the file."""
    import re
    global _LOG_LINE
    if _LOG_LINE is None:
        _LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d):\d\d (\w+) (.*)$")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > 64 * 1024:
        lines = lines[1:]  # first line is probably cut in half
    out = []
    for ln in lines:
        m = _LOG_LINE.match(ln)
        if m:
            out.append({"date": m.group(1), "time": m.group(2), "level": m.group(3), "text": _redact(m.group(4))})
        elif ln.strip() and out:
            out[-1]["text"] += " " + _redact(ln.strip())  # continuation (e.g. a multi-line message)
    return out[-n:]


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


# ---------------------------------------------------------------------------
# Trade journal: every trade, never truncated (the bots' own lists keep only the latest few hundred)
# ---------------------------------------------------------------------------

def journal_path(data_dir):
    return os.path.join(data_dir, "trades.jsonl")


def append_journal(data_dir, trade):
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(journal_path(data_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, separators=(",", ":")) + "\n")
    except OSError as e:
        log.warning(f"could not write trade journal: {e}")


def read_journal(data_dir, fallback=()):
    """All trades from the journal. Older trades from before the journal existed come from `fallback`."""
    out = []
    try:
        with open(journal_path(data_dir), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    if fallback:
        first = out[0]["time"] if out else None
        older = [t for t in fallback if first is None or t.get("time", "") < first]
        out = older + out
    return out


def trades_csv(rows, columns):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in columns])
    for r in rows:
        w.writerow([r.get(c[1], "") if not callable(c[1]) else c[1](r) for c in columns])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Go-live report card
# ---------------------------------------------------------------------------

def _days_between(a_iso, now):
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(str(a_iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (now - ts.timestamp()) / 86400)
    except (TypeError, ValueError):
        return 0.0


def build_report(*, mode, trades, started, now=None, currency, decimals, min_days, min_trades,
                 capital=None, equity=None, worst_drop_pct=None, bench=None):
    """trades: list of dicts with 'pnl' on closing trades. bench: {'name', 'return_pct'} or None."""
    now = time.time() if now is None else now
    closed = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p <= 0]
    total = sum(closed)
    run = peak = drop = 0.0
    for p in closed:
        run += p
        peak = max(peak, run)
        drop = max(drop, peak - run)
    days = _days_between(started, now) if started else 0.0
    ret = (equity / capital - 1) if capital and equity is not None else None
    r = {
        "mode": mode, "days": round(days, 1), "trades": len(closed), "wins": len(wins),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "pnl": round(total, decimals + 2), "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "return_pct": ret, "worst_drop": round(drop, decimals + 2),
        "worst_drop_pct": worst_drop_pct, "bench": bench, "currency": currency,
        "min_days": min_days, "min_trades": min_trades,
    }
    # verdict
    if mode == "live":
        level, text = "live", f"Live for {days:.0f} days."
    elif days < min_days or len(closed) < min_trades:
        level = "wait"
        text = (f"Keep paper trading: {days:.0f} of {min_days} days and {len(closed)} of {min_trades} "
                f"closed trades so far. Too early to judge.")
    elif total <= 0:
        level, text = "bad", "Paper trading has lost money so far. Don't go live with these settings."
    elif bench and ret is not None and bench.get("return_pct") is not None and ret < bench["return_pct"]:
        level, text = "mixed", (f"Made money, but less than just holding {bench['name']} over the same time. "
                                f"Going live wouldn't have beaten doing nothing.")
    else:
        level, text = "good", "Paper results are positive. If you go live, start with a small budget."
    r["verdict"] = {"level": level, "text": text}
    # human-readable lines (status command, weekly push)
    f = lambda x: f"{x:+.{decimals}f} {currency}"
    lines = [f"{'Live' if mode == 'live' else 'Paper'} results over {days:.0f} days: {len(closed)} closed trades"
             + (f", {len(wins)} winners ({r['win_rate']:.0%})" if closed else "")]
    if closed:
        lines.append(f"Profit/loss {f(total)}" + (f" ({ret:+.1%} of budget)" if ret is not None else "")
                     + f", worst drop {drop:.{decimals}f} {currency}"
                     + (f" ({worst_drop_pct:.1%})" if worst_drop_pct else ""))
    if bench and bench.get("return_pct") is not None:
        lines.append(f"Holding {bench['name']} instead: {bench['return_pct']:+.1%}"
                     + (f" vs the bot's {ret:+.1%}" if ret is not None else ""))
    lines.append(text)
    r["lines"] = lines
    return r


# ---------------------------------------------------------------------------
# Battery / power
# ---------------------------------------------------------------------------

def power_status(base="/sys/class/power_supply"):
    """{'on_battery': bool|None, 'percent': int|None}. None when the device has no battery info."""
    def rd(p):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            return None
    plugged, pct, status = False, None, None
    try:
        names = os.listdir(base)
    except OSError:
        return {"on_battery": None, "percent": None}
    for n in names:
        t = rd(os.path.join(base, n, "type"))
        if t in ("Mains", "USB", "USB_PD", "USB_C") and rd(os.path.join(base, n, "online")) == "1":
            plugged = True
        elif t == "Battery":
            c = rd(os.path.join(base, n, "capacity"))
            pct = int(c) if c and c.isdigit() else pct
            status = rd(os.path.join(base, n, "status")) or status
    if pct is None:
        return {"on_battery": None, "percent": None}
    on_battery = not plugged and status not in ("Charging", "Full")
    return {"on_battery": on_battery, "percent": pct}


# ---------------------------------------------------------------------------
# Keep the Deck awake while holding positions that need the bot to sell them
# ---------------------------------------------------------------------------

class SleepGuard:
    """Holds a systemd-inhibit sleep lock while active. Some sleep triggers may still ignore it.
    Never blocks the caller; after a refusal it waits 10 minutes before trying again."""
    RETRY_AFTER = 600

    def __init__(self, who, clock=time.time):
        self.who, self.clock = who, clock
        self.proc, self.failed, self.failed_at, self.started_at = None, False, 0.0, 0.0

    def _check(self):
        if self.proc is not None and self.proc.poll() is not None:  # the lock process ended by itself
            quick = self.clock() - self.started_at < 5
            self.proc = None
            if quick:  # exited straight away = the system refused the lock
                if not self.failed:
                    log.warning("Couldn't keep the Deck awake (sleep lock refused); will retry in 10 minutes")
                self.failed, self.failed_at = True, self.clock()

    @property
    def active(self):
        self._check()
        return self.proc is not None and self.clock() - self.started_at >= 1  # confirmed after it stays up 1s

    def hold(self, on, why=""):
        self._check()
        if on and self.proc is None:
            if self.failed and self.clock() - self.failed_at < self.RETRY_AFTER:
                return
            if not shutil.which("systemd-inhibit"):
                self.failed, self.failed_at = True, self.clock()
                return
            try:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", f"--who={self.who}",
                     f"--why={why or 'Holding open positions'}", "--mode=block", "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.started_at = self.clock()
            except OSError:
                self.proc, self.failed, self.failed_at = None, True, self.clock()
        elif on and self.proc is not None and self.failed and self.clock() - self.started_at >= 5:
            self.failed = False
            log.info(f"Keeping the Deck awake: {why}")
        elif not on and self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None
            self.failed = False
            log.info("Sleep allowed again (no open positions)")

    def status(self, n_open):
        if self.active:
            return {"active": True, "text": f"Keeping the Deck awake while holding {n_open} position{'s' if n_open != 1 else ''}"}
        if n_open and self.failed:
            return {"active": False, "text": "Couldn't keep the Deck awake - keep it plugged in and don't sleep it"}
        if n_open and self.proc is not None:
            return {"active": False, "text": "Asking the Deck to stay awake..."}
        return {"active": False, "text": "Sleep allowed (nothing held)" if not n_open else "Sleep allowed"}


# ---------------------------------------------------------------------------
# Watchdog: run every ~10 minutes by a systemd timer, separate from the bot itself
# ---------------------------------------------------------------------------

WEEK = 7 * 86400


def watchdog_tick(data_dir, label, snap, notify, enabled, battery_sensitive=False, now=None, power=None):
    """Sends phone alerts for: bot stalled/recovered, weekly summary, on battery while holding.
    Returns the list of (title, message) it sent. Remembers what it already said in watchdog.json."""
    now = time.time() if now is None else now
    path = os.path.join(data_dir, "watchdog.json")
    st = _read(path, {})
    sent = []

    def send(title, msg):
        notify(title, msg)
        sent.append((title, msg))

    if not enabled:  # stopped on purpose with the stop command - stay quiet
        st["stalled"] = False
        _write(path, st)
        return sent

    h = (snap or {}).get("health") or {}
    stalled = h.get("status") == "stale" or snap is None
    if stalled and not st.get("stalled"):
        send("stopped checking", f"{label} hasn't checked the market recently ({h.get('text', 'no status')}). "
                                 f"Open Konsole and run ./{label.lower()} logs, or restart it with ./{label.lower()} restart.")
        st["stalled"] = True
    elif not stalled and st.get("stalled"):
        send("back to normal", f"{label} is checking the market again. {h.get('text', '')}")
        st["stalled"] = False

    # weekly summary
    if snap and snap.get("report"):
        last = st.get("last_summary")
        if last is None:
            st["last_summary"] = now  # first run: start counting the week from now
        elif now - last >= WEEK:
            rep = snap["report"]
            extra = f" Bot is {'paused' if snap.get('paused') else 'active'}, risk {snap.get('risk_profile', '?')}."
            send("weekly summary", "\n".join(rep.get("lines", [])) + extra)
            st["last_summary"] = now

    # on battery while holding positions that only the bot can sell
    if battery_sensitive and snap:
        pw = power if power is not None else power_status()
        holding = snap.get("open_count", 0) > 0
        if pw.get("on_battery") and holding:
            if not st.get("battery_warned"):
                send("on battery", f"The Deck is on battery ({pw.get('percent')}%) while {label} holds "
                                   f"{snap['open_count']} position(s). It can't sell if the Deck sleeps or runs flat - plug it in.")
                st["battery_warned"] = True
            if (pw.get("percent") or 100) <= 20 and not st.get("battery_low_warned"):
                send("battery low", f"Battery at {pw.get('percent')}% and {label} still holds positions. "
                                    f"Plug in now, or sell everything from the dashboard.")
                st["battery_low_warned"] = True
        else:
            st["battery_warned"] = False
            st["battery_low_warned"] = False
    _write(path, st)
    return sent


def watchdog_units(name, python, script, workdir):
    """systemd unit files for the watchdog timer."""
    svc = f"""[Unit]
Description={name} watchdog (phone alerts)

[Service]
Type=oneshot
ExecStart={python} {script} watchdog
WorkingDirectory={workdir}
"""
    timer = f"""[Unit]
Description=Run the {name} watchdog every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return svc, timer


def ntfy(topic, title, message):
    """Sends a phone notification through ntfy.sh. Returns True when it went out."""
    topic = (topic or "").strip()
    if not topic:
        return False
    try:
        import urllib.request
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode(), method="POST",
                                     headers={"Title": title})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log.warning(f"Notification failed: {e}")
        return False


def valid_topic(topic):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic or ""))



# ---------------------------------------------------------------------------
# Small helpers: local time, key masking, Solana key check, equity history
# ---------------------------------------------------------------------------

def local_time(iso, fmt="%m-%d %H:%M"):
    """A UTC timestamp shown in the Deck's own time zone."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return str(iso or "")


def mask(secret, keep=4):
    s = str(secret or "")
    return ("…" + s[-keep:]) if len(s) > keep else ("set" if s else "")


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def valid_solana_address(addr):
    try:
        return 32 <= len(addr) <= 44 and len(b58decode(addr)) == 32
    except ValueError:
        return False


def solana_key_matches(private_key, wallet):
    """A Solana private key (base58, 64 bytes) ends with its own public key, so we can check that the
    key belongs to the wallet without any crypto library. Also accepts the [1,2,...] JSON-array form."""
    try:
        pk = private_key.strip()
        raw = bytes(json.loads(pk)) if pk.startswith("[") else b58decode(pk)
    except (ValueError, TypeError):
        return False
    return len(raw) == 64 and b58encode(raw[32:]) == wallet


def add_history_point(hist, ts, value, bench=None, every=3600, cap=2000):
    """Appends (ts, value[, bench]) at most once per `every` seconds; keeps the newest `cap` points."""
    if value is None:
        return hist
    if hist and ts - hist[-1][0] < every:
        hist[-1] = [hist[-1][0], value, bench]   # keep the latest value in the current slot
    else:
        hist.append([ts, value, bench])
    return hist[-cap:]


def merge_histories(histories, every=3600):
    """Sums several accounts' equity histories into one, carrying each account's last value forward."""
    buckets = sorted({int(p[0] // every) for h in histories for p in h})
    last = [None] * len(histories)
    idx = [0] * len(histories)
    out = []
    for b in buckets:
        bench = None
        for k, h in enumerate(histories):
            while idx[k] < len(h) and int(h[idx[k]][0] // every) <= b:
                last[k] = h[idx[k]]
                idx[k] += 1
            if last[k] is not None and len(last[k]) > 2 and last[k][2] is not None:
                bench = last[k][2] if bench is None else bench
        vals = [p[1] for p in last if p is not None]
        if vals:
            out.append([b * every, sum(vals), bench])
    return out


# ---------------------------------------------------------------------------
# Encrypted backups (standard library only: scrypt key derivation, HMAC-SHA256 counter-mode stream
# cipher, encrypt-then-MAC). A wrong password or a damaged/tampered file is rejected, never half-read.
# ---------------------------------------------------------------------------

BACKUP_MAGIC = b"BOTBACKUP1\n"
BACKUP_EXT = ".botbak"


def _keys_from_password(password, salt):
    import hashlib
    k = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=64)
    return k[:32], k[32:]


def _keystream_xor(key, nonce, data):
    import hashlib
    out = bytearray(len(data))
    block = 0
    for i in range(0, len(data), 32):
        ks = hmac.new(key, nonce + block.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[i:i + 32]
        out[i:i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, ks))
        block += 1
    return bytes(out)


def encrypt_blob(plain, password):
    import hashlib
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    ek, mk = _keys_from_password(password, salt)
    body = BACKUP_MAGIC + salt + nonce + _keystream_xor(ek, nonce, plain)
    return body + hmac.new(mk, body, hashlib.sha256).digest()


def decrypt_blob(blob, password):
    """Returns the plaintext, or raises ValueError('wrong password or damaged file')."""
    import hashlib
    n = len(BACKUP_MAGIC)
    if len(blob) < n + 16 + 16 + 32 or blob[:n] != BACKUP_MAGIC:
        raise ValueError("That isn't a bot backup file.")
    salt, nonce = blob[n:n + 16], blob[n + 16:n + 32]
    body, tag = blob[:-32], blob[-32:]
    ek, mk = _keys_from_password(password, salt)
    if not hmac.compare_digest(hmac.new(mk, body, hashlib.sha256).digest(), tag):
        raise ValueError("Wrong password, or the backup file is damaged.")
    return _keystream_xor(ek, nonce, body[n + 32:])


def backup_places(home=None):
    """Where backups can go: SD cards / USB sticks first, then ~/Documents/bot-backups."""
    home = home or os.path.expanduser("~")
    user = os.path.basename(home.rstrip("/"))
    places = []
    for base in (f"/run/media/{user}", "/run/media"):
        try:
            for n in sorted(os.listdir(base)):
                p = os.path.join(base, n)
                if os.path.isdir(p) and os.access(p, os.W_OK) and p != f"/run/media/{user}":
                    places.append({"path": os.path.join(p, "bot-backups"), "label": f"💾 {n} (SD card / USB)"})
        except OSError:
            continue
    places.append({"path": os.path.join(home, "Documents", "bot-backups"), "label": "🏠 Documents/bot-backups on this Deck"})
    seen, out = set(), []
    for pl in places:
        if pl["path"] not in seen:
            seen.add(pl["path"]); out.append(pl)
    return out


def find_backups(bot, home=None, limit=10):
    found = []
    for pl in backup_places(home):
        try:
            for n in os.listdir(pl["path"]):
                if n.startswith(bot + "-backup-") and n.endswith(BACKUP_EXT):
                    p = os.path.join(pl["path"], n)
                    found.append({"path": p, "label": f"{n[len(bot) + 8:-len(BACKUP_EXT)]} · {pl['label'].split(' (')[0]}",
                                  "mtime": os.path.getmtime(p), "size": os.path.getsize(p)})
        except OSError:
            continue
    found.sort(key=lambda b: -b["mtime"])
    return found[:limit]


def make_backup(data_dir, bot, dest_dir, password, now=None):
    """Zips data_dir (minus logs and temp files), encrypts it, writes <bot>-backup-<date>.botbak. Returns path."""
    import io
    import zipfile
    from datetime import datetime
    if len(password or "") < 8:
        raise ValueError("Use a password of at least 8 characters.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(data_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.endswith((".log", ".tmp")) or ".log." in f or f.startswith(".restore"):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, data_dir))
    blob = encrypt_blob(buf.getvalue(), password)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(dest_dir, f"{bot}-backup-{stamp}{BACKUP_EXT}")
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return path


def open_backup(path, password, must_have="config.json"):
    """Decrypts and checks a backup. Returns a ZipFile. Raises ValueError with a plain reason."""
    import io
    import zipfile
    try:
        with open(path, "rb") as f:
            blob = f.read(200 * 1024 * 1024)
    except OSError:
        raise ValueError("Couldn't open that backup file.")
    plain = decrypt_blob(blob, password)
    try:
        z = zipfile.ZipFile(io.BytesIO(plain))
    except zipfile.BadZipFile:
        raise ValueError("The backup file is damaged.")
    names = z.namelist()
    for n in names:
        if n.startswith("/") or ".." in n.split("/") or "\\" in n:
            raise ValueError("The backup contains unsafe file names - not restored.")
    if must_have not in names:
        raise ValueError("That backup doesn't look like it's from this bot.")
    return z


def restore_backup(data_dir, zf, now=None):
    """Moves the current data folder aside (never deletes it), then unpacks the backup. Returns the aside path."""
    from datetime import datetime
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    aside = f"{data_dir.rstrip('/')}.before-restore-{stamp}"
    n = 2
    while os.path.exists(aside):  # never collide with an earlier restore's saved copy
        aside = f"{data_dir.rstrip('/')}.before-restore-{stamp}-{n}"
        n += 1
    if os.path.exists(data_dir):
        os.replace(data_dir, aside)
    os.makedirs(data_dir, exist_ok=True)
    for n in zf.namelist():
        if n.endswith("/"):
            continue
        target = os.path.join(data_dir, n)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(zf.read(n))
    return aside if os.path.exists(aside) else None


# ---------------------------------------------------------------------------
# One-tap updates from a zip in Downloads
# ---------------------------------------------------------------------------

def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def inspect_update(zip_path, bot, allowed):
    """Checks a downloaded bot zip. Returns {'version', 'files'} or raises ValueError with the reason."""
    import re
    import zipfile
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile):
        raise ValueError("Not a readable zip file.")
    files = {}
    total = 0
    for info in z.infolist():
        n = info.filename
        if info.is_dir():
            continue
        parts = n.split("/")
        if n.startswith("/") or ".." in parts or "\\" in n or len(parts) != 2 or parts[0] != bot:
            raise ValueError(f"Unexpected file in the zip ({n}) - not installed.")
        if parts[1] not in allowed:
            raise ValueError(f"Unexpected file in the zip ({parts[1]}) - not installed.")
        total += info.file_size
        if total > 5 * 1024 * 1024:
            raise ValueError("The zip is far too big to be a bot update.")
        files[parts[1]] = n
    main = f"{bot}.py"
    if main not in files or "panel.py" not in files:
        raise ValueError("The zip is missing the bot's main files.")
    m = re.search(r'^VERSION = "([0-9.]+)"', z.read(files[main]).decode("utf-8", "replace"), re.M)
    if not m:
        raise ValueError("That zip is an older version without a version number.")
    return {"version": m.group(1), "files": files}


def find_update(bot, current_version, allowed, home=None):
    """The newest valid zip for this bot in Downloads that's newer than what's installed, or None."""
    home = home or os.path.expanduser("~")
    folder = os.path.join(home, "Downloads")
    best = None
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for n in names:
        if not (n.lower().startswith(bot) and n.lower().endswith(".zip")):
            continue
        p = os.path.join(folder, n)
        try:
            info = inspect_update(p, bot, allowed)
        except ValueError:
            continue
        if _version_tuple(info["version"]) > _version_tuple(current_version):
            if best is None or _version_tuple(info["version"]) > _version_tuple(best["version"]):
                best = {"path": p, "name": n, "version": info["version"], "mtime": os.path.getmtime(p)}
    return best


def install_update(zip_path, bot, here, allowed):
    """Copies the verified files over the installed bot (never touches data/). The replaced files are kept
    in .previous/ so an update can be undone. Returns the new version."""
    import shutil as _sh
    import zipfile
    info = inspect_update(zip_path, bot, allowed)
    prev = os.path.join(here, ".previous")
    os.makedirs(prev, exist_ok=True)
    z = zipfile.ZipFile(zip_path)
    for name, member in info["files"].items():
        target = os.path.join(here, name)
        if os.path.exists(target):
            _sh.copy2(target, os.path.join(prev, name))
        tmp = target + ".new"
        with open(tmp, "wb") as f:
            f.write(z.read(member))
        mode = 0o755 if name in (bot, f"{bot}.py", "install.sh") else 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    return info["version"]


# ---------------------------------------------------------------------------
# Backup / restore / update: shared button handling and terminal commands
# ---------------------------------------------------------------------------

def maintenance_snapshot(bot, version, allowed):
    return {"version": version, "update": find_update(bot, version, allowed),
            "backup_places": backup_places(), "backups": find_backups(bot)}


def maintenance_action(name, value, confirm, *, bot, data_dir, script, version, allowed):
    """Handles backup, restore, check_update and update. Returns (ok, message) or None if not ours."""
    v = value if isinstance(value, dict) else {}
    if name == "backup":
        dest = str(v.get("dest") or "")
        if dest not in [p["path"] for p in backup_places()]:
            return False, "Pick where to save the backup."
        try:
            path = make_backup(data_dir, bot, dest, str(v.get("password") or ""))
        except ValueError as e:
            return False, str(e)
        except OSError as e:
            return False, f"Couldn't write the backup there: {e.strerror or e}"
        log.info(f"backup saved to {path}")
        return True, f"💾 Backup saved: {os.path.basename(path)}. Keep the password safe - it can't be recovered."
    if name == "restore":
        if confirm != "RESTORE":
            return False, "Type RESTORE to confirm."
        path = str(v.get("path") or "")
        if path not in [b["path"] for b in find_backups(bot, limit=1000)]:
            return False, "Pick one of the backups listed."
        password = str(v.get("password") or "")
        try:
            open_backup(path, password)            # check the password now, so mistakes show right away
        except ValueError as e:
            return False, str(e)
        req = os.path.join(data_dir, ".restore-request")
        _write(req, {"path": path, "password": password})
        return run_detached(script, ["restore-pending"], "restore")[0], \
            "♻️ Restoring... the bot restarts in a few seconds. Your current data is kept aside, not deleted."
    if name == "check_update":
        up = find_update(bot, version, allowed)
        if not up:
            return True, f"✅ You're up to date (version {version}). No newer {bot} zip in Downloads."
        return True, f"⬆️ Version {up['version']} is ready to install ({up['name']} in Downloads)."
    if name == "update":
        if confirm != "UPDATE":
            return False, "Confirm the update first."
        up = find_update(bot, version, allowed)
        if not up:
            return False, f"No newer {bot} zip found in Downloads."
        run_detached(script, ["apply-update", up["path"]], "update")
        return True, f"⬆️ Installing version {up['version']}... the bot restarts in a few seconds."
    return None


def cli_restore_pending(data_dir, stop_bot, start_bot):
    """Run in its own process: stop the bot, restore, start it again."""
    req_path = os.path.join(data_dir, ".restore-request")
    req = _read(req_path, None)
    try:
        os.remove(req_path)
    except OSError:
        pass
    if not req:
        print("No restore was requested.")
        return 1
    try:
        zf = open_backup(req["path"], req["password"])
    except ValueError as e:
        print(f"Restore cancelled: {e}")
        return 1
    stop_bot()
    aside = restore_backup(data_dir, zf)
    start_bot()
    print(f"Restored from {os.path.basename(req['path'])}." + (f" Previous data kept in {aside}." if aside else ""))
    return 0


def cli_restore(bot, data_dir, stop_bot, start_bot, running):
    """Interactive restore from the terminal (also used by install.sh on a fresh install)."""
    import getpass
    backups = find_backups(bot, limit=20)
    if not backups:
        print("No backups found on SD cards, USB sticks or in ~/Documents/bot-backups.")
        return 1
    print("Backups found:")
    for i, b in enumerate(backups, 1):
        print(f"  {i}. {b['label']}  ({b['path']})")
    pick = input(f"Which one? [1-{len(backups)}, Enter = 1]: ").strip() or "1"
    if not pick.isdigit() or not 1 <= int(pick) <= len(backups):
        print("Cancelled.")
        return 1
    b = backups[int(pick) - 1]
    for _ in range(3):
        try:
            zf = open_backup(b["path"], getpass.getpass("Backup password (hidden): "))
            break
        except ValueError as e:
            print(f"  {e}")
    else:
        return 1
    was_running = running()
    if was_running:
        stop_bot()
    aside = restore_backup(data_dir, zf)
    print("Restored." + (f" Your previous data is kept in {aside}." if aside else ""))
    if was_running:
        start_bot()
        print("The bot is running again.")
    return 0


def cli_backup(bot, data_dir):
    import getpass
    places = backup_places()
    for i, p in enumerate(places, 1):
        print(f"  {i}. {p['label']}")
    pick = input(f"Save the backup where? [1-{len(places)}, Enter = 1]: ").strip() or "1"
    if not pick.isdigit() or not 1 <= int(pick) <= len(places):
        print("Cancelled.")
        return 1
    pw = getpass.getpass("Choose a backup password (8+ characters, hidden): ")
    if pw != getpass.getpass("Type it again: "):
        print("The passwords didn't match.")
        return 1
    try:
        path = make_backup(data_dir, bot, places[int(pick) - 1]["path"], pw)
    except (ValueError, OSError) as e:
        print(f"Backup failed: {e}")
        return 1
    print(f"Backup saved: {path}\nKeep the password safe - without it the backup can't be opened.")
    return 0
