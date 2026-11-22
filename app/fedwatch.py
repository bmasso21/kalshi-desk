"""Fed decisions: Kalshi's odds for each FOMC meeting against the odds implied by 30-day fed funds futures
(the CME FedWatch method). Read-only public data; paper picks only.

Inputs (free): fed funds futures settlement-month prices (Yahoo Finance, ~10 min delayed: fine for a decision weeks
away), the latest effective fed funds rate (New York Fed), Kalshi's KXFEDDECISION events.

Method, meeting by meeting in date order:
  a month's futures price P says the AVERAGE effective rate over that month is 100 - P.
  If a meeting falls on day d of an N-day month, the rate is r0 for the first d days and r1 after:
      avg = (d * r0 + (N - d) * r1) / N   ->   r1 = (N * avg - d * r0) / (N - d)
  (when fewer than 7 days follow the meeting, next month's average is used as r1 instead: too few days to read).
  The expected move r1 - r0 is split between the two neighbouring 25 bp steps (e.g. +0.27 -> 92% "+25", 8% "+50").
  r1 becomes r0 for the next meeting. The first r0 is the latest effective rate.
Kalshi's outcomes are the move AT that meeting: cut >25, cut 25, hold, hike 25, hike >25."""
from __future__ import annotations

import calendar
import datetime as dt
import math
import re
import threading
import time

from .sports import ensure_schema

TAKER = 0.07
MONTH_CODE = "FGHJKMNQUVXZ"
OUTCOME = {"C26": -2, "C25": -1, "H0": 0, "H25": 1, "H26": 2}     # Kalshi ticker suffix -> steps of 25 bp
MIN_EDGE = 0.03
MEETINGS_AHEAD = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS fed_board (ticker TEXT PRIMARY KEY, event TEXT, meeting TEXT, outcome TEXT, fair REAL, yes_bid REAL,
    yes_ask REAL, yes_edge REAL, no_edge REAL, ts REAL);
CREATE TABLE IF NOT EXISTS fed_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, event TEXT, meeting TEXT,
    outcome TEXT, side TEXT, fair REAL, price REAL, fee REAL, edge REAL, status TEXT DEFAULT 'open', result TEXT, pnl REAL,
    settled_ts REAL, UNIQUE(ticker, side));
"""


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def futures_symbol(year: int, month: int) -> str:
    return f"ZQ{MONTH_CODE[month - 1]}{year % 100:02d}.CBT"


def split_steps(delta: float) -> dict:
    """Expected move (percentage points) -> {steps: probability} over the two neighbouring 25 bp steps."""
    k = delta / 0.25
    lo = math.floor(k)
    f = k - lo
    out = {lo: 1 - f}
    if f > 1e-9:
        out[lo + 1] = f
    return out


def to_outcomes(steps: dict) -> dict:
    """{steps: p} -> Kalshi outcome probabilities (beyond +-1 step lands in '>25')."""
    o = {k: 0.0 for k in OUTCOME}
    for s, p in steps.items():
        key = "C26" if s <= -2 else "C25" if s == -1 else "H0" if s == 0 else "H25" if s == 1 else "H26"
        o[key] += p
    return o


def implied(meetings: list[dt.date], avg_rate: dict, r_start: float) -> list[dict]:
    """meetings: decision dates in order. avg_rate: {(year, month): average rate implied by that month's futures}.
    -> [{date, r0, r1, delta, probs}] (stops at the first meeting whose month has no futures price)."""
    out, r0 = [], r_start
    for d in sorted(meetings):
        n = calendar.monthrange(d.year, d.month)[1]
        after = n - d.day
        ym = (d.year, d.month)
        nxt = (d.year + (d.month == 12), d.month % 12 + 1)
        if after >= 7 and ym in avg_rate:
            r1 = (n * avg_rate[ym] - d.day * r0) / after
        elif nxt in avg_rate:
            r1 = avg_rate[nxt]
        else:
            break
        delta = r1 - r0
        out.append({"date": d.isoformat(), "r0": round(r0, 4), "r1": round(r1, 4), "delta_bp": round(100 * delta, 1),
                    "probs": to_outcomes(split_steps(delta))})
        r0 = r1
    return out


def meeting_date(ev: dict) -> dt.date | None:
    ms = ev.get("markets") or []
    for m in ms:
        c = m.get("close_time")
        if c:
            try:
                return dt.datetime.fromisoformat(c.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=-5))).date()
            except ValueError:
                pass
    return None


class FedWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client, session=None):
        import requests
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        self.http = session or requests.Session()
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self.state = {"running": False, "last": None, "error": None, "note": "", "effr": None, "meetings": []}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    # ---------------------------------------------------------------- data --
    def _get(self, url, **kw):
        """GET with one retry on a dropped connection. The session keeps connections open between the half-hourly
        checks; the far end closes an idle one, and the first request on it fails ('connection reset', 10054)."""
        import requests
        try:
            return self.http.get(url, **kw)
        except (requests.ConnectionError, ConnectionError):
            return self.http.get(url, **kw)

    def effr(self) -> float:
        r = self._get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json", timeout=20)
        r.raise_for_status()
        return float(r.json()["refRates"][0]["percentRate"])

    def futures(self, months: list[tuple]) -> dict:
        out = {}
        for y, m in months:
            r = self._get(f"https://query1.finance.yahoo.com/v8/finance/chart/{futures_symbol(y, m)}",
                              params={"interval": "1d", "range": "5d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code != 200:
                continue
            res = (r.json().get("chart") or {}).get("result") or []
            p = fnum(res[0]["meta"].get("regularMarketPrice")) if res else None
            if p:
                out[(y, m)] = round(100 - p, 4)
        return out

    # ---------------------------------------------------------------- scan --
    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        evs = [e for e in self.k.paginate("/events", "events", {"series_ticker": "KXFEDDECISION", "status": "open", "with_nested_markets": "true", "limit": 50},
                                          max_items=50)]
        evs = sorted((e for e in evs if meeting_date(e)), key=meeting_date)
        dates = [meeting_date(e) for e in evs]
        months = sorted({(d.year, d.month) for d in dates} | {(d.year + (d.month == 12), d.month % 12 + 1) for d in dates})
        r0 = self.effr()
        avg = self.futures(months)
        model = {x["date"]: x for x in implied(dates, avg, r0)}
        picks = 0
        rows = []
        for i, e in enumerate(evs):
            d = meeting_date(e).isoformat()
            if d not in model:
                continue
            probs = model[d]["probs"]
            for m in e.get("markets") or []:
                key = m["ticker"].rsplit("-", 1)[-1]
                if key not in OUTCOME:
                    continue
                p = probs[key]
                yb, ya = fnum(m.get("yes_bid_dollars")) or 0.0, fnum(m.get("yes_ask_dollars"))
                ye = (p - ya - TAKER * ya * (1 - ya)) if ya and 0 < ya < 1 else None
                na = round(1 - yb, 4) if yb > 0 else None
                ne = ((1 - p) - na - TAKER * na * (1 - na)) if na and 0 < na < 1 else None
                self.db.execute("INSERT OR REPLACE INTO fed_board VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (m["ticker"], e.get("event_ticker"), d, key, round(p, 4), yb, ya, ye, ne, now))
                rows.append((m["ticker"], key, p, ya, ye, ne))
                if i >= int(self.opt("fed_meetings_ahead", MEETINGS_AHEAD)):
                    continue
                for side, edge, price, fair in (("yes", ye, ya, p), ("no", ne, na, 1 - p)):
                    if edge is not None and edge >= float(self.opt("fed_min_edge", MIN_EDGE)) and 0.03 <= price <= 0.97:
                        cur = self.db.execute("INSERT OR IGNORE INTO fed_picks(ts, ticker, event, meeting, outcome, side, fair, price, fee, edge) "
                                              "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                              (now, m["ticker"], e.get("event_ticker"), d, key, side, round(fair, 4), price,
                                               round(TAKER * price * (1 - price), 6), round(edge, 4)))
                        picks += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        self.settle(now)
        self.state.update(last=now, error=None, effr=r0, meetings=list(model.values())[:6],
                          note=f"{len(model)} meetings priced from futures (effective rate {r0:.2f}%), {picks} new paper pick(s)")
        return {"meetings": len(model), "picks": picks, "rows": len(rows)}

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT id, ticker, side, price, fee FROM fed_picks WHERE status='open'")
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower()
               for m in self.k.markets(status=None, tickers=list({r["ticker"] for r in rows}), max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = (1.0 if r["side"] == x else 0.0) - r["price"] - r["fee"]
                self.db.execute("UPDATE fed_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(120)
        while not self._stop.is_set():
            if self.opt("fed_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("fed_every_min", 30)) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="fedwatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {"state": self.state, "min_edge_c": 100 * float(self.opt("fed_min_edge", MIN_EDGE)),
                "board": self.db.query("SELECT * FROM fed_board ORDER BY meeting, outcome"),
                "picks": self.db.query("SELECT * FROM fed_picks ORDER BY ts DESC LIMIT 100")}
