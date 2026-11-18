"""Hourly cushion watch (paper only, places no orders).

For hourly "finishes above X" contracts (gold by default): at set minutes before each top-of-the-hour close, read
every strike's live bid/ask from Kalshi's public production data, work out where the market puts the underlying
(the level where "above X" is a 50/50 bet), and record a paper buy of the favoured side of every strike whose
cushion (distance from that level to the strike) is at least min_gap, at the REAL ask. Settles from Kalshi's result.

It exists to check the backtest's finding (buying with a $15+ cushion paid) against prices that could actually
have been paid, instead of "last trade + 1c"."""
from __future__ import annotations

import datetime as dt
import math
import threading
import time

from .sports import ensure_schema

TAKER = 0.07
CHECKS = (50, 40, 30, 20, 15, 10, 5)        # minutes before the close at which the ladder is read
FV_CHECKS = (50, 40, 30)                    # the fair-value paper rule: 30-60 minutes left (the backtest's only lead)
FV_MIN_EDGE, FV_LO, FV_HI = 0.02, 0.20, 0.80  # model edge after the fee; prices where the model was calibrated
BUCKETS = ((10, 15, "$10-15"), (15, 25, "$15-25"), (25, 1e9, "$25+"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS gw_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, series TEXT, ticker TEXT, close_ts REAL, check_min INTEGER,
    strike REAL, level REAL, gap REAL, side TEXT, ask REAL, bid REAL, last REAL, fee REAL,
    status TEXT DEFAULT 'open', result TEXT, pnl REAL, settled_ts REAL,
    UNIQUE(ticker, side, check_min)
);
CREATE TABLE IF NOT EXISTS gw_fv_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, close_ts REAL, check_min INTEGER, strike REAL, gold REAL,
    ladder REAL, fair REAL, side TEXT, ask REAL, edge REAL, fee REAL,
    status TEXT DEFAULT 'open', result TEXT, pnl REAL, settled_ts REAL,
    UNIQUE(ticker, side, check_min)
);
CREATE TABLE IF NOT EXISTS gw_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, series TEXT, close_ts REAL, check_min INTEGER,
    strikes INTEGER, level REAL, picks INTEGER, note TEXT
);
"""


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _iso(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def strike_of(m) -> float | None:
    st = (m.get("strike_type") or "").lower()
    if st in ("greater", "greater_or_equal") and fnum(m.get("floor_strike")) is not None:
        return fnum(m.get("floor_strike"))
    return None


def level_from_quotes(rows) -> float | None:
    """rows: [(strike, yes_mid)] -> the level where 'above X' is 50/50 (interpolated), or None."""
    pts = sorted((k, p) for k, p in rows if k is not None and p is not None)
    for (k1, p1), (k2, p2) in zip(pts, pts[1:]):
        if p1 >= 0.5 >= p2 and p1 > p2:
            return k1 + (p1 - 0.5) / (p1 - p2) * (k2 - k1)
    return None


def bucket(g: float) -> str | None:
    return next((lab for lo, hi, lab in BUCKETS if lo <= abs(g) < hi), None)


class GapWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client, fv=None):
        self._cfg = cfg_getter              # -> the "gapwatch" config section
        self.db = wdb
        self.k = kalshi_client              # public (unauthenticated) production client: market data only
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self.done: set = set()
        self.fv = fv                        # GoldLive (model price from live gold) or None
        self.state = {"running": False, "last_check": None, "error": None, "note": ""}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    @property
    def enabled(self) -> bool:
        return bool(self.opt("enabled", True))

    def series(self) -> list[str]:
        s = self.opt("series", ["KXGOLDH"])
        return [str(x) for x in (s if isinstance(s, list) else [s])][:5]

    # ------------------------------------------------------------- one check --
    def check(self, series: str, close_ts: float, check_min: int, now: float | None = None) -> dict:
        now = now or time.time()
        mk = [m for m in self.k.markets(status="open", series_ticker=series, max_items=500)
              if abs((_iso(m.get("close_time")) or 0) - close_ts) < 90]
        rows, quotes = [], []
        for m in mk:
            k = strike_of(m)
            yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
            if k is None or yb is None or ya is None or not (0 <= yb < ya <= 1):
                continue
            last = fnum(m.get("last_price_dollars"))
            quotes.append((k, m, yb, ya, last if last is not None and 0 < last < 1 else None))   # 0 = never traded
            if ya - yb <= 0.10:                                       # only tight quotes locate the level
                rows.append((k, (yb + ya) / 2))
        level = level_from_quotes(rows)
        picks = 0
        if level is not None:
            min_gap = float(self.opt("min_gap", 10))
            for k, m, yb, ya, last in quotes:
                g = level - k
                if abs(g) < min_gap:
                    continue
                side = "yes" if g > 0 else "no"
                ask = ya if side == "yes" else round(1 - yb, 4)
                bid = yb if side == "yes" else round(1 - ya, 4)
                if not 0.05 <= ask <= 0.99 or ask >= 1:
                    continue
                lp = (last if side == "yes" else (1 - last)) if last is not None else None
                cur = self.db.execute("INSERT OR IGNORE INTO gw_picks(ts, series, ticker, close_ts, check_min, strike, level, gap, side, ask, bid, last, fee) "
                                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (now, series, m.get("ticker"), close_ts, check_min, k, round(level, 3), round(abs(g), 3), side,
                                       ask, bid, lp, round(TAKER * ask * (1 - ask), 6)))
                picks += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        note = "" if level is not None else ("no contracts for this close" if not mk else "could not read the level (no tight quotes either side of 50c)")
        if self.fv is not None and check_min in FV_CHECKS and series == getattr(self.fv, "series", None):
            try:
                fvp = self.fv_check(close_ts, check_min, now, level)
                note = (note + f" · fair value: {fvp} paper buys").strip(" ·")
            except Exception as e:
                note = (note + f" · fair value failed: {e}"[:200]).strip(" ·")
        self.db.execute("INSERT INTO gw_checks(ts, series, close_ts, check_min, strikes, level, picks, note) VALUES(?,?,?,?,?,?,?,?)",
                        (now, series, close_ts, check_min, len(quotes), round(level, 3) if level is not None else None, picks, note))
        return {"strikes": len(quotes), "level": level, "picks": picks, "note": note}

    def fv_check(self, close_ts: float, check_min: int, now: float, ladder=None) -> int:
        """Paper buy of every side the live model rates at least FV_MIN_EDGE under fair, priced FV_LO..FV_HI, at the real ask."""
        b = self.fv.board(close_ts, now)
        n = 0
        for r in b.get("rows") or []:
            for side in ("yes", "no"):
                e, ask = r.get(f"{side}_edge"), r.get(f"{side}_ask")
                if e is None or ask is None or e < FV_MIN_EDGE or not FV_LO <= ask <= FV_HI:
                    continue
                fair = r["fair_yes"] if side == "yes" else 1 - r["fair_yes"]
                cur = self.db.execute("INSERT OR IGNORE INTO gw_fv_picks(ts, ticker, close_ts, check_min, strike, gold, ladder, fair, side, ask, edge, fee) "
                                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (now, r["ticker"], close_ts, check_min, r["strike"], b.get("gold"), round(ladder, 3) if ladder is not None else None,
                                       round(fair, 4), side, ask, e, round(TAKER * ask * (1 - ask), 6)))
                n += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        return n

    # -------------------------------------------------------------- settling --
    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT DISTINCT ticker FROM gw_picks WHERE status='open' AND close_ts < ? UNION "
                             "SELECT DISTINCT ticker FROM gw_fv_picks WHERE status='open' AND close_ts < ?", (now - 120, now - 120))
        n = 0
        tick = [r["ticker"] for r in rows]
        for i in range(0, len(tick), 50):
            for m in self.k.markets(status=None, tickers=tick[i:i + 50], max_items=100):
                res = (m.get("result") or "").lower()
                if res not in ("yes", "no"):
                    continue
                for tbl in ("gw_picks", "gw_fv_picks"):
                    for p in self.db.query(f"SELECT id, side, ask, fee FROM {tbl} WHERE ticker=? AND status='open'", (m.get("ticker"),)):
                        pnl = (1.0 if p["side"] == res else 0.0) - p["ask"] - p["fee"]
                        self.db.execute(f"UPDATE {tbl} SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (res, round(pnl, 6), now, p["id"]))
                        n += 1
        return n

    # ------------------------------------------------------------------ loop --
    def due(self, now: float) -> list[tuple[float, int]]:
        """(close, minutes) checks whose moment has come within the last two minutes and have not run yet."""
        nxt = math.ceil(now / 3600) * 3600
        out = []
        for m in CHECKS:
            t = nxt - m * 60
            if t <= now < t + 120 and (nxt, m) not in self.done:
                out.append((nxt, m))
        return out

    def tick(self, now: float | None = None):
        now = now or time.time()
        if not self.enabled:
            return
        for close, m in self.due(now):
            self.done.add((close, m))
            for s in self.series():
                r = self.check(s, close, m, now)
                self.state["last_check"] = now
                self.state["note"] = f"{s} {m} min before the close: {r['strikes']} strikes, level {r['level'] and round(r['level'], 2)}, {r['picks']} paper buys {r['note']}".strip()
        if len(self.done) > 500:
            self.done = {x for x in self.done if x[0] > now - 7200}
        if int(now) % 300 < 20:
            self.settle(now)

    def loop(self):
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.state["running"] = True
                self.tick()
                self.state["error"] = None
            except Exception as e:
                self.state["error"] = str(e)[:300]
            finally:
                self.state["running"] = False
            self._stop.wait(15)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="gapwatch").start()

    def stop(self):
        self._stop.set()

    # --------------------------------------------------------------- report --
    def summary(self) -> dict:
        rows = self.db.query("SELECT * FROM gw_picks WHERE status='settled'")

        def block(rs):
            if not rs:
                return {"n": 0}
            pnl = sum(r["pnl"] for r in rs)
            cost = sum(r["ask"] for r in rs)
            hours = len({r["close_ts"] for r in rs})
            slip = [r["ask"] - r["last"] for r in rs if r.get("last") is not None]
            return {"n": len(rs), "hours": hours, "wins": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(pnl, 2), "staked": round(cost, 2),
                    "return_pct": round(100 * pnl / cost, 2) if cost else None, "avg_ask_c": round(100 * cost / len(rs), 1),
                    "ask_over_last_c": round(100 * sum(slip) / len(slip), 2) if slip else None}
        first = {}
        for r in sorted(rows, key=lambda r: -r["check_min"]):             # each contract once: the earliest check it qualified at
            if r["gap"] >= 15:
                first.setdefault((r["ticker"], r["side"]), r)
        out = {"rule_15_first_touch": block(list(first.values())),
               "by_bucket": {lab: block([r for r in rows if bucket(r["gap"]) == lab]) for _, _, lab in BUCKETS},
               "by_check": {m: block([r for r in rows if r["check_min"] == m and r["gap"] >= 15]) for m in CHECKS},
               "open": self.db.query("SELECT COUNT(*) n FROM gw_picks WHERE status='open'")[0]["n"]}
        fv = self.db.query("SELECT * FROM gw_fv_picks WHERE status='settled'")
        ffirst = {}
        for r in sorted(fv, key=lambda r: -r["check_min"]):
            ffirst.setdefault((r["ticker"], r["side"]), r)
        out["fv_rule_first_touch"] = block(list(ffirst.values()))
        out["fv_by_check"] = {m: block([r for r in fv if r["check_min"] == m]) for m in FV_CHECKS}
        out["fv_open"] = self.db.query("SELECT COUNT(*) n FROM gw_fv_picks WHERE status='open'")[0]["n"]
        return out

    def status(self) -> dict:
        return {"enabled": self.enabled, "series": self.series(), "checks": list(CHECKS), "min_gap": float(self.opt("min_gap", 10)), "state": self.state,
                "summary": self.summary(),
                "recent": self.db.query("SELECT * FROM gw_picks ORDER BY id DESC LIMIT 40"),
                "fv_recent": self.db.query("SELECT * FROM gw_fv_picks ORDER BY id DESC LIMIT 40"),
                "checks_log": self.db.query("SELECT * FROM gw_checks ORDER BY id DESC LIMIT 24")}
