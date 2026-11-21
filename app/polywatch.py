"""Kalshi against Polymarket, paper test: the same game trades on both; when Kalshi's asking price sits below
Polymarket's price, is Kalshi the one that is wrong?

Polymarket's prices are public and free to read as often as we like, so this also answers what the sportsbook
comparison (two checks a day on a 500-lookup plan) cannot: do gaps open on Kalshi for minutes at a time?

Every few minutes, for NBA, NFL and NHL games starting within a day:
  1. Polymarket's game-winner markets (gamma API): the two teams, best bid and ask. Only tight markets are used
     (spread of 3c or less), priced at the midpoint.
  2. Kalshi's game events for the same sport. A game is paired by its date and the two team codes in both sites'
     tickers (Polymarket 'nba-lac-sas-2026-11-19', Kalshi 'KXNBAGAME-26NOV19LACSAS'); a code may be a shortened form
     of the other ('SA' / 'SAS').
  3. The rule: a team whose Kalshi ask is 3c or more below Polymarket's midpoint after Kalshi's fee is bought once on
     paper at that real ask, and settles on Kalshi's result.
Fed decisions are compared the same way (added 21 Nov 2026): Polymarket's "Fed Decision in <month>?" events against
Kalshi's KXFEDDECISION ladder for the same meeting, outcome by outcome (cut 50+, cut 25, no change, hike 25, hike 50+).
An event is used only if all five outcomes are found on both sites; each outcome is bought once on paper when Kalshi's
ask is 3c+ under Polymarket's midpoint after the fee, and settles on Kalshi's result after the meeting.
Read-only public data from both sites. Places no orders anywhere."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

from .sports import ensure_schema

TAKER = 0.07
SPORTS = {"nba": "KXNBAGAME", "nfl": "KXNFLGAME", "nhl": "KXNHLGAME"}
GAMMA = "https://gamma-api.polymarket.com/events"
MIN_EDGE = 0.03
MAX_POLY_SPREAD = 0.03
HORIZON_H = 24
MIN_LEAD_SEC = 300                 # nothing is bought in the last 5 minutes before the start, or after it
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
FED_TAG, FED_SERIES = "fed-rates", "KXFEDDECISION"
FED_SLUG = re.compile(r"^fed-decision-in-([a-z]+)(?:-\d+)*$")
FED_MONTHS = "january february march april may june july august september october november december".split()
# Polymarket's outcome title -> Kalshi's market suffix (the Fed moves in 25 bp steps, so "50+ bps" and ">25bps" are the same event)
FED_OUTCOMES = {"50+ bps decrease": "C26", "25 bps decrease": "C25", "no change": "H0", "25 bps increase": "H25", "50+ bps increase": "H26"}


def fed_events(evs: list[dict], now: float) -> list[dict]:
    """Polymarket's 'Fed Decision in <month>?' events -> [{event (Kalshi event ticker), label, end, outcomes: {suffix: (bid, ask)}}].
    The meeting is the one in the month of the event's end date (US Eastern); the month in the slug must agree."""
    out = []
    for e in evs or []:
        m = FED_SLUG.match(str(e.get("slug") or ""))
        end = _ts(e.get("endDate"))
        if not m or m.group(1) not in FED_MONTHS or not end or end <= now:
            continue
        d = dt.datetime.fromtimestamp(end - 5 * 3600, dt.timezone.utc)
        if FED_MONTHS[d.month - 1] != m.group(1):
            continue
        oc = {}
        for mk in e.get("markets") or []:
            code = FED_OUTCOMES.get(str(mk.get("groupItemTitle") or "").strip().lower())
            b, a = fnum(mk.get("bestBid")), fnum(mk.get("bestAsk"))
            if code and not mk.get("closed") and mk.get("active", True) and b is not None and a is not None and 0 <= b <= a <= 1:
                oc[code] = (b, a)
        if set(oc) == set(FED_OUTCOMES.values()):                 # all five or nothing: a missing outcome may mean different rules
            out.append({"event": f"{FED_SERIES}-{d.year % 100:02d}{MON[d.month - 1]}", "label": f"Fed decision {MON[d.month - 1].title()} {d.year}",
                        "end": end, "outcomes": oc})
    return out

SCHEMA = """
CREATE TABLE IF NOT EXISTS poly_board (ticker TEXT PRIMARY KEY, sport TEXT, game TEXT, team TEXT, start_ts REAL, poly_bid REAL, poly_ask REAL,
    kalshi_bid REAL, kalshi_ask REAL, edge REAL, ts REAL);
CREATE TABLE IF NOT EXISTS poly_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT UNIQUE, event TEXT, sport TEXT, game TEXT, team TEXT,
    start_ts REAL, poly_mid REAL, price REAL, fee REAL, edge REAL, status TEXT DEFAULT 'open', result TEXT, pnl REAL, settled_ts REAL);
"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(s):
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")                        # Polymarket writes '2026-11-20 01:00:00+00'
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def poly_games(events: list[dict], sport: str) -> list[dict]:
    """Polymarket events -> [{date, codes (a, b), teams (a, b), bid, ask (for team a), start}] for plain game-winner markets."""
    out = []
    for e in events or []:
        m = re.fullmatch(rf"{sport}-([a-z0-9]+)-([a-z0-9]+)-(\d{{4}})-(\d{{2}})-(\d{{2}})", e.get("slug") or "")
        if not m:
            continue                                            # 'nba-lac-sas-2026-11-19-exact-margin' and the like
        for mk in e.get("markets") or []:
            if (mk.get("sportsMarketType") or "") != "moneyline" or mk.get("closed") or mk.get("active") is False:
                continue
            try:
                names = json.loads(mk.get("outcomes") or "[]")
            except ValueError:
                continue
            bid, ask = fnum(mk.get("bestBid")), fnum(mk.get("bestAsk"))
            start = _ts(mk.get("gameStartTime")) or _ts(e.get("startTime"))
            if len(names) != 2 or bid is None or ask is None or not 0 < bid < ask < 1 or start is None:
                continue
            out.append({"date": dt.date(int(m.group(3)), int(m.group(4)), int(m.group(5))), "codes": (m.group(1).upper(), m.group(2).upper()),
                        "teams": (str(names[0]), str(names[1])), "bid": bid, "ask": ask, "start": start})
            break
    return out


def kalshi_games(events: list[dict]) -> dict:
    """Kalshi game events -> {(date, frozenset(codes)): {code: market}} from tickers like KXNBAGAME-26NOV19LACSAS-LAC."""
    out = {}
    for e in events or []:
        ms = e.get("markets") or []
        m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)$", e.get("event_ticker") or "")
        if len(ms) != 2 or not m or m.group(2) not in MON:
            continue
        codes = [x["ticker"].rsplit("-", 1)[-1].upper() for x in ms]
        if "".join(codes) != m.group(4) and "".join(reversed(codes)) != m.group(4):
            continue                                            # the two markets must be exactly the two teams of the event
        d = dt.date(2000 + int(m.group(1)), MON.index(m.group(2)) + 1, int(m.group(3)))
        out[(d, frozenset(codes))] = {"event": e.get("event_ticker"), "by_code": dict(zip(codes, ms))}
    return out


def same_code(a: str, b: str) -> bool:
    """'SAS' and 'SA' are the same team; 'LAC' and 'LAL' are not."""
    return a == b or (len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a)))


def pair(pg: dict, kg: dict):
    """The Kalshi game for one Polymarket game: -> (event, market for team a, market for team b) or None."""
    a, b = pg["codes"]
    hits = []
    for (d, codes), g in kg.items():
        if d != pg["date"]:
            continue
        ka = [c for c in codes if same_code(c, a)]
        kb = [c for c in codes if same_code(c, b)]
        if len(ka) == 1 and len(kb) == 1 and ka[0] != kb[0]:
            hits.append((g["event"], g["by_code"][ka[0]], g["by_code"][kb[0]]))
    return hits[0] if len(hits) == 1 else None                  # two candidates = ambiguous = no pairing


class PolyWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client, session=None):
        import requests
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        self.http = session or requests.Session()
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self.state = {"running": False, "last": None, "error": None, "note": "", "games": 0, "paired": 0}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    def poly_events(self, sport: str) -> list[dict]:
        out = []
        for off in range(0, 800, 100):
            r = self.http.get(GAMMA, params={"tag_slug": sport, "closed": "false", "limit": 100, "offset": off},
                              headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=25)
            r.raise_for_status()
            page = r.json() or []
            out += page
            if len(page) < 100:
                break
        return out

    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        min_edge = float(self.opt("poly_min_edge", MIN_EDGE))
        games = paired = picks = 0
        errs, diag = [], {}
        self.db.execute("DELETE FROM poly_board WHERE start_ts<?", (now,))
        for sport, series in SPORTS.items():
            try:
                evs = self.poly_events(sport)
                allg = poly_games(evs, sport)
                pgs = [g for g in allg if now + MIN_LEAD_SEC < g["start"] <= now + HORIZON_H * 3600]
                diag[sport] = {"polymarket_events": len(evs), "game_markets": len(allg), "in_window": len(pgs), "paired": 0,
                               "sample_slugs": [e.get("slug") for e in evs[:3]]}
                if not pgs:
                    continue
                kg = kalshi_games(list(self.k.paginate("/events", "events", {"series_ticker": series, "status": "open", "with_nested_markets": "true",
                                                                           "limit": 200}, max_items=400)))
            except Exception as e:
                errs.append(f"{sport}: {e}"[:120])
                continue
            for g in pgs:
                games += 1
                mt = pair(g, kg)
                if not mt:
                    continue
                paired += 1
                diag[sport]["paired"] += 1
                ev, ma, mb = mt
                tight = g["ask"] - g["bid"] <= MAX_POLY_SPREAD + 1e-9
                name = f"{g['teams'][0]} vs {g['teams'][1]}"
                for m, team, pb, pa in ((ma, g["teams"][0], g["bid"], g["ask"]), (mb, g["teams"][1], 1 - g["ask"], 1 - g["bid"])):
                    kb, ka = fnum(m.get("yes_bid_dollars")) or 0.0, fnum(m.get("yes_ask_dollars"))
                    mid = (pb + pa) / 2
                    edge = (mid - ka - TAKER * ka * (1 - ka)) if ka and 0 < ka < 1 and tight else None
                    self.db.execute("INSERT OR REPLACE INTO poly_board VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (m["ticker"], sport, name, team, g["start"], round(pb, 4), round(pa, 4), kb, ka, edge, now))
                    if edge is not None and edge >= min_edge and self.opt("poly_enabled", True):
                        cur = self.db.execute("INSERT OR IGNORE INTO poly_picks(ts, ticker, event, sport, game, team, start_ts, poly_mid, price, fee, edge) "
                                              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                              (now, m["ticker"], ev, sport, name, team, g["start"], round(mid, 4), ka, round(TAKER * ka * (1 - ka), 6), round(edge, 4)))
                        picks += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        fed = {"meetings": 0, "paired": 0, "picks": 0}
        try:
            fed = self.scan_fed(now, min_edge)
        except Exception as e:
            errs.append(f"fed: {e}"[:120])
        diag["fed"] = fed
        picks += fed["picks"]
        self.settle(now)
        self.state.update(last=now, error="; ".join(errs) or None, games=games, paired=paired, by_sport=diag,
                          note=f"{games} Polymarket games starting within {HORIZON_H} h, {paired} paired with Kalshi; "
                               f"{fed['meetings']} Fed meeting(s), {fed['paired']} paired; {picks} new paper buy(s)")
        return {"games": games, "paired": paired, "picks": picks}

    def scan_fed(self, now: float, min_edge: float) -> dict:
        r = self.http.get(GAMMA, params={"tag_slug": FED_TAG, "closed": "false", "limit": 100, "offset": 0},
                          headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        fes = fed_events(r.json() or [], now)
        out = {"meetings": len(fes), "paired": 0, "picks": 0}
        if not fes:
            return out
        km = {m["ticker"]: m for m in self.k.markets(status="open", series_ticker=FED_SERIES, max_items=400)}
        for fe in fes:
            ms = {c: km.get(f"{fe['event']}-{c}") for c in fe["outcomes"]}
            if not all(ms.values()):
                continue                                          # Kalshi does not list all five for that meeting: skip it
            out["paired"] += 1
            for c, (pb, pa) in fe["outcomes"].items():
                m = ms[c]
                start = _ts(m.get("close_time")) or fe["end"]
                kb, ka = fnum(m.get("yes_bid_dollars")) or 0.0, fnum(m.get("yes_ask_dollars"))
                tight = pa - pb <= MAX_POLY_SPREAD + 1e-9
                mid = (pb + pa) / 2
                edge = (mid - ka - TAKER * ka * (1 - ka)) if ka and 0 < ka < 1 and tight else None
                team = m.get("yes_sub_title") or c
                self.db.execute("INSERT OR REPLACE INTO poly_board VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (m["ticker"], "fed", fe["label"], team, start, round(pb, 4), round(pa, 4), kb, ka, edge, now))
                if edge is not None and edge >= min_edge and self.opt("poly_enabled", True):
                    cur = self.db.execute("INSERT OR IGNORE INTO poly_picks(ts, ticker, event, sport, game, team, start_ts, poly_mid, price, fee, edge) "
                                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                          (now, m["ticker"], fe["event"], "fed", fe["label"], team, start, round(mid, 4), ka, round(TAKER * ka * (1 - ka), 6), round(edge, 4)))
                    out["picks"] += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        return out

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT id, ticker, price, fee FROM poly_picks WHERE status='open' AND start_ts<?", (now - 3600,))
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower() for m in self.k.markets(status=None, tickers=[r["ticker"] for r in rows][:200], max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = (1.0 if x == "yes" else 0.0) - r["price"] - r["fee"]
                self.db.execute("UPDATE poly_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(300)
        while not self._stop.is_set():
            if self.opt("poly_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("poly_every_min", 5)) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="polywatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        rs = self.db.query("SELECT pnl, price, fee FROM poly_picks WHERE status='settled'")
        cost = sum(r["price"] + r["fee"] for r in rs)
        return {"state": self.state, "min_edge_c": 100 * float(self.opt("poly_min_edge", MIN_EDGE)), "max_poly_spread_c": 100 * MAX_POLY_SPREAD,
                "totals": {"settled": len(rs), "won": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(sum(r["pnl"] for r in rs), 2),
                           "return_pct": round(100 * sum(r["pnl"] for r in rs) / cost, 1) if cost else None,
                           "open": self.db.query("SELECT COUNT(*) n FROM poly_picks WHERE status='open'")[0]["n"]},
                "board": self.db.query("SELECT * FROM poly_board ORDER BY start_ts, game, team"),
                "picks": self.db.query("SELECT * FROM poly_picks ORDER BY ts DESC LIMIT 100")}
