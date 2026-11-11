"""Closing-line value (Phase 1.5).

For every fill in the `fills` table, once its market has closed:
  closing line  = the market's last traded YES price before close_time (same environment as the fill)
  CLV (cents)   = how much better your price was than the closing line, in your side's terms:
                    buy YES at p   -> CL - p          sell YES at p -> p - CL
                    buy NO         -> (1-CL) - noPx   sell NO       -> noPx - (1-CL)
                  i.e. sign * (CL - yes_price), sign = +1 for buy YES / sell NO, -1 for buy NO / sell YES.
Positive CLV means you bought cheaper (or sold dearer) than where the market finished; over many
trades it is the best early sign of real edge, long before P&L is statistically meaningful.
Averages are weighted by contracts. Grouped by strategy (or source for manual/outside fills),
category and series. The same definition is used by the backtester.

Fallbacks for the closing line: last trade (live, then /historical/trades) -> market last_price.
"""
from __future__ import annotations

import threading
import time
import traceback

from .kalshi_client import KalshiError
from .warehouse import iso_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS clv (
    env TEXT, fill_key TEXT, ticker TEXT, ts REAL, strategy TEXT, source TEXT, category TEXT, series TEXT,
    action TEXT, side TEXT, yes_price REAL, count REAL, close_ts REAL, closing_yes REAL, clv_cents REAL,
    method TEXT, status TEXT, updated REAL, PRIMARY KEY (env, fill_key)
);
CREATE INDEX IF NOT EXISTS clv_ts ON clv(env, ts);
"""
OPEN_STATUSES = {"active", "initialized", "inactive", "unopened", "open", "paused"}


def clv_sign(action: str | None, side: str | None, book_side: str | None = None) -> int:
    """+1 if the fill is long YES exposure (bought YES on the bid side of the YES book), -1 if short YES
    (sold YES / bought NO). Kalshi's book_side is unambiguous; action+side is the fallback."""
    if book_side in ("bid", "ask"):
        return 1 if book_side == "bid" else -1
    buy = (action or "buy").lower() != "sell"
    yes = (side or "yes").lower() == "yes"
    return 1 if buy == yes else -1


class CLVTracker:
    def __init__(self, engine):
        self.e = engine
        self.db = engine.db
        self.db.conn.executescript(SCHEMA)
        self.markets: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.state = {"last_run": None, "scored": 0, "pending": 0, "error": None}
        self._stop = threading.Event()

    # ------------------------------------------------------------ lookups --
    def _market(self, t) -> dict | None:
        m = self.markets.get(t)
        if m and m.get("_final"):
            return m
        c = self.e.client
        try:
            m = c.market(t)
        except KalshiError as ex:
            if ex.status != 404:
                raise
            m = c.request("GET", f"/historical/markets/{t}").get("market")
        if not m:
            return None
        closed = (m.get("status") not in OPEN_STATUSES) and (iso_ts(m.get("close_time")) or 0) <= time.time()
        m["_final"] = closed
        self.markets[t] = m
        return m

    def _event(self, et) -> dict:
        if et not in self.events:
            try:
                self.events[et] = self.e.client.request("GET", f"/events/{et}").get("event") or {}
            except Exception:
                self.events[et] = {}
        return self.events[et]

    def closing_line(self, m: dict) -> tuple[float | None, str]:
        t, close = m["ticker"], iso_ts(m.get("close_time"))
        c = self.e.client
        for path in ("/markets/trades", "/historical/trades"):
            try:
                page = c.request("GET", path, {"ticker": t, "max_ts": int(close) if close else None, "limit": 100})
            except KalshiError:
                continue
            best = None
            for tr in page.get("trades") or []:
                ts = iso_ts(tr.get("created_time"))
                if ts is None or (close and ts > close) or tr.get("yes_price_dollars") in (None, ""):
                    continue
                if best is None or ts > best[0]:
                    best = (ts, float(tr["yes_price_dollars"]))
            if best:
                return best[1], "last_trade"
        lp = m.get("last_price_dollars")
        if lp not in (None, "") and float(lp) > 0:
            return float(lp), "market_last_price"
        return None, "none"

    # -------------------------------------------------------------- score --
    def run_once(self) -> dict:
        env = self.e.cfg.env
        if not self.e.client:
            return {}
        todo = self.db.query(
            "SELECT f.* FROM fills f LEFT JOIN clv c ON c.env=f.env AND c.fill_key=f.fill_key "
            "WHERE f.env=? AND (c.fill_key IS NULL OR c.status='pending') ORDER BY f.ts LIMIT 2000", (env,))
        scored = pending = 0
        lines: dict[str, tuple] = {}
        for f in todo:
            t = f["ticker"]
            try:
                m = self._market(t)
            except Exception as ex:
                self.state["error"] = f"{t}: {ex}"
                continue
            if not m:
                continue
            ev = self._event(m.get("event_ticker"))
            base = (env, f["fill_key"], t, f["ts"], f["strategy"], f["order_source"] or "outside",
                    ev.get("category"), ev.get("series_ticker"), f["action"] or "buy", f["outcome_side"] or "yes",
                    float(f["yes_price"] or 0), float(f["count"] or 0), iso_ts(m.get("close_time")))
            if not m.get("_final"):
                self.db.execute("INSERT OR REPLACE INTO clv VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'pending',?)",
                                base + (time.time(),))
                pending += 1
                continue
            if t not in lines:
                lines[t] = self.closing_line(m)
            cl, method = lines[t]
            if cl is None:
                self.db.execute("INSERT OR REPLACE INTO clv VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,'nodata',?)",
                                base + (method, time.time()))
                continue
            c = round(100 * clv_sign(f["action"], f["outcome_side"], f["book_side"]) * (cl - float(f["yes_price"] or 0)), 4)
            self.db.execute("INSERT OR REPLACE INTO clv VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'done',?)",
                            base + (cl, c, method, time.time()))
            scored += 1
        self.state.update(last_run=time.time(), scored=self.state["scored"] + scored, pending=pending)
        return {"scored": scored, "pending": pending}

    # ------------------------------------------------------------ reports --
    def summary(self, days: float = 30, env: str | None = None) -> dict:
        env = env or self.e.cfg.env
        since = time.time() - days * 86400
        rows = self.db.query("SELECT * FROM clv WHERE env=? AND ts>=?", (env, since))
        done = [r for r in rows if r["status"] == "done"]

        def agg(rs):
            n = sum(r["count"] for r in rs)
            w = sum(r["count"] * r["clv_cents"] for r in rs)
            pos = sum(r["count"] for r in rs if r["clv_cents"] > 0)
            return {"fills": len(rs), "contracts": round(n, 2),
                    "avg_clv_cents": round(w / n, 2) if n else None,
                    "total_clv_dollars": round(w / 100, 2),
                    "beat_close_pct": round(100 * pos / n, 1) if n else None}

        def group(key):
            g: dict[str, list] = {}
            for r in done:
                g.setdefault(key(r) or "?", []).append(r)
            return {k: agg(v) for k, v in sorted(g.items(), key=lambda kv: -sum(x["count"] for x in kv[1]))}

        return {"env": env, "days": days, "overall": agg(done),
                "pending": sum(1 for r in rows if r["status"] == "pending"),
                "nodata": sum(1 for r in rows if r["status"] == "nodata"),
                "by_strategy": group(lambda r: r["strategy"] or r["source"]),
                "by_category": group(lambda r: r["category"]),
                "by_series": group(lambda r: r["series"]),
                "recent": [{k: r[k] for k in ("ts", "ticker", "strategy", "source", "action", "side", "yes_price", "count",
                                              "closing_yes", "clv_cents", "status", "category")}
                           for r in sorted(rows, key=lambda r: -r["ts"])[:200]]}

    def strategy_clv(self, strategy: str, days: float = 30) -> dict:
        """For the capital allocator (3.2): contract-weighted CLV of one strategy over a window."""
        return self.summary(days)["by_strategy"].get(strategy, {"fills": 0, "contracts": 0, "avg_clv_cents": None})

    # --------------------------------------------------------------- loop --
    def loop(self):
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.run_once()
                self.state["error"] = None
            except Exception:
                self.state["error"] = traceback.format_exc()[-300:]
                self.db.audit("clv_error", self.state["error"])
            self._stop.wait(600)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="clv").start()

    def stop(self):
        self._stop.set()
