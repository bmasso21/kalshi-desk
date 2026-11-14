"""Sports fair-value scanner (paper only): sportsbook consensus vs Kalshi game markets, before the game starts.

The method is the one used on the NFL and college football boards: take the sportsbooks' prices, remove their
margin, and look at every option on the board (winner, spread, total) for the ones priced at or better than fair.
Sportsbook lines are set by the sharpest money, so when a prediction market is cheaper than the book's no-margin
price on the same side, the prediction market is usually the one that is wrong. This module
  * pulls odds from The Odds API (free plan: 500 credits a month; one credit per market type per sport per pull),
  * removes each bookmaker's margin and takes the median across books as the fair probability,
  * matches each game to Kalshi's winner, spread and total markets. Spreads and totals are compared ONLY at the
    exact line the books quote (no modelling of other lines), on both sides (Kalshi's NO is the other side),
  * records PAPER picks in two tiers: "edge" (Kalshi beats fair by the margin after the taker fee) and "lean"
    (at fair or slightly better after the fee: how most NFL / college plays were actually chosen),
  * follows every pick to the last Kalshi price before kick-off (closing-line value) and to settlement.
It reads public Kalshi data only and never places an order. Credits are counted before each call and it stops at the cap.
"""
from __future__ import annotations

import collections
import datetime as dt
import math
import statistics
import threading
import time

import requests

from .sports import _iso, ensure_schema, match_fixture, name_match

API = "https://api.the-odds-api.com/v4"
TAKER = 0.07
SCHEMA = """
CREATE TABLE IF NOT EXISTS fv_board2 (
    ticker TEXT, side TEXT, sport TEXT, event_id TEXT, game TEXT, market TEXT, label TEXT, line REAL, commence_ts REAL,
    fair REAL, prev_fair REAL, books INTEGER, book_range REAL, bid REAL, ask REAL, edge_taker REAL, edge_rest REAL, ts REAL,
    PRIMARY KEY (ticker, side)
);
CREATE TABLE IF NOT EXISTS fv_picks2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tier TEXT, sport TEXT, event_id TEXT, game TEXT, market TEXT, label TEXT, line REAL,
    ticker TEXT, side TEXT, commence_ts REAL, fair REAL, books INTEGER, price REAL, fee REAL, edge REAL,
    last_mid REAL, last_mid_ts REAL, last_fair REAL, close_mid REAL, close_fair REAL, result TEXT, pnl REAL, status TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fv_picks2_one ON fv_picks2(ticker, side);
CREATE TABLE IF NOT EXISTS fv_calls (ts REAL, sport TEXT, status INTEGER, cost INTEGER, remaining TEXT, events INTEGER, note TEXT);
"""
# sport -> Kalshi series of the winner market, the market types to pull (one credit each) and hours between pulls
DEFAULT_SPORTS = {
    "americanfootball_nfl": {"series": "KXNFLGAME", "markets": "h2h,spreads,totals", "hours": 12},
    "americanfootball_ncaaf": {"series": "KXNCAAFGAME", "markets": "h2h,spreads,totals", "hours": 12},
    "basketball_nba": {"series": "KXNBAGAME", "markets": "h2h", "hours": 24},
    "icehockey_nhl": {"series": "KXNHLGAME", "markets": "h2h", "hours": 24},
}


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


def _med(ps):
    return {"p": statistics.median(ps), "books": len(ps), "range": max(ps) - min(ps)}


def consensus(event: dict) -> dict | None:
    """Winner market: fair probability of each team as the median across bookmakers."""
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


def spread_lines(event: dict) -> dict:
    """{(team, points the team must win by): {p, books, range}} for every line at least one book quotes, e.g.
    ("Green Bay Packers", 3.5) = Green Bay -3.5. Each book counts only at the exact line it quotes."""
    acc = collections.defaultdict(list)
    for b in event.get("bookmakers") or []:
        for m in b.get("markets") or []:
            if m.get("key") != "spreads":
                continue
            oc = m.get("outcomes") or []
            p = devig(oc)
            if not p or len(oc) != 2:
                continue
            try:
                pts = {o["name"]: float(o["point"]) for o in oc}
            except (KeyError, TypeError, ValueError):
                continue
            a, b2 = list(pts)
            if abs(pts[a] + pts[b2]) > 1e-9:
                continue
            for team in (a, b2):
                if pts[team] < 0:                                   # the side laying points: "wins by over |point|"
                    acc[(team, -pts[team])].append(p[team])
    return {k: _med(v) for k, v in acc.items()}


def total_lines(event: dict) -> dict:
    """{line: {p (of the Over), books, range}} for every total at least one book quotes."""
    acc = collections.defaultdict(list)
    for b in event.get("bookmakers") or []:
        for m in b.get("markets") or []:
            if m.get("key") != "totals":
                continue
            oc = m.get("outcomes") or []
            p = devig(oc)
            if not p or "Over" not in p or "Under" not in p:
                continue
            try:
                pt = {float(o["point"]) for o in oc}
            except (KeyError, TypeError, ValueError):
                continue
            if len(pt) == 1:
                acc[pt.pop()].append(p["Over"])
    return {k: _med(v) for k, v in acc.items()}


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
        self.state = {"running": False, "last_poll": wdb.get("fv_last_poll"), "error": None, "note": wdb.get("fv_note") or ""}
        wdb.execute("DELETE FROM fv_picks2 WHERE tier='lean' AND price < ?", (float(self.opt("fv_lean_min_price", 0.30)) - 1e-9,))   # recorded before that rule

    # ---------------------------------------------------------------- settings --
    def opt(self, k, default):
        v = self._cfg().get(k)
        return default if v in (None, "") else v

    @property
    def configured(self) -> bool:
        return bool((self._cfg().get("theodds_key") or "").strip())

    def sports(self) -> dict:
        s = self._cfg().get("fv_sports")
        out = {}
        for k, v in (s if isinstance(s, dict) and s else DEFAULT_SPORTS).items():
            d = DEFAULT_SPORTS.get(k, {"markets": "h2h", "hours": 24})
            v = {"series": v} if isinstance(v, str) else dict(v or {})
            if v.get("series") or d.get("series"):
                out[k] = {"series": v.get("series") or d["series"], "markets": v.get("markets") or d["markets"], "hours": float(v.get("hours") or d["hours"])}
        return out

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

    def fetch(self, sport: str, markets: str = "h2h") -> list[dict]:
        key = (self._cfg().get("theodds_key") or "").strip()
        if not key:
            raise RuntimeError("No key for The Odds API yet - add it in Settings")
        cost = len([m for m in markets.split(",") if m.strip()])
        self._spend(cost)                       # markets x one region, counted BEFORE the call
        now = time.time()
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"apiKey": key, "regions": "us", "markets": markets, "oddsFormat": "decimal",
                  "commenceTimeFrom": iso(now), "commenceTimeTo": iso(now + float(self.opt("fv_hours_ahead", 48)) * 3600)}
        try:
            r = self.http.get(f"{self._base or self._cfg().get('theodds_base') or API}/sports/{sport}/odds", params=params, timeout=20)
        except requests.RequestException as e:
            self.db.execute("INSERT INTO fv_calls VALUES(?,?,?,?,?,?,?)", (now, sport, 0, cost, None, 0, str(e).replace(key, "***")[:200]))
            raise RuntimeError(str(e).replace(key, "***"))
        rem = r.headers.get("x-requests-remaining")
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        n = len(body) if isinstance(body, list) else 0
        note = "" if r.status_code == 200 else str(body if body is not None else r.text)[:200].replace(key, "***")
        self.db.execute("INSERT INTO fv_calls VALUES(?,?,?,?,?,?,?)", (now, sport, r.status_code, cost, rem, n, note))
        if rem is not None:
            u = self.db.get("fv_usage") or {}
            self.db.put("fv_usage", {**u, "remaining": rem})
        if r.status_code != 200 or not isinstance(body, list):
            raise RuntimeError(f"The Odds API {r.status_code}: {note}")
        return body

    # ------------------------------------------------------------------- scan --
    def kalshi_events(self, series: str) -> dict:
        ev = {}
        for m in self.k.markets(status="open", series_ticker=series, max_items=5000):
            ev.setdefault(m.get("event_ticker"), []).append(m)
        return ev

    def _consider(self, out, now, sport, e, game, start, market, label, line, ticker, side, fair, books, rng, yes_bid, yes_ask):
        """One side of one Kalshi market against its fair price: put it on the board and record a paper pick if it qualifies."""
        two = bool(yes_bid and yes_ask and 0 < yes_bid < yes_ask < 1)
        bid, ask = (yes_bid, yes_ask) if side == "yes" else ((1 - yes_ask, 1 - yes_bid) if two else (None, None))
        e_take = (fair - ask - TAKER * ask * (1 - ask)) if two else None
        e_rest = (fair - (bid + 0.01)) if two else None                 # a resting bid 1c over the best bid, no fee
        old = self.db.query("SELECT fair, ts FROM fv_board2 WHERE ticker=? AND side=?", (ticker, side))
        prev = old[0]["fair"] if old else None
        self.db.execute("INSERT OR REPLACE INTO fv_board2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ticker, side, sport, e.get("id"), game, market, label, line, start, fair, prev, books, rng, bid, ask, e_take, e_rest, now))
        self.db.execute("UPDATE fv_picks2 SET last_fair=? WHERE ticker=? AND side=? AND status='open'", (fair, ticker, side))
        if not two or e_take is None:
            return
        ok = (books >= int(self.opt("fv_min_books", 3)) and rng <= float(self.opt("fv_max_book_range", 0.05)) + 1e-9
              and ask - bid <= float(self.opt("fv_max_spread", 0.06)) + 1e-9 and start - now >= float(self.opt("fv_min_lead_min", 10)) * 60
              and float(self.opt("fv_min_price", 0.10)) - 1e-9 <= ask <= float(self.opt("fv_max_price", 0.92)) + 1e-9)
        if not ok:
            return
        tier = "edge" if e_take >= float(self.opt("fv_min_edge", 0.02)) - 1e-9 else "lean" if e_take >= float(self.opt("fv_lean_edge", 0.0)) - 1e-9 else None
        if tier == "lean" and ask < float(self.opt("fv_lean_min_price", 0.30)) - 1e-9:
            tier = None     # removing a bookmaker's margin evenly overstates underdogs by about a cent: a "lean" on one is that error, not value
        if not tier or self.db.query("SELECT 1 FROM fv_picks2 WHERE ticker=? AND side=?", (ticker, side)):
            return
        fee = TAKER * ask * (1 - ask)
        self.db.execute("INSERT INTO fv_picks2(ts, tier, sport, event_id, game, market, label, line, ticker, side, commence_ts, fair, books, price, fee, edge, "
                        "last_mid, last_mid_ts, last_fair, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                        (now, tier, sport, e.get("id"), game, market, label, line, ticker, side, start, fair, books, ask, fee, e_take, (bid + ask) / 2, now, fair))
        out["picks" if tier == "edge" else "leans"] += 1

    def scan_sport(self, sport: str, spec: dict | str, events: list[dict] | None = None) -> dict:
        spec = {"series": spec, "markets": "h2h"} if isinstance(spec, str) else spec
        series, mk = spec["series"], [m.strip() for m in spec.get("markets", "h2h").split(",") if m.strip()]
        kev = self.kalshi_events(series)
        out = {"sport": sport, "games": 0, "kalshi_events": len(kev), "matched": 0, "spread_lines": 0, "total_lines": 0, "picks": 0, "leans": 0}
        if events is None:
            if not self._games_soon(kev):
                out["skipped"] = "no Kalshi game in the next two days (no credits spent)"
                return out
            events = self.fetch(sport, ",".join(mk))
        out["games"] = len(events)
        sp_ev = self.kalshi_events(series.replace("GAME", "SPREAD")) if "spreads" in mk and "GAME" in series else {}
        to_ev = self.kalshi_events(series.replace("GAME", "TOTAL")) if "totals" in mk and "GAME" in series else {}
        now = time.time()
        for e in events:
            c = consensus(e)
            start = _iso(e.get("commence_time"))
            if not c or not start:
                continue
            mt = match_fixture({"start_ts": start, "p1": c["home"], "p2": c["away"]}, kev)
            if not mt:
                continue
            out["matched"] += 1
            ev_t, t_home, t_away, _how = mt
            byt = {m["ticker"]: m for m in kev[ev_t]}
            game = f"{c['away']} at {c['home']}"
            for tk, team, p in ((t_home, c["home"], c["p_home"]), (t_away, c["away"], c["p_away"])):
                m = byt[tk]
                self._consider(out, now, sport, e, game, start, "winner", f"{team} to win", None, tk, "yes", p, c["books"], c["range"],
                               fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")))
            suffix = ev_t.split("-", 1)[1] if "-" in ev_t else None
            teams = (c["home"], c["away"])
            sl = spread_lines(e) if sp_ev else {}
            for m in sp_ev.get(f"{series.replace('GAME', 'SPREAD')}-{suffix}", []) if suffix else []:
                k = fnum(m.get("floor_strike"))
                nm = (m.get("yes_sub_title") or "").split(" wins by")[0].strip()
                team = next((t for t in teams if name_match(nm, t)), None)
                if k is None or not team or (team, k) not in sl:
                    continue
                f = sl[(team, k)]
                other = teams[1] if team == teams[0] else teams[0]
                yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
                out["spread_lines"] += 1
                self._consider(out, now, sport, e, game, start, "spread", f"{team} -{k:g}", k, m["ticker"], "yes", f["p"], f["books"], f["range"], yb, ya)
                self._consider(out, now, sport, e, game, start, "spread", f"{other} +{k:g}", k, m["ticker"], "no", 1 - f["p"], f["books"], f["range"], yb, ya)
            tl = total_lines(e) if to_ev else {}
            for m in to_ev.get(f"{series.replace('GAME', 'TOTAL')}-{suffix}", []) if suffix else []:
                k = fnum(m.get("floor_strike"))
                if k is None or k not in tl:
                    continue
                f = tl[k]
                yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
                out["total_lines"] += 1
                self._consider(out, now, sport, e, game, start, "total", f"Over {k:g}", k, m["ticker"], "yes", f["p"], f["books"], f["range"], yb, ya)
                self._consider(out, now, sport, e, game, start, "total", f"Under {k:g}", k, m["ticker"], "no", 1 - f["p"], f["books"], f["range"], yb, ya)
        return out

    @staticmethod
    def _games_soon(kev: dict) -> bool:
        """Is any Kalshi game of this series dated today, tomorrow or the day after (US Eastern)? Costs nothing."""
        from .sports import kalshi_event_parts
        today = dt.datetime.fromtimestamp(time.time() - 5 * 3600, dt.timezone.utc).date()
        for ev in kev:
            d, _ = kalshi_event_parts(ev)
            if d and -1 <= (d - today).days <= 2:
                return True
        return False

    def poll(self, only: str | None = None, force: bool = True) -> dict:
        """Pull the configured sports (one credit per market type each) and compare with Kalshi.
        force=False pulls only the sports whose own interval has passed."""
        res, err = [], None
        last = self.db.get("fv_last_by_sport") or {}
        for sport, spec in self.sports().items():
            if only and sport != only:
                continue
            if not force and time.time() - float(last.get(sport) or 0) < spec["hours"] * 3600:
                continue
            try:
                res.append(self.scan_sport(sport, spec))
                last[sport] = time.time()
            except BudgetExceeded as e:
                err = str(e)
                break
            except Exception as e:
                err = f"{sport}: {e}"[:300]
                res.append({"sport": sport, "error": str(e)[:200]})
                last[sport] = time.time()                              # do not hammer a failing call
        self.db.put("fv_last_by_sport", last)
        self.db.execute("DELETE FROM fv_board2 WHERE commence_ts < ?", (time.time() - 6 * 3600,))
        if res:
            nm = lambda r: r["sport"].split("_")[-1].upper()
            note = "; ".join(f"{nm(r)}: failed" if "error" in r else f"{nm(r)}: skipped, {r['skipped']}" if r.get("skipped") else
                             f"{nm(r)}: {r['matched']}/{r['games']} games matched" + (f", {r['spread_lines']} spread and {r['total_lines']} total lines" if r["spread_lines"] or r["total_lines"] else "")
                             + (f", {r['picks']} edge pick(s)" if r["picks"] else "") + (f", {r['leans']} lean(s)" if r["leans"] else "") for r in res)
            self.state.update(last_poll=time.time(), note=note)
            self.db.put("fv_last_poll", self.state["last_poll"])
            self.db.put("fv_note", note)
        self.state["error"] = err
        return {"results": res, "error": err, "usage": self.usage()}

    # ------------------------------------------------------- follow the picks --
    def upkeep(self) -> dict:
        """Free (Kalshi public data only): keep each open pick's last pre-game price, freeze it at kick-off as the
        closing line, and settle the pick when the market has a result."""
        now = time.time()
        n = {"tracked": 0, "closed": 0, "settled": 0}
        cache = {}
        for p in self.db.query("SELECT * FROM fv_picks2 WHERE status IN ('open', 'closed')"):
            try:
                if p["ticker"] not in cache:
                    m = self.k.market(p["ticker"]) or {}
                    cache[p["ticker"]] = m.get("market", m)
                m = cache[p["ticker"]]
            except Exception:
                continue
            yes = p["side"] == "yes"
            if p["status"] == "open":
                if now < p["commence_ts"]:
                    bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
                    if bid and ask and 0 < bid < ask < 1:
                        mid = (bid + ask) / 2
                        self.db.execute("UPDATE fv_picks2 SET last_mid=?, last_mid_ts=? WHERE id=?", (mid if yes else 1 - mid, now, p["id"]))
                        n["tracked"] += 1
                else:
                    self.db.execute("UPDATE fv_picks2 SET status='closed', close_mid=last_mid, close_fair=last_fair WHERE id=?", (p["id"],))
                    n["closed"] += 1
            res = (m.get("result") or "").lower()
            if res in ("yes", "no") and now >= p["commence_ts"]:
                pnl = (1.0 if res == p["side"] else 0.0) - p["price"] - p["fee"]
                self.db.execute("UPDATE fv_picks2 SET status='settled', result=?, pnl=?, close_mid=COALESCE(close_mid, last_mid), "
                                "close_fair=COALESCE(close_fair, last_fair) WHERE id=?", ("won" if res == p["side"] else "lost", pnl, p["id"]))
                n["settled"] += 1
        return n

    # ------------------------------------------------------------------ report --
    def summary(self) -> dict:
        rows = self.db.query("SELECT * FROM fv_picks2 ORDER BY ts DESC")

        def stat(xs):
            if not xs:
                return {"n": 0, "mean_c": None, "t": None}
            mu = sum(xs) / len(xs)
            sd = statistics.stdev(xs) if len(xs) > 1 else None
            return {"n": len(xs), "mean_c": round(100 * mu, 2), "t": round(mu / (sd / math.sqrt(len(xs))), 2) if sd else None}

        def block(rs):
            clv = [r["close_mid"] - r["price"] for r in rs if r["close_mid"] is not None]
            stl = [r for r in rs if r["status"] == "settled"]
            return {"picks": len(rs), "open": sum(1 for r in rs if r["status"] == "open"),
                    "clv": {**stat(clv), "beat_close_pct": round(100 * sum(1 for x in clv if x > 0) / len(clv), 1) if clv else None},
                    "pnl": {**stat([r["pnl"] for r in stl]), "wins": sum(1 for r in stl if r["pnl"] > 0),
                            "total": round(sum(r["pnl"] for r in stl), 2) if stl else 0.0, "staked": round(sum(r["price"] for r in stl), 2)}}
        out = block(rows)
        out["tiers"] = {t: block([r for r in rows if r["tier"] == t]) for t in ("edge", "lean")}
        out["markets"] = {mk: block([r for r in rows if r["market"] == mk]) for mk in ("winner", "spread", "total")}
        out["needed"] = "at least 100 picks with closing prices before this means anything"
        return out

    def status(self) -> dict:
        board = self.db.query("SELECT * FROM fv_board2 WHERE commence_ts > ? ORDER BY COALESCE(edge_taker, -9) DESC LIMIT 400", (time.time(),))
        return {"configured": self.configured, "enabled": bool(self.opt("fv_enabled", True)), "usage": self.usage(), "state": self.state,
                "sports": self.sports(), "min_edge_c": round(100 * float(self.opt("fv_min_edge", 0.02)), 1),
                "lean_edge_c": round(100 * float(self.opt("fv_lean_edge", 0.0)), 1),
                "board": board, "picks": self.db.query("SELECT * FROM fv_picks2 ORDER BY ts DESC LIMIT 300"), "summary": self.summary(),
                "calls": self.db.query("SELECT * FROM fv_calls ORDER BY ts DESC LIMIT 30")}

    # -------------------------------------------------------------------- loop --
    def due(self) -> bool:
        if not self.configured or not self.opt("fv_enabled", True):
            return False
        last = self.db.get("fv_last_by_sport") or {}
        return any(time.time() - float(last.get(s) or 0) >= spec["hours"] * 3600 for s, spec in self.sports().items())

    def loop(self):
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.state["running"] = True
                if self.due():
                    self.poll(force=False)
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
