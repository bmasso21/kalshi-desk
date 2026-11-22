"""Early flow, paper test: follow large trades in a brand-new weather market's first hour.

Where it comes from (studies.newmkt / studies.newflow, Jul-Nov 2026): a single trade of 100+ contracts in a market's first
hour made its buyer +2.25c a contract after fees (t 4.4, 4,084 cases); buying the same side at the next real trade
within 10 minutes and 1c made +1.5c (t 1.6), +1.85c in weather (t 2.0). Positive in both halves but NOT proven, and
the backtest could only enter where someone else also bought. This runs the rule forward at REAL asks.

Every minute:
  1. Weather markets about to open are known in advance (Kalshi lists them 10 minutes to several hours before they
     open; the weather series are re-listed every 5 minutes). Each market open
     for less than 70 minutes, and due to stay open a day or more, is watched.
  2. One call per watched event gives every market's traded volume and quote. A market whose volume grew by 100+
     contracts has its new trades read; a single trade of 100+ contracts inside the first hour is a SIGNAL
     (side = whoever crossed the spread, price = what they paid).
  3. For 10 minutes after a signal: if the same side's ask is no more than 1c above the signal price, it is bought
     once on paper at that ask (one buy per market and side) and held to settlement on Kalshi's result.
Signals that never became buyable are kept too (the share that could be followed is part of the answer).
Read-only public market data. Places no orders."""
from __future__ import annotations

import datetime as dt
import threading
import time

from .sports import ensure_schema

TAKER = 0.07
CATEGORY = "Climate and Weather"
MIN_SIZE = 100                 # contracts in ONE trade
WINDOW = 3600                  # signals come from a market's first hour
WAIT = 600                     # seconds after the signal in which the side may be bought
SLIP = 0.01                    # at most this much above the signal price
MIN_LIFE = 86400               # markets open a day or more
DISCOVER_EVERY = 300           # seconds between looks for markets about to open. Kalshi creates the daily rain markets only
                               # 10 minutes before they open: a half-hourly look found them 20 minutes late (22 Nov 2026)
WALK_EVERY = 24 * 3600         # seconds between full walks of every weather series
LOOK_AHEAD = 6 * 3600
MAX_SERIES = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_signals (trade_id TEXT PRIMARY KEY, ts REAL, seen_ts REAL, ticker TEXT, event TEXT, series TEXT, side TEXT,
    price REAL, size REAL, open_ts REAL, close_ts REAL, status TEXT DEFAULT 'waiting', best_ask REAL);
CREATE TABLE IF NOT EXISTS flow_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, event TEXT, series TEXT, side TEXT,
    signal_price REAL, signal_size REAL, price REAL, fee REAL, close_ts REAL, status TEXT DEFAULT 'open', result TEXT, pnl REAL,
    settled_ts REAL, UNIQUE(ticker, side));
"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def taker_side(tr: dict):
    s = (tr.get("taker_outcome_side") or tr.get("taker_side") or "").lower()
    if s in ("yes", "no"):
        return s
    b = (tr.get("taker_book_side") or "").lower()
    return "yes" if b == "bid" else "no" if b == "ask" else None


def side_ask(m: dict, side: str):
    """What it costs to buy this side now: the YES ask, or 1 minus the YES bid for NO."""
    if side == "yes":
        a = fnum(m.get("yes_ask_dollars"))
        return a if a and 0 < a < 1 else None
    b = fnum(m.get("yes_bid_dollars"))
    return round(1 - b, 4) if b and 0 < b < 1 else None


class FlowWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client):
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self._series = (0.0, [], True)         # (when listed, tickers, True while a full walk is pending)
        self._found = 0.0                      # when markets about to open were last looked for
        self.upcoming: dict = {}               # ticker -> {open, close, event, series}
        self.vol: dict = {}                    # ticker -> traded volume at the last look
        self.state = {"running": False, "last": None, "error": None, "note": "", "watching": 0, "next_open": None, "series": 0}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    # ------------------------------------------------------------ discovery --
    def discover(self, now: float) -> int:
        """Markets of the weather series that open soon or opened within the watch window. One request per series."""
        if now - self._series[0] > WALK_EVERY or not self._series[1]:
            ss = [s["ticker"] for s in (self.k.series_list(CATEGORY) or []) if s.get("ticker")]
            self._series = (now, ss[:MAX_SERIES], True)
        live = set()
        for ser in self._series[1]:
            for m in self.k.paginate("/markets", "markets", {"series_ticker": ser, "limit": 100}, max_items=100):
                o, c, t = _ts(m.get("open_time")), _ts(m.get("close_time")), m.get("ticker")
                if not t or o is None or c is None:
                    continue
                if c > now - 2 * 86400:
                    live.add(ser)
                if c - o >= MIN_LIFE and now - WINDOW - WAIT < o <= now + LOOK_AHEAD and (m.get("market_type") or "binary") == "binary":
                    self.upcoming.setdefault(t, {"open": o, "close": c, "event": m.get("event_ticker") or t, "series": ser})
        if self._series[2]:                                       # that was the full walk: keep the series that are in use
            self._series = (self._series[0], sorted(live) or self._series[1], False)
        self._found = now
        for t in [t for t, u in self.upcoming.items() if u["open"] + WINDOW + WAIT < now]:
            self.upcoming.pop(t, None)
            self.vol.pop(t, None)
        self.state["series"] = len(self._series[1])
        return len(self.upcoming)

    # ---------------------------------------------------------------- watch --
    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        if now - self._found >= DISCOVER_EVERY:
            self.discover(now)
        active = {t: u for t, u in self.upcoming.items() if u["open"] <= now < u["open"] + WINDOW + WAIT}
        later = sorted(u["open"] for u in self.upcoming.values() if u["open"] > now)
        signals = picks = 0
        quotes: dict = {}
        for ev in sorted({u["event"] for u in active.values()}):
            for m in self.k.markets(status=None, event_ticker=ev, max_items=200):
                t = m.get("ticker")
                if t not in active:
                    continue
                quotes[t] = m
                v = fnum(m.get("volume_fp") or m.get("volume")) or 0.0
                grew = v - self.vol.get(t, 0.0)
                self.vol[t] = v
                if grew + 1e-9 < MIN_SIZE:
                    continue
                u = active[t]
                for tr in self.k.paginate("/markets/trades", "trades", {"ticker": t, "min_ts": int(u["open"]), "limit": 1000}, max_items=5000):
                    ts, n, side, yp = _ts(tr.get("created_time")), fnum(tr.get("count_fp") or tr.get("count")) or 0.0, taker_side(tr), fnum(tr.get("yes_price_dollars"))
                    if ts is None or side is None or yp is None or not 0 < yp < 1 or n + 1e-9 < MIN_SIZE or not u["open"] <= ts < u["open"] + WINDOW:
                        continue
                    cur = self.db.execute("INSERT OR IGNORE INTO flow_signals(trade_id, ts, seen_ts, ticker, event, series, side, price, size, open_ts, close_ts, status) "
                                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (tr.get("trade_id") or f"{t}:{ts}:{yp}:{n}", ts, now, t, u["event"], u["series"], side,
                                           round(yp if side == "yes" else 1 - yp, 4), n, u["open"], u["close"],
                                           "waiting" if now - ts <= WAIT else "late"))        # late = first seen after its 10 minutes (e.g. the app was restarted)
                    signals += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        # signals still inside their 10 minutes: is the same side buyable within 1c?
        for s in self.db.query("SELECT * FROM flow_signals WHERE status='waiting'"):
            if now - s["ts"] > WAIT:
                self.db.execute("UPDATE flow_signals SET status='expired' WHERE trade_id=?", (s["trade_id"],))
                continue
            m = quotes.get(s["ticker"])
            ask = side_ask(m, s["side"]) if m else None
            if ask is None:
                continue
            if s["best_ask"] is None or ask < s["best_ask"]:
                self.db.execute("UPDATE flow_signals SET best_ask=? WHERE trade_id=?", (ask, s["trade_id"]))
            if ask <= s["price"] + SLIP + 1e-9 and self.opt("flow_enabled", True):
                cur = self.db.execute("INSERT OR IGNORE INTO flow_picks(ts, ticker, event, series, side, signal_price, signal_size, price, fee, close_ts) "
                                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (now, s["ticker"], s["event"], s["series"], s["side"], s["price"], s["size"], ask, round(TAKER * ask * (1 - ask), 6), s["close_ts"]))
                new = cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
                picks += new                                           # 'held' = that market and side was already bought on an earlier signal
                self.db.execute("UPDATE flow_signals SET status=? WHERE trade_id=?", ("bought" if new else "held", s["trade_id"]))
        self.settle(now)
        self.state.update(last=now, error=None, watching=len(active), next_open=later[0] if later else None,
                          note=f"{len(active)} new weather market(s) in their first 70 minutes, {len(later)} more opening within 6 h; "
                               f"{signals} new large trade(s), {picks} new paper buy(s)")
        return {"watching": len(active), "signals": signals, "picks": picks}

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT id, ticker, side, price, fee FROM flow_picks WHERE status='open' AND close_ts<?", (now,))
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower()
               for m in self.k.markets(status=None, tickers=list({r["ticker"] for r in rows})[:200], max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = (1.0 if r["side"] == x else 0.0) - r["price"] - r["fee"]
                self.db.execute("UPDATE flow_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(360)
        while not self._stop.is_set():
            if self.opt("flow_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("flow_every_sec", 60)))

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="flowwatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        rs = self.db.query("SELECT pnl, price, fee FROM flow_picks WHERE status='settled'")
        cost = sum(r["price"] + r["fee"] for r in rs)
        sg = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM flow_signals GROUP BY status")}
        return {"state": self.state, "rule": {"min_contracts": MIN_SIZE, "first_minutes": WINDOW // 60, "wait_minutes": WAIT // 60, "slip_c": int(SLIP * 100)},
                "signals": {"bought": sg.get("bought", 0), "expired": sg.get("expired", 0), "waiting": sg.get("waiting", 0), "seen_too_late": sg.get("late", 0), "already_held": sg.get("held", 0)},
                "totals": {"settled": len(rs), "won": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(sum(r["pnl"] for r in rs), 2),
                           "return_pct": round(100 * sum(r["pnl"] for r in rs) / cost, 1) if cost else None,
                           "open": self.db.query("SELECT COUNT(*) n FROM flow_picks WHERE status='open'")[0]["n"]},
                "upcoming": sorted(({"ticker": t, **u} for t, u in self.upcoming.items()), key=lambda x: (x["open"], x["ticker"]))[:60],
                "recent_signals": self.db.query("SELECT * FROM flow_signals ORDER BY ts DESC LIMIT 60"),
                "picks": self.db.query("SELECT * FROM flow_picks ORDER BY ts DESC LIMIT 100")}
