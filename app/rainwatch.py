"""Observed rain, paper test: once a city's official station has reported measurable rain today, is YES still cheap?

A daily rain market (KXRAIN) pays YES if the day's official precipitation is above 0 (trace = 0). Backtest
(studies.rainlock, 25 Nov 2026): of 373 market-days where the station's hourly readings showed 0.01 in or more after the
first hour of the day, the official total was above 0 in all 373. Where YES was bought after such a reading it won
every time, at an average 94c - but in only 4 separate days with stored trades, so the price side is unproven.

Every 10 minutes: ONE request to the Iowa Environmental Mesonet for the hourly/special METAR rain of every station the
rain markets name; for each city whose reading today (after the first hour of the climate day) is 0.01 in or more, the
day's Kalshi rain market is read; if YES is offered at 97c or less, 100 contracts are bought on paper at the real ask
(fee rounded up to the cent on the order) and held to Kalshi's result. Cities where YES was already 98c+ are counted too.
Read-only public data. Places no orders."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

from .finalwatch import order_fee
from .sports import ensure_schema
from .studies import RAIN_RULE, RAIN_STATIONS, iem_precip_multi
from .weather import day_window

SERIES = "KXRAIN"
MIN_RAIN = 0.01
MAX_PRICE = 0.97
SIZE = 100
EVERY = 600
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rain_days (ticker TEXT PRIMARY KEY, cli TEXT, day TEXT, first_rain_ts REAL, rain_in REAL, seen_ts REAL,
    yes_ask REAL, offered REAL, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS rain_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT UNIQUE, event TEXT, cli TEXT, day TEXT,
    first_rain_ts REAL, price REAL, size REAL, offered REAL, fee REAL, close_ts REAL, status TEXT DEFAULT 'open', result TEXT, pnl REAL,
    settled_ts REAL);
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


def event_for(d: dt.date) -> str:
    return f"{SERIES}-{d.year % 100:02d}{MON[d.month - 1]}{d.day:02d}"


class RainWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client, fetch=None):
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        self.fetch = fetch or (lambda aids, a, b: iem_precip_multi(aids, a, b))
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self.state = {"running": False, "last": None, "error": None, "note": ""}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    def markets_by_cli(self, days: set) -> dict:
        """{(cli, day): market} for the rain events of the given local days, from each market's own rules."""
        out = {}
        for d in days:
            for m in self.k.markets(status=None, event_ticker=event_for(d), max_items=200):
                mm = RAIN_RULE.search(m.get("rules_primary") or "")
                if mm and (m.get("strike_type") or "greater") == "greater" and (fnum(m.get("floor_strike")) or 0) == 0:
                    out[(mm.group(1).upper(), d)] = m
        return out

    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        aids = sorted({a for a, _ in RAIN_STATIONS.values()})
        obs = self.fetch(aids, now - 30 * 3600, now + 86400)
        rainy = {}                                            # (cli, day) -> (first ts, inches)
        for cli, (aid, off) in RAIN_STATIONS.items():
            d = (dt.datetime.fromtimestamp(now, dt.timezone.utc) + dt.timedelta(hours=off)).date()      # local standard-time day
            ds, de = day_window(d, off)
            for ts, x in obs.get(aid, []):
                if ds + 3600 <= ts < de and x >= MIN_RAIN - 1e-9:
                    rainy[(cli, d)] = (ts, x)
                    break
        new = picks = 0
        if rainy:
            mk = self.markets_by_cli({d for _, d in rainy})
            for (cli, d), (fts, inch) in rainy.items():
                m = mk.get((cli, d))
                if not m:
                    continue
                t = m["ticker"]
                if self.db.query("SELECT 1 FROM rain_days WHERE ticker=? AND status IN ('bought','already_priced','closed')", (t,)):
                    continue
                ya, sz = fnum(m.get("yes_ask_dollars")), fnum(m.get("yes_ask_size_fp"))
                if (m.get("status") or "") not in ("active", "open"):
                    status, note = "closed", f"Kalshi status {m.get('status')}"
                elif ya is None or ya <= 0 or ya >= 1:
                    status, note = "no_ask", "no YES offered"
                elif ya > MAX_PRICE + 1e-9:
                    status, note = "already_priced", f"YES ask {ya}"
                else:
                    status, note = "bought", f"YES at {ya}, {sz} offered"
                    if self.opt("rain_enabled", True):
                        size = SIZE if (sz or 0) >= SIZE else max(1.0, float(int(sz or 0)))
                        cur = self.db.execute("INSERT OR IGNORE INTO rain_picks(ts, ticker, event, cli, day, first_rain_ts, price, size, offered, fee, close_ts) "
                                              "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (now, t, m.get("event_ticker"), cli, d.isoformat(), fts, ya, size, sz,
                                                                                order_fee(ya, size), _ts(m.get("close_time"))))
                        picks += 1 if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
                cur = self.db.execute("INSERT OR REPLACE INTO rain_days VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (t, cli, d.isoformat(), fts, inch, now, ya, sz, status, note))
                new += 1
        self.settle(now)
        self.state.update(last=now, error=None, note=f"{len(rainy)} cities with measurable rain today, {picks} new paper buy(s)")
        return {"rainy": len(rainy), "picks": picks}

    def settle(self, now: float) -> int:
        rows = self.db.query("SELECT id, ticker, price, size, fee FROM rain_picks WHERE status='open' AND (close_ts IS NULL OR close_ts < ?)", (now,))
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower() for m in self.k.markets(status=None, tickers=[r["ticker"] for r in rows][:200], max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = r["size"] * ((1.0 if x == "yes" else 0.0) - r["price"]) - r["fee"]
                self.db.execute("UPDATE rain_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(540)
        while not self._stop.is_set():
            if self.opt("rain_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(EVERY)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="rainwatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        rs = self.db.query("SELECT pnl, price, size, fee FROM rain_picks WHERE status='settled'")
        cost = sum(r["price"] * r["size"] + r["fee"] for r in rs)
        days = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM rain_days GROUP BY status")}
        return {"state": self.state, "rule": {"min_rain_in": MIN_RAIN, "max_price_c": int(MAX_PRICE * 100), "size": SIZE},
                "days": days,
                "totals": {"settled": len(rs), "won": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(sum(r["pnl"] for r in rs), 2),
                           "return_pct": round(100 * sum(r["pnl"] for r in rs) / cost, 2) if cost else None,
                           "open": self.db.query("SELECT COUNT(*) n FROM rain_picks WHERE status='open'")[0]["n"]},
                "recent_days": self.db.query("SELECT * FROM rain_days ORDER BY seen_ts DESC LIMIT 60"),
                "picks": self.db.query("SELECT * FROM rain_picks ORDER BY ts DESC LIMIT 100")}
