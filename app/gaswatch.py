"""Gas prices, paper test: tomorrow's AAA gas contracts against a model built from AAA figures already published.

The backtest (app/gasfv.py) found one lead: on DAILY gas contracts, buying in the morning when the trend model says a
contract is 5c+ too cheap made money over Sep-Nov 2026 - but only on the NO side, which may just be a season of falling
prices. This runs the rule forward at REAL asks, YES and NO scored separately, so the next months settle the question.

Each check:
  1. AAA's daily figure by date, rebuilt from Kalshi's settlement values (a day's contract settles on that day's figure).
  2. Nothing is bought until TODAY's figure is in (the contracts for tomorrow are then one day out).
  3. Every open daily contract for tomorrow is priced with both models (trend / no trend, see gasfv.Gas.forecast).
  4. The rule (trend model): a side priced 5c+ under fair after the fee, at 20-95c, between 8 a.m. and 6 p.m. Eastern,
     is bought once on paper at its real ask. The 20c floor and the 6 p.m. cut-off come from the backtest's breakdown
     (cheap longshots and evening entries did not pay), so they are fitted to the past: the paper result is the test.
Read-only public market data. Places no orders."""
from __future__ import annotations

import datetime as dt
import threading
import time

from . import gasfv as GS
from .goldfv import fetch_settles, phi
from .sports import ensure_schema

TAKER = 0.07
SERIES = "KXAAAGASD"
MIN_EDGE = 0.05
PRICE_LO, PRICE_HI = 0.20, 0.95
HOUR_LO, HOUR_HI = 8, 18          # Eastern: buy only from 8:00 to 17:59

SCHEMA = """
CREATE TABLE IF NOT EXISTS gas_board (ticker TEXT PRIMARY KEY, day TEXT, strike REAL, fair REAL, fair_flat REAL, yes_bid REAL,
    yes_ask REAL, yes_edge REAL, no_edge REAL, ts REAL);
CREATE TABLE IF NOT EXISTS gas_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, day TEXT, close_ts REAL, strike REAL,
    side TEXT, fair REAL, fair_flat REAL, base REAL, price REAL, fee REAL, edge REAL, status TEXT DEFAULT 'open', result TEXT,
    pnl REAL, settled_ts REAL, UNIQUE(ticker, side));
"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close_ts(m):
    c = m.get("close_time")
    try:
        return dt.datetime.fromisoformat(c.replace("Z", "+00:00")).timestamp() if c else None
    except ValueError:
        return None


class GasWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client):
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self._vals = (0.0, {})                 # (fetched at, {date: AAA figure})
        self.state = {"running": False, "last": None, "error": None, "note": "", "latest": None}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    def figures(self, now: float) -> dict:
        """{date: AAA figure} from Kalshi's settled daily gas contracts, refreshed at most every 20 minutes."""
        if now - self._vals[0] > 1200 or not self._vals[1]:
            vals = {GS.value_date(c): v for c, v in fetch_settles(self.k, SERIES, now - (GS.ERR_WIN + 25) * 86400).items()}
            self._vals = (now, vals)
        return self._vals[1]

    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        gas = GS.Gas(self.figures(now))
        today = GS.et(now).date()
        L, base = gas.latest(today)
        self.state.update(last=now, error=None, latest={"day": L.isoformat(), "figure": base} if L else None)
        self.settle(now)
        if L != today:
            self.state["note"] = f"waiting for today's AAA figure (latest in hand: {L.isoformat() if L else 'none'})"
            return {"rows": 0, "picks": 0, "waiting": True}
        target = today + dt.timedelta(days=1)
        mu, sd = gas.forecast(L, 1, True)
        mu0, sd0 = gas.forecast(L, 1, False)
        if mu is None or mu0 is None:
            self.state["note"] = "not enough consecutive AAA figures yet for the model"
            return {"rows": 0, "picks": 0, "waiting": True}
        hour = GS.et(now).hour
        in_hours = HOUR_LO <= hour < HOUR_HI
        min_edge = float(self.opt("gas_min_edge", MIN_EDGE))
        rows = picks = 0
        self.db.execute("DELETE FROM gas_board WHERE day<>?", (target.isoformat(),))
        for m in self.k.markets(status="open", series_ticker=SERIES, max_items=400):
            c, k = _close_ts(m), fnum(m.get("floor_strike"))
            if c is None or k is None or (m.get("strike_type") or "greater") != "greater" or GS.value_date(c) != target or c <= now:
                continue
            p, p0 = phi((mu - k) / sd), phi((mu0 - k) / sd0)
            yb, ya = fnum(m.get("yes_bid_dollars")) or 0.0, fnum(m.get("yes_ask_dollars"))
            ye = (p - ya - TAKER * ya * (1 - ya)) if ya and 0 < ya < 1 else None
            na = round(1 - yb, 4) if yb > 0 else None
            ne = ((1 - p) - na - TAKER * na * (1 - na)) if na and 0 < na < 1 else None
            self.db.execute("INSERT OR REPLACE INTO gas_board VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (m["ticker"], target.isoformat(), k, round(p, 4), round(p0, 4), yb, ya, ye, ne, now))
            rows += 1
            if not in_hours or not self.opt("gas_enabled", True):
                continue
            for side, edge, price, fair, flat in (("yes", ye, ya, p, p0), ("no", ne, na, 1 - p, 1 - p0)):
                if edge is not None and edge >= min_edge and PRICE_LO <= price <= PRICE_HI:
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO gas_picks(ts, ticker, day, close_ts, strike, side, fair, fair_flat, base, price, fee, edge) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, m["ticker"], target.isoformat(), c, k, side, round(fair, 4), round(flat, 4), base, price,
                         round(TAKER * price * (1 - price), 6), round(edge, 4)))
                    picks += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        self.state["note"] = (f"AAA {L.isoformat()}: ${base:.4f}; model expects ${mu:.4f} for {target.isoformat()} (+-{100 * sd:.1f}c), "
                              f"{rows} contracts priced, {picks} new paper buy(s)" + ("" if in_hours else "; outside buying hours (8am-6pm ET)"))
        return {"rows": rows, "picks": picks, "waiting": False}

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT id, ticker, side, price, fee FROM gas_picks WHERE status='open' AND close_ts<?", (now,))
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower()
               for m in self.k.markets(status=None, tickers=list({r["ticker"] for r in rows})[:200], max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = (1.0 if r["side"] == x else 0.0) - r["price"] - r["fee"]
                self.db.execute("UPDATE gas_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(180)
        while not self._stop.is_set():
            if self.opt("gas_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("gas_every_min", 20)) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="gaswatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        def side(s):
            rs = self.db.query("SELECT pnl, price, fee FROM gas_picks WHERE side=? AND status='settled'", (s,))
            cost = sum(r["price"] + r["fee"] for r in rs)
            return {"settled": len(rs), "won": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(sum(r["pnl"] for r in rs), 2),
                    "return_pct": round(100 * sum(r["pnl"] for r in rs) / cost, 1) if cost else None,
                    "open": self.db.query("SELECT COUNT(*) n FROM gas_picks WHERE side=? AND status='open'", (s,))[0]["n"]}
        return {"state": self.state, "min_edge_c": 100 * float(self.opt("gas_min_edge", MIN_EDGE)),
                "rule": {"price_c": [int(PRICE_LO * 100), int(PRICE_HI * 100)], "hours_et": [HOUR_LO, HOUR_HI]},
                "by_side": {"no": side("no"), "yes": side("yes")},
                "board": self.db.query("SELECT * FROM gas_board ORDER BY strike"),
                "picks": self.db.query("SELECT * FROM gas_picks ORDER BY ts DESC LIMIT 100")}
