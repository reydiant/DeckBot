#!/usr/bin/env python3
"""
DeckBot - a hands-off trend-following crypto bot for Kraken and Coinbase.

Strategy (spot, long-only, 4-hour candles):
  Enter  when the trend is up (EMA21 > EMA55, price above a rising EMA100)
         AND price closes at a new 20-candle high (breakout confirmation).
  Size   so that hitting the stop loses `risk_per_trade` of the bot's equity,
         capped at `max_position_pct` of equity per coin.
  Exit   on a close below EMA55, or when price hits the stop:
         initial stop = entry - 2.5 ATR, then trails at highest close - 3 ATR.
  AI     a self-learning model scores every breakout; it trains daily on months of
         history for ~20 coins and only vetoes signals once a walk-forward test
         proves it helps (see "Self-learning AI" below).
  Guards daily loss limit (no new buys for the rest of the UTC day),
         drawdown kill-switch (pauses new buys until you resume),
         and exchange-side stop orders so positions stay protected while the
         Deck is asleep.

The bot only ever trades the budget you give it and only sells coins it
bought itself; anything else in your account is left alone.

Commands:  setup | run | status | learn | go-live | paper | pause | resume | sell-all
           start | stop | restart | logs
"""

import argparse
import getpass
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from panel import dashboard_links, health, reset_token, run_cli, run_detached, set_panel_settings, start_panel

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DECKBOT_DATA", os.path.join(HERE, "data"))
CONFIG_PATH = os.path.join(DATA, "config.json")
KEYS_PATH = os.path.join(DATA, "keys.json")
PANEL_PATH = os.path.join(DATA, "panel.json")
SCRIPT = os.path.abspath(__file__)
LOG_PATH = os.path.join(DATA, "deckbot.log")
SERVICE_NAME = "deckbot.service"

RISK_PRESETS = {
    "conservative": {"risk_per_trade": 0.01, "daily_loss_limit": 0.03, "max_drawdown": 0.15},
    "moderate": {"risk_per_trade": 0.02, "daily_loss_limit": 0.05, "max_drawdown": 0.20},
    "aggressive": {"risk_per_trade": 0.04, "daily_loss_limit": 0.10, "max_drawdown": 0.30},
}

DEFAULT_CONFIG = {
    "mode": "paper",                 # "paper" or "live"
    "risk_profile": "aggressive",
    "accounts": [],                  # [{"exchange": "kraken", "budget": 500, "quote": "USD"}]
    "coins": ["BTC", "ETH", "SOL"],
    "max_positions": 3,
    "max_position_pct": 0.40,        # max share of bot equity in one coin
    "timeframe": "4h",
    "ema_fast": 21,
    "ema_slow": 55,
    "ema_trend": 100,
    "breakout_lookback": 20,
    "atr_period": 14,
    "stop_atr": 2.5,
    "trail_atr": 3.0,
    "cooldown_candles": 6,           # wait after a stop-out before re-buying that coin
    "exchange_stops": True,          # place protective stop orders on the exchange
    "check_every_seconds": 300,
    "ntfy_topic": "",                # optional phone alerts via https://ntfy.sh
    # self-learning AI
    "ai": True,
    "ai_coins": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "BCH", "ATOM",
                 "UNI", "NEAR", "AAVE", "XLM", "ETC", "FIL", "ALGO", "ARB"],  # history it learns from
    "ai_history_candles": 720,       # ~120 days of 4h candles per coin
    "ai_retrain_hours": 24,
    "ai_min_samples": 80,            # walk-forward-tested signals needed before it may act
    "ai_min_auc": 0.57,
}

# Taker fee estimates used for paper trading (entry-tier rates).
PAPER_FEES = {"kraken": 0.004, "coinbase": 0.006}
PAPER_SLIPPAGE = 0.001
TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}

log = logging.getLogger("deckbot")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    stored = read_json(CONFIG_PATH, {})
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(stored)
    cfg.update(RISK_PRESETS.get(cfg["risk_profile"], RISK_PRESETS["aggressive"]))
    # explicit overrides in config.json win over the preset
    for k in ("risk_per_trade", "daily_loss_limit", "max_drawdown"):
        if k in stored:
            cfg[k] = stored[k]
    return cfg


def save_config(cfg):
    stored = {k: v for k, v in cfg.items() if k not in ("risk_per_trade", "daily_loss_limit", "max_drawdown")
              or k in read_json(CONFIG_PATH, {})}
    write_json(CONFIG_PATH, stored)


def state_path(exchange_id):
    return os.path.join(DATA, f"state-{exchange_id}.json")


def setup_logging(to_console=True):
    os.makedirs(DATA, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        log.addHandler(ch)


def notify(cfg, title, message):
    topic = (cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=message.encode(), method="POST",
            headers={"Title": f"DeckBot: {title}"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log.warning(f"Notification failed: {e}")


# ---------------------------------------------------------------------------
# Indicators (pure Python)
# ---------------------------------------------------------------------------

def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(highs, lows, closes, period):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    prev = sum(trs[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(closes)):
        prev = (prev * (period - 1) + trs[i]) / period  # Wilder smoothing
        out[i] = prev
    return out


def analyze(candles, cfg):
    """candles: closed [ts, o, h, l, c, v] oldest->newest. Returns signal dict for the last candle."""
    need = max(cfg["ema_trend"], cfg["ema_slow"]) + 10
    if len(candles) < need:
        return None
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    f = ema(closes, cfg["ema_fast"])
    s = ema(closes, cfg["ema_slow"])
    t = ema(closes, cfg["ema_trend"])
    a = atr(highs, lows, closes, cfg["atr_period"])
    i = len(candles) - 1
    lb = cfg["breakout_lookback"]
    prior_high = max(highs[i - lb:i])
    trend_up = f[i] > s[i] and closes[i] > t[i] and t[i] > t[i - 6]
    return {
        "ts": candles[i][0],
        "close": closes[i],
        "atr": a[i],
        "ema_slow": s[i],
        "entry": trend_up and closes[i] > prior_high,
        "exit": closes[i] < s[i],
    }


# ---------------------------------------------------------------------------
# Self-learning AI
#
# Buy signals are rare (a few a month), far too few to learn from our own trades.
# So the bot replays its exact strategy over months of 4h candles for ~20 liquid
# coins, labels every historical breakout by whether the bot's own exit rules would
# have made money, and fits a model that scores new breakouts.
#
# It's validated walk-forward (trained only on the past, scored on later periods),
# runs in shadow mode until that test shows it beats taking every signal, and
# retrains every day on fresh data.
# ---------------------------------------------------------------------------

AI_FEATURES = ["trend_gap", "above_trend", "trend_slope", "volatility", "breakout", "volume_ratio",
               "rsi", "momentum", "range_pos", "room_to_high", "btc_trend", "btc_momentum"]


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


class Series:
    """Indicator arrays for one coin's candles, computed once."""

    def __init__(self, candles, cfg):
        self.c = candles
        self.ts = [x[0] for x in candles]
        self.o = [x[1] for x in candles]
        self.h = [x[2] for x in candles]
        self.l = [x[3] for x in candles]
        self.cl = [x[4] for x in candles]
        self.v = [x[5] or 0 for x in candles]
        self.f = ema(self.cl, cfg["ema_fast"])
        self.s = ema(self.cl, cfg["ema_slow"])
        self.t = ema(self.cl, cfg["ema_trend"])
        self.a = atr(self.h, self.l, self.cl, cfg["atr_period"])
        self.r = rsi(self.cl)
        self.idx = {t: i for i, t in enumerate(self.ts)}
        self.lb = cfg["breakout_lookback"]
        self.warm = max(cfg["ema_trend"], cfg["ema_slow"], 60) + 10

    def is_signal(self, i):
        if i < self.warm or not self.a[i]:
            return False
        trend_up = self.f[i] > self.s[i] and self.cl[i] > self.t[i] and self.t[i] > self.t[i - 6]
        return trend_up and self.cl[i] > max(self.h[i - self.lb:i])

    def features(self, i, btc=None):
        a, c = self.a[i], self.cl[i]
        vol_avg = sum(self.v[i - 20:i]) / 20
        rng = self.h[i] - self.l[i]
        bi = btc.idx.get(self.ts[i]) if btc else None
        if bi is not None and bi >= btc.warm:
            btc_trend = (btc.cl[bi] / btc.t[bi] - 1) * 100
            btc_mom = (btc.cl[bi] / btc.cl[bi - 30] - 1) * 100
        else:
            btc_trend = (c / self.t[i] - 1) * 100
            btc_mom = (c / self.cl[i - 30] - 1) * 100
        return {
            "trend_gap": (self.f[i] - self.s[i]) / a,
            "above_trend": (c - self.t[i]) / a,
            "trend_slope": (self.t[i] / self.t[i - 6] - 1) * 100,
            "volatility": a / c * 100,
            "breakout": (c - max(self.h[i - self.lb:i])) / a,
            "volume_ratio": math.log((self.v[i] + 1e-9) / (vol_avg + 1e-9)) if vol_avg > 0 else 0.0,
            "rsi": (self.r[i] or 50) / 100,
            "momentum": (c / self.cl[i - 30] - 1) * 100,
            "range_pos": (c - self.l[i]) / rng if rng > 0 else 0.5,
            "room_to_high": (max(self.h[i - 60:i]) - c) / a,
            "btc_trend": btc_trend,
            "btc_momentum": btc_mom,
        }

    def outcome(self, i, cfg, fee):
        """Replay the bot's exits from a signal at candle i. Returns (R multiple, exit index) or None."""
        entry = self.cl[i]
        stop_dist = cfg["stop_atr"] * self.a[i]
        stop, highest = entry - stop_dist, entry
        for j in range(i + 1, len(self.cl)):
            if self.l[j] <= stop:
                exit_p = min(self.o[j], stop)
            elif self.cl[j] < self.s[j]:
                exit_p = self.cl[j]
            else:
                highest = max(highest, self.cl[j])
                stop = max(stop, highest - cfg["trail_atr"] * self.a[j])
                continue
            ret = exit_p / entry - 1 - 2 * fee
            return ret / (stop_dist / entry), j
        return None  # trade still open at the end of the data


def build_dataset(series_by_coin, btc, cfg, fee):
    samples = []
    for coin, s in series_by_coin.items():
        i = s.warm
        while i < len(s.cl):
            if s.is_signal(i):
                out = s.outcome(i, cfg, fee)
                if out is None:
                    break
                r, j = out
                samples.append({"ts": s.ts[i], "exit_ts": s.ts[j], "coin": coin,
                                "x": [s.features(i, btc)[k] for k in AI_FEATURES], "r": r, "y": 1 if r > 0 else 0})
                i = j + 1  # like the bot: one position per coin at a time
            else:
                i += 1
    samples.sort(key=lambda d: d["ts"])
    return samples


def _fit(xs, ys, l2=0.05, iters=400, lr=0.2):
    n, d = len(xs), len(xs[0])
    mean = [sum(x[k] for x in xs) / n for k in range(d)]
    std = [math.sqrt(sum((x[k] - mean[k]) ** 2 for x in xs) / n) or 1.0 for k in range(d)]
    z = [[max(-5, min(5, (x[k] - mean[k]) / std[k])) for k in range(d)] for x in xs]
    w, b = [0.0] * d, math.log((sum(ys) + 1) / (n - sum(ys) + 1))
    for _ in range(iters):
        gw, gb = [0.0] * d, 0.0
        for zi, yi in zip(z, ys):
            p = 1 / (1 + math.exp(-max(-30, min(30, b + sum(wk * v for wk, v in zip(w, zi))))))
            e = p - yi
            gb += e
            for k in range(d):
                gw[k] += e * zi[k]
        b -= lr * gb / n
        w = [wk - lr * (g / n + l2 * wk) for wk, g in zip(w, gw)]
    return {"w": w, "b": b, "mean": mean, "std": std}


def _predict(m, x):
    z = [max(-5, min(5, (v - mu) / sd)) for v, mu, sd in zip(x, m["mean"], m["std"])]
    s = m["b"] + sum(w * v for w, v in zip(m["w"], z))
    return 1 / (1 + math.exp(-max(-30, min(30, s))))


def _auc(pairs):
    pos = sorted(p for p, y in pairs if y)
    neg = sorted(p for p, y in pairs if not y)
    if len(pos) < 5 or len(neg) < 5:
        return None
    wins = 0.0
    for p in pos:  # simple O(n*m) is fine at this size
        for q in neg:
            wins += 1.0 if p > q else 0.5 if p == q else 0.0
    return wins / (len(pos) * len(neg))


class Brain:
    def __init__(self, path):
        self.path = path
        self.d = read_json(path, {})

    @property
    def active(self):
        return bool(self.d.get("active"))

    def predict(self, feats):
        m = self.d.get("model")
        return _predict(m, [feats[k] for k in AI_FEATURES]) if m else None

    @property
    def threshold(self):
        return self.d.get("threshold", 0.5)

    def stale(self, now, hours):
        return now - self.d.get("trained_at", 0) > hours * 3600

    def train(self, samples, cfg, now):
        """Walk-forward test, then fit the final model on everything."""
        report = {"trained_at": now, "samples": len(samples), "active": False, "model": None}
        if len(samples) < 40 or len({s["y"] for s in samples}) < 2:
            report["note"] = "not enough historical signals yet"
            self.d = report
            write_json(self.path, self.d)
            return report
        n = len(samples)
        start = int(n * 0.4)
        folds = 5
        size = max(1, (n - start) // folds)
        evals = []
        for f in range(folds):
            lo = start + f * size
            hi = n if f == folds - 1 else lo + size
            test = samples[lo:hi]
            if not test:
                continue
            cutoff = test[0]["ts"]
            train = [s for s in samples[:lo] if s["exit_ts"] <= cutoff]  # no peeking past the test start
            if len(train) < 30 or len({s["y"] for s in train}) < 2:
                continue
            m = _fit([s["x"] for s in train], [s["y"] for s in train])
            thr = sum(s["y"] for s in train) / len(train)
            for s in test:
                evals.append((_predict(m, s["x"]), s["y"], s["r"], thr))
        all_r = [e[2] for e in evals]
        took = [e for e in evals if e[0] >= e[3]]
        auc = _auc([(e[0], e[1]) for e in evals])
        report.update({
            "tested": len(evals),
            "auc": auc,
            "win_rate_all": sum(e[1] for e in evals) / len(evals) if evals else None,
            "avg_r_all": sum(all_r) / len(all_r) if all_r else None,
            "avg_r_taken": sum(e[2] for e in took) / len(took) if took else None,
            "take_rate": len(took) / len(evals) if evals else None,
        })
        final = _fit([s["x"] for s in samples], [s["y"] for s in samples])
        report["model"] = final
        report["threshold"] = sum(s["y"] for s in samples) / n
        report["active"] = bool(
            cfg["ai"] and report["tested"] >= cfg["ai_min_samples"] and auc is not None
            and auc >= cfg["ai_min_auc"] and report["avg_r_taken"] is not None
            and report["avg_r_taken"] >= report["avg_r_all"] + 0.05 and report["take_rate"] >= 0.3)
        self.d = report
        write_json(self.path, self.d)
        return report


def describe_brain(d):
    if not d:
        return "AI: not trained yet (it trains on first run, or now with ./deckbot learn)"
    if d.get("note"):
        return f"AI: {d['note']} ({d.get('samples', 0)} signals)"
    auc = d.get("auc")
    line = (f"AI: {'ACTIVE - skipping weak breakouts' if d.get('active') else 'shadow mode (scoring, not acting)'}"
            f" | {d.get('samples', 0)} historical signals, walk-forward test on {d.get('tested', 0)}: "
            f"AUC {'n/a' if auc is None else f'{auc:.2f}'} (0.50 = coin flip)")
    if d.get("avg_r_all") is not None and d.get("avg_r_taken") is not None:
        line += (f"\n    take every signal: {d['avg_r_all']:+.2f}R avg ({d['win_rate_all']:.0%} winners)  |  "
                 f"only AI-approved ({d['take_rate']:.0%} of them): {d['avg_r_taken']:+.2f}R avg")
    if d.get("trained_at"):
        line += f"\n    last trained {datetime.fromtimestamp(d['trained_at'], timezone.utc):%Y-%m-%d %H:%M} UTC"
    return line


# ---------------------------------------------------------------------------
# Exchange access
# ---------------------------------------------------------------------------

def make_exchange(exchange_id, keys=None):
    import ccxt  # imported lazily so `status`/`setup` work before install finishes
    cls = getattr(ccxt, exchange_id)
    params = {"enableRateLimit": True, "timeout": 20000}
    if keys:
        params["apiKey"] = keys.get("apiKey", "")
        params["secret"] = keys.get("secret", "")
    return cls(params)


def fetch_closed_candles(ex, symbol, timeframe, now_ms=None, bars=300):
    """Closed candles only. Builds 4h candles from 1h where the exchange has no 4h (Coinbase)."""
    now_ms = now_ms or int(time.time() * 1000)
    tf_ms = TF_SECONDS[timeframe] * 1000
    if timeframe in ex.timeframes:
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=bars)
    else:
        hour_ms = 3600 * 1000
        per = tf_ms // hour_ms
        since = now_ms - bars * tf_ms
        hourly = []
        while since < now_ms:
            batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=300)
            if not batch:
                break
            hourly.extend(batch)
            nxt = batch[-1][0] + hour_ms
            if nxt <= since:
                break
            since = nxt
        dedup = {c[0]: c for c in hourly}
        hourly = [dedup[k] for k in sorted(dedup)]
        raw, bucket = [], []
        for c in hourly:
            start = c[0] - (c[0] % tf_ms)
            if bucket and bucket[0][0] - (bucket[0][0] % tf_ms) != start:
                if len(bucket) == per:
                    raw.append(_merge(bucket, tf_ms))
                bucket = []
            bucket.append(c)
        if len(bucket) == per:
            raw.append(_merge(bucket, tf_ms))
    return [c for c in raw if c[0] + tf_ms <= now_ms]


def _merge(bucket, tf_ms):
    start = bucket[0][0] - (bucket[0][0] % tf_ms)
    return [start, bucket[0][1], max(c[2] for c in bucket), min(c[3] for c in bucket),
            bucket[-1][4], sum(c[5] or 0 for c in bucket)]


# ---------------------------------------------------------------------------
# Trader: one per exchange account
# ---------------------------------------------------------------------------

class Trader:
    def __init__(self, cfg, account, keys, ex=None, clock=time.time):
        self.cfg = cfg
        self.acct = account
        self.id = account["exchange"]
        self.quote = account.get("quote", "USD")
        self.live = cfg["mode"] == "live"
        self.ex = ex or make_exchange(self.id, keys if self.live else None)
        self.clock = clock
        self.path = state_path(self.id)
        self.state = read_json(self.path, None)
        if not self.state or self.state.get("mode") != cfg["mode"]:
            self.state = self.fresh_state()
        self.markets_loaded = False
        self.errors_in_row = 0
        self.brain = Brain(os.path.join(DATA, f"brain-{self.id}.json"))

    def fresh_state(self):
        budget = float(self.acct["budget"])
        return {
            "mode": self.cfg["mode"],
            "start_equity": budget,
            "cash": budget,
            "positions": {},
            "last_candle": {},
            "cooldown_until": {},
            "day": "",
            "day_start_equity": budget,
            "peak_equity": budget,
            "halted_today": False,
            "paused": False,
            "realized_pnl": 0.0,
            "trades": [],
        }

    def save(self):
        write_json(self.path, self.state)

    # -- helpers --
    def symbol(self, coin):
        return f"{coin}/{self.quote}"

    def ensure_markets(self):
        if not self.markets_loaded:
            self.ex.load_markets()
            self.markets_loaded = True

    def price(self, symbol):
        t = self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close") or t.get("bid"))

    def equity(self, prices):
        eq = self.state["cash"]
        for sym, p in self.state["positions"].items():
            eq += p["amount"] * prices.get(sym, p["entry"])
        return eq

    def balances(self):
        b = self.ex.fetch_balance()
        return b

    def free(self, bal, code):
        return float((bal.get(code) or {}).get("free") or 0)

    def total(self, bal, code):
        return float((bal.get(code) or {}).get("total") or 0)

    def record(self, kind, symbol, amount, price, pnl=None, note=""):
        t = {"time": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds"),
             "side": kind, "symbol": symbol, "amount": amount, "price": price}
        if pnl is not None:
            t["pnl"] = round(pnl, 2)
        if note:
            t["note"] = note
        self.state["trades"] = (self.state["trades"] + [t])[-200:]
        tag = "PAPER " if not self.live else ""
        msg = f"{tag}{kind.upper()} {amount:.6g} {symbol} @ {price:.6g}"
        if pnl is not None:
            msg += f"  P&L {pnl:+.2f} {self.quote}"
        if note:
            msg += f"  ({note})"
        log.info(f"[{self.id}] {msg}")
        notify(self.cfg, f"{kind} {symbol}", msg)

    # -- order execution --
    def buy(self, symbol, cost, price):
        if not self.live:
            fee = PAPER_FEES.get(self.id, 0.005)
            fill = price * (1 + PAPER_SLIPPAGE)
            amount = cost * (1 - fee) / fill
            return amount, cost, fill
        base = symbol.split("/")[0]
        before = self.balances()
        if self.free(before, self.quote) < cost:
            cost = self.free(before, self.quote) * 0.995
        cost = float(self.ex.cost_to_precision(symbol, cost))
        self.ex.create_market_buy_order_with_cost(symbol, cost)
        got, spent = 0.0, 0.0
        for _ in range(10):  # wait for the fill to show up in balances
            time.sleep(1.5)
            after = self.balances()
            got = self.total(after, base) - self.total(before, base)
            spent = self.total(before, self.quote) - self.total(after, self.quote)
            if got > 0 and spent > 0:
                break
        if got <= 0:
            raise RuntimeError(f"buy of {symbol} not reflected in balance")
        return got, spent, spent / got

    def sell(self, symbol, amount, price):
        """Returns quote proceeds (after fees)."""
        if not self.live:
            fee = PAPER_FEES.get(self.id, 0.005)
            return amount * price * (1 - PAPER_SLIPPAGE) * (1 - fee)
        base = symbol.split("/")[0]
        before = self.balances()
        amt = min(amount, self.free(before, base))
        amt = float(self.ex.amount_to_precision(symbol, amt))
        if amt <= 0:
            raise RuntimeError(f"nothing free to sell for {symbol}")
        self.ex.create_order(symbol, "market", "sell", amt)
        proceeds = 0.0
        for _ in range(10):
            time.sleep(1.5)
            after = self.balances()
            proceeds = self.total(after, self.quote) - self.total(before, self.quote)
            if proceeds > 0:
                break
        if proceeds <= 0:
            proceeds = amt * price * (1 - PAPER_FEES.get(self.id, 0.005))
            log.warning(f"[{self.id}] sell of {symbol} not yet in balance; booking estimated proceeds")
        return proceeds

    def place_stop(self, symbol, pos):
        if not (self.live and self.cfg["exchange_stops"]):
            return
        try:
            amt = float(self.ex.amount_to_precision(symbol, pos["amount"]))
            stop = float(self.ex.price_to_precision(symbol, pos["stop"]))
            if self.id == "coinbase":
                # Coinbase has no stop-market for spot; stop-limit with room to fill on a gap.
                limit = float(self.ex.price_to_precision(symbol, pos["stop"] * 0.95))
                o = self.ex.create_order(symbol, "limit", "sell", amt, limit, {"stopLossPrice": stop})
            else:
                o = self.ex.create_order(symbol, "market", "sell", amt, None, {"stopLossPrice": stop})
            pos["stop_order_id"] = o["id"]
            pos["stop_on_exchange"] = stop
        except Exception as e:
            pos["stop_order_id"] = None
            log.warning(f"[{self.id}] couldn't place exchange stop for {symbol}: {e} (bot-side stop still active)")

    def cancel_stop(self, symbol, pos):
        """Cancel the exchange stop. Returns the order if it had already filled."""
        oid = pos.get("stop_order_id")
        if not (self.live and oid):
            return None
        try:
            self.ex.cancel_order(oid, symbol)
            pos["stop_order_id"] = None
            return None
        except Exception:
            try:
                o = self.ex.fetch_order(oid, symbol)
                if o.get("status") == "closed":
                    return o
                pos["stop_order_id"] = None
            except Exception as e:
                log.warning(f"[{self.id}] couldn't check stop order {oid}: {e}")
            return None

    # -- position management --
    def close(self, symbol, price, reason):
        pos = self.state["positions"][symbol]
        filled = self.cancel_stop(symbol, pos)
        if filled:
            self.book_stop_fill(symbol, filled)
            return
        proceeds = self.sell(symbol, pos["amount"], price)
        pnl = proceeds - pos["cost"]
        self.state["cash"] += proceeds
        self.state["realized_pnl"] += pnl
        del self.state["positions"][symbol]
        if reason in ("stop", "exchange stop"):
            self.state["cooldown_until"][symbol] = self.clock() + self.cfg["cooldown_candles"] * TF_SECONDS[self.cfg["timeframe"]]
        self.record("sell", symbol, pos["amount"], proceeds / pos["amount"], pnl, reason)

    def book_stop_fill(self, symbol, order):
        pos = self.state["positions"].pop(symbol)
        filled = float(order.get("filled") or pos["amount"])
        avg = float(order.get("average") or order.get("price") or pos["stop"])
        fee = float((order.get("fee") or {}).get("cost") or filled * avg * PAPER_FEES.get(self.id, 0.005))
        proceeds = filled * avg - fee
        pnl = proceeds - pos["cost"]
        self.state["cash"] += proceeds
        self.state["realized_pnl"] += pnl
        self.state["cooldown_until"][symbol] = self.clock() + self.cfg["cooldown_candles"] * TF_SECONDS[self.cfg["timeframe"]]
        self.record("sell", symbol, filled, avg, pnl, "exchange stop")

    def reconcile(self, bal):
        """Live only: notice stops that filled on the exchange while we were away."""
        for symbol, pos in list(self.state["positions"].items()):
            oid = pos.get("stop_order_id")
            if oid:
                try:
                    o = self.ex.fetch_order(oid, symbol)
                    st = o.get("status")
                    if st == "closed":
                        self.book_stop_fill(symbol, o)
                        continue
                    if st in ("canceled", "expired", "rejected"):
                        pos["stop_order_id"] = None
                except Exception as e:
                    log.warning(f"[{self.id}] couldn't check stop for {symbol}: {e}")
            base = symbol.split("/")[0]
            if self.total(bal, base) < pos["amount"] * 0.5:
                log.warning(f"[{self.id}] {symbol} position is gone from the account (sold outside the bot?). "
                            f"Dropping it; its value is not credited back to the bot's cash.")
                notify(self.cfg, "position missing", f"{symbol} was sold outside DeckBot")
                del self.state["positions"][symbol]
                continue
            if self.cfg["exchange_stops"] and not pos.get("stop_order_id"):
                self.place_stop(symbol, pos)

    def update_day(self, eq):
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            self.state["day"] = day
            self.state["day_start_equity"] = eq
            if self.state["halted_today"]:
                log.info(f"[{self.id}] new day - daily loss limit reset")
            self.state["halted_today"] = False
        self.state["peak_equity"] = max(self.state["peak_equity"], eq)
        if not self.state["halted_today"] and eq < self.state["day_start_equity"] * (1 - self.cfg["daily_loss_limit"]):
            self.state["halted_today"] = True
            msg = f"down {self.cfg['daily_loss_limit']:.0%} today - no new buys until tomorrow (UTC)"
            log.warning(f"[{self.id}] {msg}")
            notify(self.cfg, "daily loss limit", msg)
        if not self.state["paused"] and eq < self.state["start_equity"] * (1 - self.cfg["max_drawdown"]):
            self.state["paused"] = True
            msg = (f"equity {eq:.2f} is {self.cfg['max_drawdown']:.0%} below the starting budget - "
                   f"new buys paused. Review, then run: ./deckbot resume")
            log.warning(f"[{self.id}] {msg}")
            notify(self.cfg, "PAUSED", msg)

    def step(self):
        """One cycle: protect positions, then act on any newly closed candles."""
        self.ensure_markets()
        cfg = self.cfg
        tf = cfg["timeframe"]
        if cfg["ai"] and self.brain.stale(self.clock(), cfg["ai_retrain_hours"]):
            try:
                self.learn()
            except Exception as e:
                log.warning(f"[{self.id}] AI training failed ({type(e).__name__}: {e}); retrying in an hour")
                self.brain.d["trained_at"] = self.clock() - (cfg["ai_retrain_hours"] - 1) * 3600
        symbols = [self.symbol(c) for c in cfg["coins"] if self.symbol(c) in self.ex.markets]
        for c in cfg["coins"]:
            if self.symbol(c) not in self.ex.markets:
                log.warning(f"[{self.id}] {self.symbol(c)} isn't traded on {self.id}; skipping")
        held = list(self.state["positions"].keys())
        prices = {s: self.price(s) for s in set(symbols) | set(held)}

        if self.live:
            self.reconcile(self.balances())

        # 1) stops, checked every cycle
        for symbol, pos in list(self.state["positions"].items()):
            if prices[symbol] <= pos["stop"]:
                self.close(symbol, prices[symbol], "stop")

        eq = self.equity(prices)
        self.update_day(eq)

        # 2) new candles: trailing stops, trend exits, entries
        now_ms = int(self.clock() * 1000)
        for symbol in symbols + [h for h in held if h not in symbols and h in self.state["positions"]]:
            candles = fetch_closed_candles(self.ex, symbol, tf, now_ms)
            if not candles or candles[-1][0] == self.state["last_candle"].get(symbol):
                continue
            sig = analyze(candles, cfg)
            self.state["last_candle"][symbol] = candles[-1][0]
            if not sig or not sig["atr"]:
                continue
            pos = self.state["positions"].get(symbol)
            if pos:
                pos["highest"] = max(pos["highest"], sig["close"])
                pos["atr"] = sig["atr"]
                if sig["exit"]:
                    self.close(symbol, prices[symbol], "trend ended")
                    continue
                new_stop = max(pos["stop"], pos["highest"] - cfg["trail_atr"] * sig["atr"])
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    on_ex = pos.get("stop_on_exchange")
                    if self.live and cfg["exchange_stops"] and (on_ex is None or new_stop - on_ex >= 0.5 * sig["atr"]):
                        filled = self.cancel_stop(symbol, pos)
                        if filled:
                            self.book_stop_fill(symbol, filled)
                            continue
                        self.place_stop(symbol, pos)
                continue
            if sig["entry"] and symbol in symbols:
                ai = self.ai_check(symbol, candles, now_ms)
                if ai is False:
                    continue
                self.try_enter(symbol, prices[symbol], sig, eq, ai)

        eq = self.equity(prices)
        self.state["last_equity"] = eq
        self.state["last_run"] = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
        self.save()
        return eq

    def learn(self):
        """Rebuild the training set from fresh history and retrain (walk-forward validated)."""
        self.ensure_markets()
        cfg = self.cfg
        now_ms = int(self.clock() * 1000)
        series = {}
        for coin in dict.fromkeys(cfg["coins"] + cfg["ai_coins"]):
            sym = self.symbol(coin)
            if sym not in self.ex.markets:
                continue
            try:
                candles = fetch_closed_candles(self.ex, sym, cfg["timeframe"], now_ms, bars=cfg["ai_history_candles"])
            except Exception as e:
                log.warning(f"[{self.id}] no history for {sym}: {e}")
                continue
            if len(candles) > 200:
                series[coin] = Series(candles, cfg)
        samples = build_dataset(series, series.get("BTC"), cfg, PAPER_FEES.get(self.id, 0.005))
        report = self.brain.train(samples, cfg, self.clock())
        log.info(f"[{self.id}] AI retrained on {len(series)} coins - " + describe_brain(report).replace("\n", " "))
        return report

    def ai_check(self, symbol, candles, now_ms):
        """None = no opinion, a note string = approved/shadow, False = vetoed."""
        if not self.cfg["ai"] or not self.brain.d.get("model"):
            return None
        try:
            s = Series(candles, self.cfg)
            i = len(candles) - 1
            if i < s.warm:
                return None
            btc = s
            if not symbol.startswith("BTC/"):
                btc_sym = self.symbol("BTC")
                if btc_sym in self.ex.markets:
                    btc = Series(fetch_closed_candles(self.ex, btc_sym, self.cfg["timeframe"], now_ms), self.cfg)
            p = self.brain.predict(s.features(i, btc))
        except Exception as e:
            log.warning(f"[{self.id}] AI couldn't score {symbol}: {e}")
            return None
        st = self.state
        st["ai_scored"] = st.get("ai_scored", 0) + 1
        good = p >= self.brain.threshold
        if self.brain.active and not good:
            st["ai_vetoed"] = st.get("ai_vetoed", 0) + 1
            log.info(f"[{self.id}] {symbol} breakout skipped by AI (score {p:.2f} < {self.brain.threshold:.2f})")
            notify(self.cfg, f"AI skipped {symbol}", f"breakout scored {p:.2f}, below the {self.brain.threshold:.2f} bar")
            return False
        mode = "" if self.brain.active else ", shadow"
        log.info(f"[{self.id}] {symbol} breakout AI score {p:.2f}{mode}")
        return f"AI {p:.2f}{mode}"

    def try_enter(self, symbol, price, sig, eq, ai_note=None):
        cfg, st = self.cfg, self.state
        why = None
        if st["paused"]:
            why = "bot paused (drawdown limit)"
        elif st["halted_today"]:
            why = "daily loss limit hit"
        elif len(st["positions"]) >= cfg["max_positions"]:
            why = "max positions open"
        elif self.clock() < st["cooldown_until"].get(symbol, 0):
            why = "cooling down after a stop-out"
        if why:
            log.info(f"[{self.id}] {symbol} buy signal skipped: {why}")
            return
        stop_dist = cfg["stop_atr"] * sig["atr"]
        if stop_dist <= 0 or stop_dist >= price:
            return
        risk_cash = eq * cfg["risk_per_trade"]
        cost = min(risk_cash / stop_dist * price, eq * cfg["max_position_pct"], st["cash"] * 0.99)
        m = self.ex.markets.get(symbol, {})
        lim = m.get("limits") or {}
        min_cost = float((lim.get("cost") or {}).get("min") or 0) or 1.0
        min_amt = float((lim.get("amount") or {}).get("min") or 0)
        if cost < max(min_cost, min_amt * price) * 1.05:
            log.info(f"[{self.id}] {symbol} buy signal skipped: ${cost:.2f} is below the exchange minimum")
            return
        amount, spent, fill = self.buy(symbol, cost, price)
        st["cash"] -= spent
        pos = {
            "amount": amount, "entry": fill, "cost": spent,
            "stop": fill - stop_dist, "highest": sig["close"], "atr": sig["atr"],
            "opened": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds"),
            "stop_order_id": None,
        }
        st["positions"][symbol] = pos
        self.record("buy", symbol, amount, fill, note=f"stop {pos['stop']:.6g}" + (f", {ai_note}" if ai_note else ""))
        self.place_stop(symbol, pos)

    def sell_all(self):
        self.ensure_markets()
        for symbol in list(self.state["positions"]):
            self.close(symbol, self.price(symbol), "manual sell-all")
        self.save()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def build_traders(cfg):
    keys = read_json(KEYS_PATH, {})
    traders = []
    for acct in cfg["accounts"]:
        k = keys.get(acct["exchange"])
        if cfg["mode"] == "live" and not k:
            log.error(f"No API keys for {acct['exchange']} - run ./deckbot setup")
            continue
        traders.append(Trader(cfg, acct, k))
    return traders


def cmd_run(args):
    setup_logging(to_console=sys.stdout.isatty())
    cfg = load_config()
    if not cfg["accounts"]:
        print("Not set up yet - run ./deckbot setup")
        return 1
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    log.info(f"DeckBot started in {cfg['mode'].upper()} mode ({cfg['risk_profile']} risk) "
             f"on {', '.join(a['exchange'] for a in cfg['accounts'])}")
    traders = build_traders(cfg)
    if not getattr(args, "once", False):
        start_panel(PANEL_PATH, panel_snapshot, panel_action, default_port=8765)
    backoff = 0
    while not stop["now"]:
        ok = True
        for t in traders:
            try:
                eq = t.step()
                t.errors_in_row = 0
                log.info(f"[{t.id}] equity {eq:,.2f} {t.quote} | cash {t.state['cash']:,.2f} | "
                         f"{len(t.state['positions'])} open")
            except Exception as e:
                ok = False
                t.errors_in_row += 1
                log.error(f"[{t.id}] cycle failed: {type(e).__name__}: {e}")
                if t.errors_in_row == 5:
                    notify(cfg, "errors", f"{t.id}: 5 failed cycles in a row - last: {e}")
                try:
                    t.save()
                except Exception:
                    pass
        if args.once:
            break
        backoff = 0 if ok else min(backoff + 60, 900)
        wait = cfg["check_every_seconds"] + backoff
        for _ in range(int(wait)):
            if stop["now"]:
                break
            time.sleep(1)
        # pick up pause/resume/mode changes made from the CLI
        new_cfg = load_config()
        def risk_of(c):
            return tuple(c[k] for k in ("risk_profile", "risk_per_trade", "daily_loss_limit", "max_drawdown"))
        if (new_cfg["mode"] != cfg["mode"] or new_cfg["accounts"] != cfg["accounts"]
                or risk_of(new_cfg) != risk_of(cfg)):
            log.info(f"Config changed - reloading ({new_cfg['risk_profile']} risk)")
            cfg = new_cfg
            traders = build_traders(cfg)
        else:
            for t in traders:
                fresh = read_json(t.path, None)
                if fresh and fresh.get("paused") != t.state["paused"]:
                    # pause/resume from the CLI; resume also resets the drawdown baseline
                    t.state["paused"] = fresh["paused"]
                    t.state["start_equity"] = fresh.get("start_equity", t.state["start_equity"])
                    log.info(f"[{t.id}] {'paused' if t.state['paused'] else 'resumed'} from the command line")
    log.info("DeckBot stopped")
    return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def ask(prompt, default=None, choices=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            val = str(default)
        if choices and val.lower() not in choices:
            print(f"  Please type one of: {', '.join(choices)}")
            continue
        if val:
            return val


def ask_number(prompt, default, lo, hi):
    """Like ask(), but keeps asking until the answer is a number between lo and hi."""
    while True:
        val = ask(prompt, default).replace(",", "").replace("$", "").strip()
        try:
            n = float(val)
        except ValueError:
            print(f"  Please type a number, like {default}.")
            continue
        if not lo <= n <= hi:
            fmt = lambda x: f"{x:,.0f}" if x >= 1000 else f"{x:g}"
            print(f"  Please pick a number between {fmt(lo)} and {fmt(hi)}.")
            continue
        return n


def ask_keys(exchange_id):
    print()
    if exchange_id == "kraken":
        print("Kraken: Settings > API > Create API key. Allow ONLY: Query Funds, Query Open/Closed Orders,")
        print("Create & Modify Orders, Cancel/Close Orders. Never enable withdrawals.")
        key = getpass.getpass("  API key (hidden): ").strip()
        secret = getpass.getpass("  Private key (hidden): ").strip()
        return {"apiKey": key, "secret": secret}
    print("Coinbase: coinbase.com/settings/api > Create API key with 'Trade' permission only (no Transfer).")
    print("It downloads a JSON file (cdp_api_key.json). Easiest: copy it to the Deck and give its path here.")
    path = input("  Path to the key JSON file (or Enter to paste manually): ").strip().strip("'\"")
    if path:
        data = read_json(os.path.expanduser(path), {})
        name = data.get("name") or data.get("id")
        secret = data.get("privateKey")
        if name and secret:
            return {"apiKey": name, "secret": secret}
        print("  That file didn't look like a Coinbase key file - paste manually instead.")
    name = input("  API key name/id: ").strip()
    print("  Paste the private key. For a multi-line key, finish with an empty line:")
    lines = []
    while True:
        line = getpass.getpass("") if not lines else input()
        if not line.strip():
            break
        lines.append(line.strip())
    return {"apiKey": name, "secret": "\n".join(lines)}


def check_keys(exchange_id, keys, quote):
    try:
        ex = make_exchange(exchange_id, keys)
        bal = ex.fetch_balance()
        free = float((bal.get(quote) or {}).get("free") or 0)
        print(f"  ✓ Connected to {exchange_id}. Available {quote}: {free:,.2f}")
        return free
    except Exception as e:
        print(f"  ✗ {exchange_id} rejected the keys: {type(e).__name__}: {e}")
        return None


def cmd_setup(args):
    cfg = load_config()
    if cfg["mode"] == "live" and any(read_json(state_path(a["exchange"]), {}).get("positions")
                                     for a in cfg["accounts"]):
        print("The bot holds live positions. Run ./deckbot sell-all (or wait for them to close) before")
        print("re-running setup, so it doesn't lose track of them.")
        return 1
    print("\n=== DeckBot setup ===")
    print("Starts in PAPER mode: real prices, pretend money. Nothing is traded until you run ./deckbot go-live.\n")
    choice = ask("Exchange: kraken, coinbase, or both", "kraken", ["kraken", "coinbase", "both"]).lower()
    exchanges = ["kraken", "coinbase"] if choice == "both" else [choice]
    quote = ask("Trade against which currency (USD, USDC, USDT, EUR, GBP, CAD...)", "USD",
                ["usd", "usdc", "usdt", "eur", "gbp", "cad", "aud", "chf", "jpy"]).upper()
    accounts = []
    for x in exchanges:
        budget = ask_number(f"How much {quote} may the bot use on {x}", 500, 10, 10_000_000)
        accounts.append({"exchange": x, "budget": budget, "quote": quote})
    coins = ask("Coins to trade (comma separated)", ",".join(cfg["coins"]))
    cfg["coins"] = [c.strip().upper() for c in coins.split(",") if c.strip()]
    cfg["risk_profile"] = ask("Risk profile: conservative, moderate, aggressive",
                              cfg["risk_profile"], list(RISK_PRESETS)).lower()
    cfg["accounts"] = accounts
    topic = ask("Phone alerts via the free ntfy app - pick a private topic name, or 'none'",
                cfg.get("ntfy_topic") or "none")
    cfg["ntfy_topic"] = "" if topic.lower() == "none" else topic
    cfg["mode"] = "paper"

    if ask("Add exchange API keys now? (needed only for live trading) yes/no", "no", ["yes", "no"]) == "yes":
        keys = read_json(KEYS_PATH, {})
        for a in accounts:
            k = ask_keys(a["exchange"])
            if check_keys(a["exchange"], k, quote) is not None:
                keys[a["exchange"]] = k
        write_json(KEYS_PATH, keys, private=True)

    save_config(cfg)
    for a in accounts:
        if os.path.exists(state_path(a["exchange"])):
            os.remove(state_path(a["exchange"]))
    print("\nSaved. Next:")
    print("  ./deckbot start     run in the background (keeps going in Game Mode)")
    print("  ./deckbot status    see what it's doing")
    if cfg["ntfy_topic"]:
        print(f"  Install the ntfy app on your phone and subscribe to: {cfg['ntfy_topic']}")
    return 0


# ---------------------------------------------------------------------------
# Control panel (dashboard + Decky quick panel). The panel reads the same files the bot writes and
# runs the same commands as the terminal, so both always agree.
# ---------------------------------------------------------------------------

def _open_live_positions(cfg):
    return any(read_json(state_path(a["exchange"]), {}).get("positions") for a in cfg["accounts"])


def panel_snapshot():
    cfg = load_config()
    keys = read_json(KEYS_PATH, {})
    equity = pnl_total = pnl_day = 0.0
    positions, raw, paused, halted, updated, ai_lines = [], [], False, False, "", []
    budget = 0.0
    quote = cfg["accounts"][0]["quote"] if cfg["accounts"] else "USD"
    for a in cfg["accounts"]:
        st = read_json(state_path(a["exchange"]), None)
        budget += a["budget"]
        if cfg["ai"]:
            ai_lines.append(describe_brain(read_json(os.path.join(DATA, f"brain-{a['exchange']}.json"), {})))
        if not st:
            continue
        eq = st.get("last_equity", st["cash"])
        equity += eq
        pnl_total += eq - a["budget"]
        pnl_day += eq - st.get("day_start_equity", eq)
        paused = paused or st.get("paused", False)
        halted = halted or st.get("halted_today", False)
        updated = max(updated, st.get("last_run", ""))
        for sym, p in st["positions"].items():
            positions.append({"name": f"{sym} on {a['exchange']}",
                              "detail": f"{p['amount']:.6g} at {p['entry']:.6g}, stop {p['stop']:.6g}",
                              "pnl_pct": None})
        for t in st["trades"]:
            raw.append({"time": t["time"][:16].replace("T", " "), "side": t["side"], "name": t["symbol"],
                        "amount": f"{t['amount']:.6g}", "pnl": t.get("pnl"), "_sort": t["time"]})
    raw.sort(key=lambda t: t["_sort"])
    curve, run = [], 0.0
    for t in raw:
        if t["pnl"] is not None:
            run += t["pnl"]
            curve.append(round(run, 4))
    trades = [{k: v for k, v in t.items() if k != "_sort"} for t in reversed(raw[-60:])]
    rp = cfg["risk_profile"]
    return {
        "bot": "DeckBot", "currency": quote, "decimals": 2,
        "mode": cfg["mode"], "paused": paused, "halted": halted,
        "service": service_state(), "updated": updated or "not started yet",
        "health": health(updated or None, cfg["check_every_seconds"]),
        "equity": equity if cfg["accounts"] else None, "equity_label": "Equity",
        "pnl_total": pnl_total, "pnl_day": pnl_day,
        "open_count": len(positions), "max_open": cfg["max_positions"],
        "positions": positions, "trades": trades, "curve": curve,
        "ai_text": "\n".join(ai_lines) if cfg["ai"] else "AI is switched off in the settings.",
        "risk_profile": rp, "presets": list(RISK_PRESETS),
        "risk_text": (f"{rp.title()}: risks {cfg['risk_per_trade']:.0%} of the budget per trade, daily stop "
                      f"-{cfg['daily_loss_limit']:.0%}, kill-switch at -{cfg['max_drawdown']:.0%}."),
        "sizing": {"show": False},
        "keys_ready": all(a["exchange"] in keys for a in cfg["accounts"]) and bool(cfg["accounts"]),
    }


def panel_action(name, value, confirm):
    cfg = load_config()
    if name == "risk":
        return run_cli(SCRIPT, ["risk", str(value)])
    if name in ("pause", "resume"):
        return run_cli(SCRIPT, [name])
    if name == "paper":
        if cfg["mode"] == "live" and _open_live_positions(cfg):
            return False, "The bot still holds live positions. Sell everything first."
        return run_detached(SCRIPT, ["paper"], "paper")
    if name == "go_live":
        if confirm != "GO LIVE":
            return False, "Type GO LIVE to confirm."
        if not all(a["exchange"] in read_json(KEYS_PATH, {}) for a in cfg["accounts"]):
            return False, "Add the exchange keys in the terminal first."
        return run_detached(SCRIPT, ["go-live", "--yes"], "golive")
    if name == "sell_all":
        if confirm != "SELL":
            return False, "Type SELL to confirm."
        return run_detached(SCRIPT, ["sell-all", "--yes"], "sellall")
    return False, "Unknown action."


def cmd_dashboard(args):
    if args.setting == "lan":
        if args.value not in ("on", "off"):
            print("Use: dashboard lan on  or  dashboard lan off")
            return 1
        set_panel_settings(PANEL_PATH, lan=args.value == "on")
        print(f"Phone/LAN access {'on' if args.value == 'on' else 'off'}. Restart the bot to apply.")
        if service_state() == "running":
            restart_service()
    elif args.setting == "reset-token":
        reset_token(PANEL_PATH)
        print("New token made. Old links stop working.")
        if service_state() == "running":
            restart_service()
    local, lan = dashboard_links(PANEL_PATH, default_port=8765)
    print(f"Dashboard on this Deck: {local}")
    if lan:
        print(f"From your phone (same Wi-Fi): {lan}")
    print("Keep the link private: anyone with it can trade your account.")
    return 0


def cmd_status(args):
    cfg = load_config()
    print(f"Mode: {cfg['mode'].upper()}   Risk: {cfg['risk_profile']} "
          f"({cfg['risk_per_trade']:.0%}/trade, daily stop {cfg['daily_loss_limit']:.0%}, "
          f"kill-switch at -{cfg['max_drawdown']:.0%})")
    print(f"Service: {service_state()}")
    for a in cfg["accounts"]:
        st = read_json(state_path(a["exchange"]), None)
        print(f"\n[{a['exchange']}] budget {a['budget']:,.2f} {a['quote']}")
        if not st:
            print("  no activity yet")
            continue
        eq = st.get("last_equity", st["cash"])
        ret = eq / st["start_equity"] - 1
        flags = [f for f, on in (("PAUSED", st["paused"]), ("daily limit hit", st["halted_today"])) if on]
        print(f"  equity {eq:,.2f} ({ret:+.1%})  cash {st['cash']:,.2f}  realized {st['realized_pnl']:+,.2f}"
              f"  last run {st.get('last_run', '-')}" + (f"  [{', '.join(flags)}]" if flags else ""))
        for sym, p in st["positions"].items():
            prot = "exchange+bot" if p.get("stop_order_id") else "bot"
            print(f"  holding {p['amount']:.6g} {sym} from {p['entry']:.6g}, stop {p['stop']:.6g} ({prot})")
        if cfg["ai"]:
            print("  " + describe_brain(read_json(os.path.join(DATA, f"brain-{a['exchange']}.json"), {})))
            if st.get("ai_scored"):
                print(f"    live breakouts scored: {st['ai_scored']}, skipped by AI: {st.get('ai_vetoed', 0)}")
        for t in st["trades"][-5:]:
            pnl = f" P&L {t['pnl']:+.2f}" if "pnl" in t else ""
            print(f"  {t['time']} {t['side']} {t['amount']:.6g} {t['symbol']} @ {t['price']:.6g}{pnl}")
    return 0


def set_paused(value):
    cfg = load_config()
    for a in cfg["accounts"]:
        p = state_path(a["exchange"])
        st = read_json(p, None)
        if st:
            st["paused"] = value
            if not value:
                # restart the drawdown measurement from today's equity
                st["start_equity"] = st.get("last_equity", st["cash"])
            write_json(p, st)
    print("Paused: no new buys (stops still managed)." if value else "Resumed.")
    return 0


def cmd_go_live(args):
    cfg = load_config()
    keys = read_json(KEYS_PATH, {})
    print("LIVE MODE trades real money. Trend-following loses on many trades and wins on a few;")
    print(f"with '{cfg['risk_profile']}' risk a single stop-out costs ~{cfg['risk_per_trade']:.0%} of the budget.")
    for a in cfg["accounts"]:
        if a["exchange"] not in keys:
            if ask(f"No keys for {a['exchange']} yet - add them now? yes/no", "yes", ["yes", "no"]) == "no":
                return 1
            keys[a["exchange"]] = ask_keys(a["exchange"])
        free = check_keys(a["exchange"], keys[a["exchange"]], a["quote"])
        if free is None:
            return 1
        if free < a["budget"]:
            print(f"  Budget lowered to the available {free:,.2f} {a['quote']}.")
            a["budget"] = math.floor(free * 100) / 100
    write_json(KEYS_PATH, keys, private=True)
    if not getattr(args, "yes", False) and input('Type GO LIVE to confirm: ').strip() != "GO LIVE":
        print("Cancelled - still in paper mode.")
        return 1
    cfg["mode"] = "live"
    save_config(cfg)
    print("Live mode on. Paper positions were discarded; the bot starts fresh with your budget.")
    if service_state() == "running":
        restart_service()
    return 0


def cmd_paper(args):
    cfg = load_config()
    if cfg["mode"] == "live":
        st_open = any(read_json(state_path(a["exchange"]), {}).get("positions") for a in cfg["accounts"])
        if st_open:
            print("The bot still holds live positions. Run ./deckbot sell-all first, or they'll be left unmanaged.")
            return 1
    cfg["mode"] = "paper"
    save_config(cfg)
    print("Switched to paper mode.")
    if service_state() == "running":
        restart_service()
    return 0


def cmd_risk(args):
    profile = args.profile.lower()
    if profile not in RISK_PRESETS:
        print(f"Pick one of: {', '.join(RISK_PRESETS)}")
        return 1
    cfg = load_config()
    stored = read_json(CONFIG_PATH, {})
    stored["risk_profile"] = profile
    # a custom override in config.json would hide the preset, so drop them when a preset is chosen
    for k in ("risk_per_trade", "daily_loss_limit", "max_drawdown"):
        stored.pop(k, None)
    write_json(CONFIG_PATH, stored)
    cfg = load_config()
    print(f"Risk profile: {profile} - {cfg['risk_per_trade']:.0%} risked per trade, daily stop "
          f"{cfg['daily_loss_limit']:.0%}, kill-switch at -{cfg['max_drawdown']:.0%}.")
    if cfg["mode"] == "live" or service_state() == "running":
        print("The running bot picks this up within about a minute.")
    return 0


def cmd_sell_all(args):
    setup_logging()
    cfg = load_config()
    if not getattr(args, "yes", False) and input(f"Sell everything the bot holds ({cfg['mode']} mode)? Type SELL: ").strip() != "SELL":
        return 1
    stop_service()
    for t in build_traders(cfg):
        t.sell_all()
    print("Done. The bot is stopped; ./deckbot start to run it again.")
    return 0


# -- background service (systemd --user) --

def systemctl(*a):
    if not shutil.which("systemctl"):
        return None
    return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)


def service_state():
    r = systemctl("is-active", SERVICE_NAME)
    if r is None:
        return "systemd not available"
    return {"active": "running", "inactive": "stopped"}.get(r.stdout.strip(), r.stdout.strip() or "not installed")


def install_service():
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    py = os.path.join(HERE, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    with open(os.path.join(unit_dir, SERVICE_NAME), "w") as f:
        f.write(f"""[Unit]
Description=DeckBot crypto trading bot
After=network-online.target

[Service]
ExecStart={py} {os.path.join(HERE, 'deckbot.py')} run
WorkingDirectory={HERE}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""")
    systemctl("daemon-reload")


def restart_service():
    systemctl("restart", SERVICE_NAME)
    print("Background bot restarted.")


def stop_service():
    systemctl("stop", SERVICE_NAME)


def cmd_start(args):
    if not load_config()["accounts"]:
        print("Run ./deckbot setup first.")
        return 1
    install_service()
    r = systemctl("enable", "--now", SERVICE_NAME)
    if r is None or r.returncode != 0:
        print("Couldn't start the background service:", (r.stderr if r else "no systemd"))
        print("You can still run it in a terminal with: ./deckbot run")
        return 1
    print("DeckBot is running in the background and will start automatically when the Deck boots.")
    print("It keeps working in Game Mode. It can't trade while the Deck is asleep -")
    print("exchange-side stop orders protect open positions in the meantime (live mode).")
    return 0


def cmd_stop(args):
    systemctl("disable", "--now", SERVICE_NAME)
    print("DeckBot stopped.")
    return 0


def cmd_learn(args):
    setup_logging(to_console=False)
    cfg = load_config()
    if not cfg["accounts"]:
        print("Run ./deckbot setup first.")
        return 1
    print("Replaying the strategy over ~4 months of history for each coin and training the AI...")
    print("(public market data only - takes a minute or two)\n")
    for t in build_traders(cfg):
        try:
            print(f"[{t.id}] " + describe_brain(t.learn()))
        except Exception as e:
            print(f"[{t.id}] training failed: {type(e).__name__}: {e}")
    if service_state() == "running":
        restart_service()
    return 0


def cmd_logs(args):
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()[-40:]
        print("".join(lines), end="")
    except FileNotFoundError:
        print("No log yet.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="deckbot", description="Trend-following crypto bot for Kraken and Coinbase")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="interactive setup")
    d = sub.add_parser("dashboard", help="show the dashboard link, or: dashboard lan on|off, dashboard reset-token")
    d.add_argument("setting", nargs="?", default="link", choices=["link", "lan", "reset-token"])
    d.add_argument("value", nargs="?")
    r = sub.add_parser("run", help="run in the foreground")
    r.add_argument("--once", action="store_true", help="run a single cycle and exit")
    k = sub.add_parser("risk", help="conservative | moderate | aggressive")
    k.add_argument("profile")
    for name, h in (("go-live", "switch to real trading"), ("sell-all", "close every bot position now")):
        sub.add_parser(name, help=h).add_argument("--yes", action="store_true", help="skip the typed confirmation")
    for name, h in [("status", "show equity, positions, recent trades"), ("learn", "retrain the AI now and show its test results"),
                    ("paper", "switch back to paper trading"), ("pause", "stop opening new trades"),
                    ("resume", "allow new trades again"),
                    ("start", "run in the background"), ("stop", "stop the background bot"),
                    ("restart", "restart the background bot"), ("logs", "show recent log lines")]:
        sub.add_parser(name, help=h)
    args = p.parse_args(argv)
    handlers = {
        "setup": cmd_setup, "run": cmd_run, "status": cmd_status, "learn": cmd_learn, "go-live": cmd_go_live,
        "paper": cmd_paper, "pause": lambda a: set_paused(True), "resume": lambda a: set_paused(False),
        "sell-all": cmd_sell_all, "risk": cmd_risk, "dashboard": cmd_dashboard, "start": cmd_start, "stop": cmd_stop,
        "restart": lambda a: (restart_service(), 0)[1], "logs": cmd_logs,
    }
    if args.cmd not in handlers:
        p.print_help()
        return 0
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
