"""Sports fair-value scanner (paper only): sportsbook consensus vs Kalshi game markets, before the game starts.

The idea carried over from the FanDuel Predicts work: sportsbook lines are set by the sharpest money, so when a
prediction market is cheaper than the book's no-margin price on the same side, the prediction market is usually the
one that is wrong. This module
  * pulls moneylines from The Odds API (free plan: 500 credits a month; one sport = one credit per pull),
  * removes each bookmaker's margin and takes the median across books as the fair win probability,
  * matches each game to Kalshi's two game markets and compares,
  * records a PAPER pick whenever Kalshi's ask is below fair by more than the taker fee plus a margin,
  * follows every pick to the last Kalshi price before kick-off (closing-line value) and to settlement.
It reads public Kalshi data only and never places an order. Credits are counted before each call and it stops at the cap.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
import threading
import time

import requests

from .sports import _iso, ensure_schema, match_fixture

API = "https://api.the-odds-api.com/v4"
TAKER = 0.07
SCHEMA = """
CREATE TABLE IF NOT EXISTS fv_board (
    ticker TEXT PRIMARY KEY, sport TEXT, event_id TEXT, game TEXT, team TEXT, commence_ts REAL, fair REAL, books INTEGER,
    book_range REAL, bid REAL, ask REAL, edge_taker REAL, edge_rest REAL, ts REAL, how TEXT
);
CREATE TABLE IF NOT EXISTS fv_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sport TEXT, event_id TEXT, game TEXT, ticker TEXT, team TEXT,
    commence_ts REAL, fair REAL, books INTEGER, price REAL, fee REAL, edge REAL,
    last_mid REAL, last_mid_ts REAL, last_fair REAL, close_mid REAL, close_fair REAL, result TEXT, pnl REAL, status TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fv_picks_one ON fv_picks(ticker);
CREATE TABLE IF NOT EXISTS fv_calls (ts REAL, sport TEXT, status INTEGER, cost INTEGER, remaining TEXT, events INTEGER, note TEXT);
"""
DEFAULT_SPORTS = {"americanfootball_nfl": "KXNFLGAME", "basketball_nba": "KXNBAGAME",
                  "americanfootball_ncaaf": "KXNCAAFGAME", "icehockey_nhl": "KXNHLGAME"}


class BudgetExceeded(Exception):
    pass


def devig(outcomes: list[dict]) -> dict | None:
    """Decimal prices of a two-way market -> probabilities that sum to 1 (the bookmaker's margin removed)."""
    try:
        inv = {o["name"]: 1.0 / float(o["price"]) for o in outcomes if float(o["price"]) > 1.0}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if len(inv) != 2:
        return None
    tot = sum(inv.values())
    return {k: v / tot for k, v in inv.items()}


def consensus(event: dict) -> dict | None:
    """{team: fair probability} as the median across bookmakers, with how many books and how far apart they are."""
    home, away = event.get("home_team"), event.get("away_team")
    ps = []
    for b in event.get("bookmakers") or []:
        for m in b.get("markets") or []:
            if m.get("key") != "h2h":
                continue
            p = devig(m.get("outcomes") or [])
            if p and home in p and away in p:
                ps.append(p[home])
    if not ps:
        return None
    ph = statistics.median(ps)
    return {"home": home, "away": away, "p_home": ph, "p_away": 1 - ph, "books": len(ps), "range": max(ps) - min(ps)}


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class FairValue:
    def __init__(self, cfg_getter, wdb, kalshi_client, session=None, base=None):
        self._cfg = cfg_getter              # -> the "sports" config section, read fresh every time
        self.db = wdb
        self.k = kalshi_client              # public (unauthenticated) production client
        ensure_schema(wdb, SCHEMA)
        self.http = session or requests.Session()
        self._base = base
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.state = {"running": False, "last_poll": wdb.get("fv_last_poll"), "error": None, "note": ""}

    # ---------------------------------------------------------------- settings --
    def opt(self, k, default):
        v = self._cfg().get(k)
        return default if v in (None, "") else v

    @property
    def configured(self) -> bool:
        return bool((self._cfg().get("theodds_key") or "").strip())

    def sports(self) -> dict:
        s = self._cfg().get("fv_sports")
        return s if isinstance(s, dict) and s else DEFAULT_SPORTS

    # ------------------------------------------------------------------ budget --
    @staticmethod
    def month(now=None) -> str:
        return dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc).strftime("%Y-%m")

    def usage(self) -> dict:
        u = self.db.get("fv_usage") or {}
        m = self.month()
        used = u.get("count", 0) if u.get("month") == m else 0
        cap = int(self.opt("fv_monthly_cap", 450))
        return {"month": m, "used": used, "cap": cap, "left": max(0, cap - used), "plan_limit": 500,
                "provider_remaining": u.get("remaining") if u.get("month") == m else None}

    def _spend(self, n=1):
        with self.lock:
            u = self.usage()
            if u["used"] + n > u["cap"]:
                raise BudgetExceeded(f"The Odds API monthly budget reached ({u['used']}/{u['cap']} credits used in {u['month']})")
            self.db.put("fv_usage", {"month": u["month"], "count": u["used"] + n, "remaining": u.get("provider_remaining")})

    def fetch(self, sport: str) -> list[dict]:
        key = (self._cfg().get("theodds_key") or "").strip()
        if not key:
            raise RuntimeError("No key for The Odds API yet - add it in Settings")
        self._spend(1)                          # one market x one region = one credit, counted BEFORE the call
        now = time.time()
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"apiKey": key, "regions": "us", "markets": "h2h", "oddsFormat": "decimal",
                  "commenceTimeFrom": iso(now), "commenceTimeTo": iso(now + float(self.opt("fv_hours_ahead", 48)) * 3600)}
        try:
            r = self.http.get(f"{self._base or self._cfg().get('theodds_base') or API}/sports/{sport}/odds", params=params, timeout=20)
        except requests.RequestException as e:
            self.db.execute("INSERT INTO fv_calls VALUES(?,?,?,?,?,?,?)", (now, sport, 0, 1, None, 0, str(e).replace(key, "***")[:200]))
            raise RuntimeError(str(e).replace(key, "***"))
        rem = r.headers.get("x-requests-remaining")
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        n = len(body) if isinstance(body, list) else 0
        note = "" if r.status_code == 200 else str(body if body is not None else r.text)[:200].replace(key, "***")
        self.db.execute("INSERT INTO fv_calls VALUES(?,?,?,?,?,?,?)", (now, sport, r.status_code, 1, rem, n, note))
        if rem is not None:
            u = self.db.get("fv_usage") or {}
            self.db.put("fv_usage", {**u, "remaining": rem})
        if r.status_code != 200 or not isinstance(body, list):
            raise RuntimeError(f"The Odds API {r.status_code}: {note}")
        return body

    # ------------------------------------------------------------------- scan --
    def kalshi_events(self, series: str) -> dict:
        ev = {}
        for m in self.k.markets(status="open", series_ticker=series, max_items=2000):
            ev.setdefault(m.get("event_ticker"), []).append(m)
        return ev

    def scan_sport(self, sport: str, series: str, events: list[dict] | None = None) -> dict:
        events = self.fetch(sport) if events is None else events
        kev = self.kalshi_events(series)
        now = time.time()
        min_edge = float(self.opt("fv_min_edge", 0.02))
        min_books = int(self.opt("fv_min_books", 3))
        max_range = float(self.opt("fv_max_book_range", 0.05))
        max_spread = float(self.opt("fv_max_spread", 0.06))
        lead = float(self.opt("fv_min_lead_min", 10)) * 60
        out = {"sport": sport, "games": len(events), "kalshi_events": len(kev), "matched": 0, "picks": 0}
        for e in events:
            c = consensus(e)
            start = _iso(e.get("commence_time"))
            if not c or not start:
                continue
            mt = match_fixture({"start_ts": start, "p1": c["home"], "p2": c["away"]}, kev)
            if not mt:
                continue
            out["matched"] += 1
            ev_t, t_home, t_away, how = mt
            byt = {m["ticker"]: m for m in kev[ev_t]}
            game = f"{c['away']} at {c['home']}"
            for tk, team, p in ((t_home, c["home"], c["p_home"]), (t_away, c["away"], c["p_away"])):
                m = byt[tk]
                bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
                two_sided = bool(bid and ask and 0 < bid < ask < 1)
                e_take = (p - ask - TAKER * ask * (1 - ask)) if two_sided else None
                e_rest = (p - (bid + 0.01)) if two_sided else None           # a resting bid 1c over the best bid, no fee
                self.db.execute("INSERT OR REPLACE INTO fv_board VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (tk, sport, e.get("id"), game, team, start, p, c["books"], c["range"], bid, ask, e_take, e_rest, now, how))
                self.db.execute("UPDATE fv_picks SET last_fair=? WHERE ticker=? AND status='open'", (p, tk))
                ok = (two_sided and e_take is not None and e_take >= min_edge - 1e-9 and c["books"] >= min_books and c["range"] <= max_range + 1e-9
                      and ask - bid <= max_spread + 1e-9 and start - now >= lead)
                if ok and not self.db.query("SELECT 1 FROM fv_picks WHERE ticker=?", (tk,)):
                    fee = TAKER * ask * (1 - ask)
                    self.db.execute("INSERT INTO fv_picks(ts, sport, event_id, game, ticker, team, commence_ts, fair, books, price, fee, edge, "
                                    "last_mid, last_mid_ts, last_fair, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                                    (now, sport, e.get("id"), game, tk, team, start, p, c["books"], ask, fee, e_take, (bid + ask) / 2, now, p))
                    out["picks"] += 1
        return out

    def poll(self, only: str | None = None) -> dict:
        """Pull every configured sport once (one credit each) and compare with Kalshi."""
        res, err = [], None
        for sport, series in self.sports().items():
            if only and sport != only:
                continue
            try:
                res.append(self.scan_sport(sport, series))
            except BudgetExceeded as e:
                err = str(e)
                break
            except Exception as e:
                err = f"{sport}: {e}"[:300]
                res.append({"sport": sport, "error": str(e)[:200]})
        self.db.execute("DELETE FROM fv_board WHERE commence_ts < ?", (time.time() - 6 * 3600,))
        self.state.update(last_poll=time.time(), error=err, note="; ".join(
            f"{r['sport'].split('_')[-1].upper()}: {r.get('matched', 0)}/{r.get('games', 0)} games matched" + (f", {r['picks']} new pick(s)" if r.get("picks") else "")
            if "error" not in r else f"{r['sport'].split('_')[-1].upper()}: failed" for r in res))
        self.db.put("fv_last_poll", self.state["last_poll"])
        return {"results": res, "error": err, "usage": self.usage()}

    # ------------------------------------------------------- follow the picks --
    def upkeep(self) -> dict:
        """Free (Kalshi public data only): keep each open pick's last pre-game price, freeze it at kick-off as the
        closing line, and settle the pick when the market has a result."""
        now = time.time()
        n = {"tracked": 0, "closed": 0, "settled": 0}
        for p in self.db.query("SELECT * FROM fv_picks WHERE status IN ('open', 'closed')"):
            try:
                m = self.k.market(p["ticker"]) or {}
                m = m.get("market", m)
            except Exception:
                continue
            if p["status"] == "open":
                if now < p["commence_ts"]:
                    bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
                    if bid and ask and 0 < bid < ask < 1:
                        self.db.execute("UPDATE fv_picks SET last_mid=?, last_mid_ts=? WHERE id=?", ((bid + ask) / 2, now, p["id"]))
                        n["tracked"] += 1
                else:
                    self.db.execute("UPDATE fv_picks SET status='closed', close_mid=last_mid, close_fair=last_fair WHERE id=?", (p["id"],))
                    n["closed"] += 1
            res = (m.get("result") or "").lower()
            if res in ("yes", "no") and now >= p["commence_ts"]:
                pnl = (1.0 if res == "yes" else 0.0) - p["price"] - p["fee"]
                self.db.execute("UPDATE fv_picks SET status='settled', result=?, pnl=?, close_mid=COALESCE(close_mid, last_mid), "
                                "close_fair=COALESCE(close_fair, last_fair) WHERE id=?", (res, pnl, p["id"]))
                n["settled"] += 1
        return n

    # ------------------------------------------------------------------ report --
    def summary(self) -> dict:
        rows = self.db.query("SELECT * FROM fv_picks ORDER BY ts DESC")
        done = [r for r in rows if r["close_mid"] is not None]
        clv = [r["close_mid"] - r["price"] for r in done]
        stl = [r for r in rows if r["status"] == "settled"]

        def stat(xs):
            if not xs:
                return {"n": 0, "mean_c": None, "t": None}
            mu = sum(xs) / len(xs)
            sd = statistics.stdev(xs) if len(xs) > 1 else None
            return {"n": len(xs), "mean_c": round(100 * mu, 2), "t": round(mu / (sd / math.sqrt(len(xs))), 2) if sd else None}
        return {"picks": len(rows), "open": sum(1 for r in rows if r["status"] == "open"),
                "clv": {**stat(clv), "beat_close_pct": round(100 * sum(1 for x in clv if x > 0) / len(clv), 1) if clv else None},
                "pnl": {**stat([r["pnl"] for r in stl]), "wins": sum(1 for r in stl if r["pnl"] > 0), "total": round(sum(r["pnl"] for r in stl), 2) if stl else 0.0,
                        "staked": round(sum(r["price"] for r in stl), 2)},
                "needed": "at least 100 picks with closing prices before this means anything"}

    def status(self) -> dict:
        board = self.db.query("SELECT * FROM fv_board WHERE commence_ts > ? ORDER BY COALESCE(edge_taker, -9) DESC LIMIT 200", (time.time(),))
        return {"configured": self.configured, "enabled": bool(self.opt("fv_enabled", True)), "usage": self.usage(), "state": self.state,
                "sports": self.sports(), "poll_hours": float(self.opt("fv_poll_hours", 6)), "min_edge_c": round(100 * float(self.opt("fv_min_edge", 0.02)), 1),
                "board": board, "picks": self.db.query("SELECT * FROM fv_picks ORDER BY ts DESC LIMIT 200"), "summary": self.summary(),
                "calls": self.db.query("SELECT * FROM fv_calls ORDER BY ts DESC LIMIT 30")}

    # -------------------------------------------------------------------- loop --
    def due(self) -> bool:
        if not self.configured or not self.opt("fv_enabled", True):
            return False
        last = self.state.get("last_poll") or 0
        return time.time() - last >= float(self.opt("fv_poll_hours", 6)) * 3600

    def loop(self):
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.state["running"] = True
                if self.due():
                    self.poll()
                self.upkeep()
            except Exception as e:
                self.state["error"] = str(e)[:300]
            finally:
                self.state["running"] = False
            self._stop.wait(300)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="fairvalue").start()

    def stop(self):
        self._stop.set()
