"""Games already decided, paper test: is a game's winner still for sale on Kalshi after the final whistle?

Kalshi closes a game market a minute or two after the game ends and pays out a few minutes later. In the minutes
between, the winner trades at 99c (seen on 23 Nov 2026: 38,000 contracts in the last two minutes of one NBA game,
all at 99c). Buying the winner there pays 1c a contract less Kalshi's fee - about 0.9c on a 100-contract order -
within minutes, IF the game really is over. The risk is the rare reversal (a reviewed call, a scoring correction)
and buying before the final buzzer.

Every minute while games are on, ESPN's public scoreboard (free, no account) is read for NBA, NFL, NHL, college
football and college basketball. When a game turns final, its Kalshi winner market is found (same date and teams)
and the cheaper of two equivalent buys is recorded on paper: the winner's YES at its ask, or the loser's NO at
1 minus the loser's YES bid. 100 contracts, only if that many are offered at that price, only if the market is still
open; the fee is Kalshi's, rounded up to the cent on the order. Settles on Kalshi's result. Also recorded: how long
after the final Kalshi closed the market, and how often there was nothing to buy.
Read-only public data. Places no orders."""
from __future__ import annotations

import datetime as dt
import math
import threading
import time

from .sports import ensure_schema, match_fixture

TAKER = 0.07
SIZE = 100
MAX_PRICE = 0.99
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
# ESPN path, extra parameters, Kalshi game series
LEAGUES = {"nba": ("basketball/nba", {}, "KXNBAGAME"),
           "nfl": ("football/nfl", {}, "KXNFLGAME"),
           "nhl": ("hockey/nhl", {}, "KXNHLGAME"),
           "ncaaf": ("football/college-football", {"groups": "80", "limit": "300"}, "KXNCAAFGAME"),
           "ncaab": ("basketball/mens-college-basketball", {"groups": "50", "limit": "400"}, "KXNCAAMBGAME")}
ACTIVE_EVERY = 60                 # seconds between looks at a league with a game on (or about to start / just ended)
IDLE_EVERY = 900                  # otherwise

SCHEMA = """
CREATE TABLE IF NOT EXISTS final_games (game_id TEXT PRIMARY KEY, league TEXT, name TEXT, winner TEXT, final_seen REAL, start_ts REAL,
    event TEXT, status TEXT, note TEXT, kalshi_closed_ts REAL);
CREATE TABLE IF NOT EXISTS final_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, game_id TEXT UNIQUE, league TEXT, name TEXT, event TEXT,
    ticker TEXT, side TEXT, price REAL, size REAL, offered REAL, fee REAL, secs_after_final REAL, status TEXT DEFAULT 'open', result TEXT,
    pnl REAL, settled_ts REAL);
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


def order_fee(price: float, n: float) -> float:
    """Kalshi's taker fee on one order: 7% x contracts x P x (1-P), rounded UP to the cent."""
    return math.ceil(round(TAKER * n * price * (1 - price) * 100, 6)) / 100


def finals(js: dict) -> list[dict]:
    """ESPN scoreboard -> every completed game with a clear winner: id, name, start, teams (name, abbr, winner)."""
    out = []
    for e in (js or {}).get("events") or []:
        st = ((e.get("status") or {}).get("type") or {})
        comp = (e.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if not st.get("completed") or st.get("state") != "post" or len(cs) != 2:
            continue
        sc = [fnum(c.get("score")) for c in cs]
        win = [c for c in cs if c.get("winner") is True]
        if len(win) != 1 or None in sc or sc[0] == sc[1]:
            continue                                               # no single winner (tie, missing score): never guess
        if (max(sc) == fnum(win[0].get("score"))) is False:
            continue                                               # winner flag and score disagree: skip
        t = [{"name": (c.get("team") or {}).get("displayName") or "", "abbr": (c.get("team") or {}).get("abbreviation") or "",
              "winner": c.get("winner") is True} for c in cs]
        out.append({"id": str(e.get("id")), "name": e.get("shortName") or e.get("name") or "", "start": _ts(e.get("date")), "teams": t})
    return out


def live_states(js: dict, now: float) -> bool:
    """Is any game on, starting within 20 minutes, or finished within the last 30 minutes?"""
    for e in (js or {}).get("events") or []:
        st = ((e.get("status") or {}).get("type") or {})
        s = _ts(e.get("date"))
        if st.get("state") == "in":
            return True
        if st.get("state") == "pre" and s and s - now < 1200:
            return True
        if st.get("state") == "post" and s and now - s < 6 * 3600:
            return True
    return False


class FinalWatch:
    def __init__(self, cfg_getter, wdb, kalshi_client, session=None):
        import requests
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        self.http = session or requests.Session()
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self._next: dict = {}                  # league -> when to look again
        self._kev: dict = {}                   # series -> (fetched at, {event: [markets]})
        self.state = {"running": False, "last": None, "error": None, "note": "", "by_league": {}}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    def scoreboard(self, league: str, day: dt.date | None = None) -> dict:
        path, extra, _ = LEAGUES[league]
        params = dict(extra)
        if day:
            params["dates"] = day.strftime("%Y%m%d")
        r = self.http.get(ESPN.format(path=path), params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def kalshi_events(self, series: str, now: float) -> dict:
        c = self._kev.get(series)
        if c and now - c[0] < 120:
            return c[1]
        evs = {}
        for e in self.k.paginate("/events", "events", {"series_ticker": series, "with_nested_markets": "true", "limit": 200,
                                                     "min_close_ts": int(now - 2 * 86400)}, max_items=600):
            evs[e.get("event_ticker")] = e.get("markets") or []
        self._kev[series] = (now, evs)
        return evs

    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        et = dt.datetime.fromtimestamp(now - 5 * 3600, dt.timezone.utc)
        days = [et.date()] + ([et.date() - dt.timedelta(days=1)] if et.hour < 6 else [])
        picks = seen = 0
        errs = []
        for lg, (_, _, series) in LEAGUES.items():
            if now < self._next.get(lg, 0):
                continue
            active = False
            try:
                for d in days:
                    js = self.scoreboard(lg, d)
                    active = active or live_states(js, now)
                    for g in finals(js):
                        if g["start"] and now - g["start"] > 12 * 3600:
                            continue                               # old game: Kalshi has long settled it
                        p, s = self.on_final(lg, series, g, now)
                        picks += p
                        seen += s
            except Exception as e:
                errs.append(f"{lg}: {e}"[:120])
            self._next[lg] = now + (ACTIVE_EVERY if active else IDLE_EVERY)
            self.state["by_league"][lg] = {"active": active, "next": self._next[lg]}
        self.track_close(now)
        self.settle(now)
        self.state.update(last=now, error="; ".join(errs) or None,
                          note=f"{seen} game(s) newly final, {picks} new paper buy(s)")
        return {"new_finals": seen, "picks": picks}

    def on_final(self, lg: str, series: str, g: dict, now: float) -> tuple[int, int]:
        if self.db.query("SELECT 1 FROM final_games WHERE game_id=?", (g["id"],)):
            return 0, 0
        win = next(t for t in g["teams"] if t["winner"])
        lose = next(t for t in g["teams"] if not t["winner"])
        mt = match_fixture({"start_ts": g["start"] or now, "p1": win["name"], "p2": lose["name"], "p1_abbr": win["abbr"], "p2_abbr": lose["abbr"]},
                           self.kalshi_events(series, now))
        status, note, ev, pick = "no_kalshi_game", "", None, 0
        if mt:
            ev, tw, tl, _how = mt                                   # tickers of the winner's and the loser's markets
            mw = self.k.market(tw) or {"ticker": tw}                 # fresh quotes at this moment
            ml = self.k.market(tl) or {"ticker": tl}
            if (mw.get("status") or "") not in ("active", "open"):
                status, note = "already_closed", f"Kalshi status {mw.get('status')}"
            else:
                opts = []
                ya, ys = fnum(mw.get("yes_ask_dollars")), fnum(mw.get("yes_ask_size_fp"))
                if ya and 0 < ya <= MAX_PRICE + 1e-9:
                    opts.append((ya, ys, mw["ticker"], "yes"))
                lb, ls = fnum(ml.get("yes_bid_dollars")), fnum(ml.get("yes_bid_size_fp"))
                if lb and 0 < lb < 1 and 1 - lb <= MAX_PRICE + 1e-9:
                    opts.append((round(1 - lb, 4), ls, ml["ticker"], "no"))
                opts.sort(key=lambda o: (o[0], -(o[1] or 0)))
                if not opts:
                    status, note = "nothing_under_99c", f"winner ask {ya}, loser bid {lb}"
                elif (opts[0][1] or 0) + 1e-9 < SIZE:
                    status, note = "too_thin", f"{opts[0][1]} offered at {opts[0][0]}"
                else:
                    price, offered, tk, side = opts[0]
                    fee = order_fee(price, SIZE)
                    if self.opt("final_enabled", False):
                        cur = self.db.execute("INSERT OR IGNORE INTO final_picks(ts, game_id, league, name, event, ticker, side, price, size, offered, fee, secs_after_final) "
                                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (now, g["id"], lg, g["name"], ev, tk, side, price, SIZE, offered, fee, 0.0))
                        pick = 1 if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
                    status, note = "bought", f"{side.upper()} {tk} at {price}"
        self.db.execute("INSERT OR IGNORE INTO final_games(game_id, league, name, winner, final_seen, start_ts, event, status, note) VALUES(?,?,?,?,?,?,?,?,?)",
                        (g["id"], lg, g["name"], win["name"], now, g["start"], ev, status, note))
        return pick, 1

    def track_close(self, now: float):
        """For games seen final with the Kalshi market still open: note when Kalshi closed it (the real window)."""
        rows = self.db.query("SELECT game_id, event FROM final_games WHERE event IS NOT NULL AND kalshi_closed_ts IS NULL AND final_seen > ? "
                             "AND status IN ('bought', 'nothing_under_99c', 'too_thin')", (now - 6 * 3600,))   # Kalshi was still open at the final
        for r in rows[:20]:
            ms = self.k.markets(status=None, event_ticker=r["event"], max_items=10)
            ct = [_ts(m.get("close_time")) for m in ms if (m.get("status") or "") not in ("active", "open")]
            ct = [c for c in ct if c]
            if ct:
                self.db.execute("UPDATE final_games SET kalshi_closed_ts=? WHERE game_id=?", (min(ct), r["game_id"]))

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query("SELECT id, ticker, side, price, size, fee FROM final_picks WHERE status='open'")
        if not rows:
            return 0
        res = {m["ticker"]: (m.get("result") or "").lower() for m in self.k.markets(status=None, tickers=[r["ticker"] for r in rows][:200], max_items=200)}
        n = 0
        for r in rows:
            x = res.get(r["ticker"])
            if x in ("yes", "no"):
                pnl = r["size"] * ((1.0 if r["side"] == x else 0.0) - r["price"]) - r["fee"]
                self.db.execute("UPDATE final_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?", (x, round(pnl, 6), now, r["id"]))
                n += 1
        return n

    def loop(self):
        self._stop.wait(420)
        while not self._stop.is_set():
            if self.opt("final_enabled", False):        # retired 25 Nov 2026: final_enabled = true brings it back
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(30)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="finalwatch").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        rs = self.db.query("SELECT pnl, price, size, fee FROM final_picks WHERE status='settled'")
        cost = sum(r["price"] * r["size"] + r["fee"] for r in rs)
        games = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM final_games GROUP BY status")}
        win = self.db.query("SELECT kalshi_closed_ts - final_seen w FROM final_games WHERE kalshi_closed_ts IS NOT NULL")
        ws = sorted(r["w"] for r in win)
        return {"state": self.state, "rule": {"size": SIZE, "max_price_c": int(MAX_PRICE * 100)},
                "games": games, "close_after_final_s": {"n": len(ws), "median": round(ws[len(ws) // 2]) if ws else None,
                                                         "min": round(ws[0]) if ws else None, "max": round(ws[-1]) if ws else None},
                "totals": {"settled": len(rs), "won": sum(1 for r in rs if r["pnl"] > 0), "pnl": round(sum(r["pnl"] for r in rs), 2),
                           "return_pct": round(100 * sum(r["pnl"] for r in rs) / cost, 3) if cost else None,
                           "open": self.db.query("SELECT COUNT(*) n FROM final_picks WHERE status='open'")[0]["n"]},
                "recent_games": self.db.query("SELECT * FROM final_games ORDER BY final_seen DESC LIMIT 60"),
                "picks": self.db.query("SELECT * FROM final_picks ORDER BY ts DESC LIMIT 100")}
