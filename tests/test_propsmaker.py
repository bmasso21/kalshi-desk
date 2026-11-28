"""Player props paper test: the Mentions resting-bid replay pointed at NBA/NFL player props. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import propsmaker as P  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


NOW = dt.datetime(2026, 11, 25, 17, tzinfo=dt.timezone.utc).timestamp()          # noon Eastern
START = NOW + 5 * 3600                                                           # tip-off 10 pm Eastern
C1 = START + 3 * 3600                                                            # Kalshi's expected end of the game
T = "KXNBAPTSPROT-26NOV25LALBOS-LJAMES-25"
T2 = "KXNBAPTSPROT-26NOV25NYKMIA-JBRUNSON-30"                                     # game starts in 20 min: too late to join
T3 = "KXNBAPTSPROT-26NOV25DENPHX-NJOKIC-30"                                       # no ESPN game found: left alone


class Resp:
    def __init__(self, j):
        self.j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self.j


def espn(start, a, b, state="pre"):
    return {"date": iso(start), "status": {"type": {"state": state}},
            "competitions": [{"competitors": [{"team": {"displayName": a[0], "abbreviation": a[1]}}, {"team": {"displayName": b[0], "abbreviation": b[1]}}]}]}


class Ses:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "basketball/nba" in url and params.get("dates") == "20261125":
            return Resp({"events": [espn(START, ("Los Angeles Lakers", "LAL"), ("Boston Celtics", "BOS")),
                                    espn(NOW + 1200, ("New York Knicks", "NY"), ("Miami Heat", "MIA"))]})
        return Resp({"events": []})


def gm(ev, a, an, b, bn):
    return {"event_ticker": ev, "markets": [{"ticker": f"{ev}-{a}", "yes_sub_title": an}, {"ticker": f"{ev}-{b}", "yes_sub_title": bn}]}


class K:
    def __init__(self):
        self.calls, self.res, self.trades = [], {}, {}

    def paginate(self, path, key, params, max_items=None):
        self.calls.append((path, params.get("series_ticker") or params.get("ticker")))
        if path == "/events" and params["series_ticker"] == "KXNBAGAME":
            yield gm("KXNBAGAME-26NOV25LALBOS", "LAL", "Los Angeles L", "BOS", "Boston")
            yield gm("KXNBAGAME-26NOV25NYKMIA", "NYK", "New York K", "MIA", "Miami")
            yield gm("KXNBAGAME-26NOV25DENPHX", "DEN", "Denver", "PHX", "Phoenix")
        elif path == "/events" and params["series_ticker"] == "KXNFLGAME":
            return
        elif path == "/events":
            assert params["status"] == "open" and params["series_ticker"] in P.PROP_SERIES, params
            if params["series_ticker"] == "KXNBAPTSPROT":
                mk = lambda t: {"ticker": t, "close_time": iso(NOW + 3 * 86400), "expected_expiration_time": iso(C1), "market_type": "binary", "status": "active"}
                yield {"event_ticker": "KXNBAPTSPROT-26NOV25LALBOS", "markets": [mk(T)]}
                yield {"event_ticker": "KXNBAPTSPROT-26NOV25NYKMIA", "markets": [mk(T2)]}
                yield {"event_ticker": "KXNBAPTSPROT-26NOV25DENPHX", "markets": [mk(T3)]}
        elif path == "/markets/trades":
            yield from self.trades.get(params["ticker"], [])

    def series_list(self, category=None):
        raise AssertionError("props use a fixed series list, never the category walk")

    def orderbooks(self, tickers):
        assert tickers == [T], tickers
        return {T: {"yes_dollars": [["0.4000", "120"]], "no_dollars": [["0.5500", "30"]]}}

    def markets(self, status=None, tickers=None, max_items=None, **kw):
        return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]

    def series(self, ticker):
        return {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def main():
    k = K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    ses = Ses()
    pm = P.PropsMaker(lambda: {}, wdb, k, session=ses)
    r = pm.scan(NOW)
    o = {x["side"]: x for x in wdb.query("SELECT * FROM prop_orders2")}
    check("every prop series is read directly (5 calls), no category walk",
          [c for c in k.calls if c[0] == "/events" and c[1] in P.PROP_SERIES] == [("/events", s) for s in P.PROP_SERIES], k.calls)
    check("game start times come from ESPN for today and tomorrow (Eastern), NBA and NFL",
          sorted((u.split("/sports/")[1].split("/scoreboard")[0], p["dates"]) for u, p in ses.calls) ==
          sorted((pth, d) for pth in ("basketball/nba", "football/nfl") for d in ("20261125", "20261126")), ses.calls)
    check("one resting bid per side at the best bid on a prop whose game starts in 5 h",
          set(o) == {"yes", "no"} and all(x["ticker"] == T for x in o.values()) and close(o["yes"]["price"], .40) and o["yes"]["queue_ahead"] == 120
          and close(o["no"]["price"], .55) and r["placed"] == 2, (r, o))
    check("the bid is pulled AT tip-off (ESPN), not an hour before Kalshi's expected end of the game", close(o["yes"]["cancel_ts"], START) and START < C1 - 3600)
    check("a game starting in 20 minutes, and a game ESPN does not list, are left alone",
          pm.state["games_matched"] == 2 and not wdb.query("SELECT 1 FROM prop_orders2 WHERE ticker IN (?, ?)", (T2, T3)), pm.state)
    check("props are kept in their own table: the Mentions table is untouched",
          not wdb.query("SELECT name FROM sqlite_master WHERE name='men_orders'") or not wdb.query("SELECT 1 FROM men_orders"))

    def kt(ts, yp, n, side):
        return {"created_time": iso(ts), "yes_price_dollars": str(yp), "count_fp": str(n), "taker_outcome_side": side}
    # YES bid 40c with 120 ahead: 125 sold into it -> 5 ours filled. NO bid 55c (YES 45c): a YES trade at 46c takes the level -> full 10.
    # After tip-off a YES trade at 2c would have taken the whole YES bid - but the bid is gone by then.
    k.trades = {T: [kt(NOW + 100, .40, 125, "no"), kt(NOW + 200, .46, 1, "yes"), kt(START + 600, .02, 50, "no")]}
    k.res = {T: "no"}
    pm.settle(C1 + 600)
    o = {x["side"]: x for x in wdb.query("SELECT * FROM prop_orders2")}
    check("replayed after settlement: YES filled 5 (the in-game trade through it does not count), NO filled 10", o["yes"]["filled"] == 5 and o["no"]["filled"] == 10, o)
    # maker fee 0.0175 x 10 x .55 x .45 = 0.0433 -> 0.05; YES 0.0175 x 5 x .4 x .6 = 0.021 -> 0.03
    check("held to settlement less the maker fee rounded up: NO +10 x 0.45 - 0.05, YES -5 x 0.40 - 0.03",
          close(o["no"]["pnl"], 4.5 - 0.05) and close(o["yes"]["pnl"], -2.0 - 0.03), (o["no"]["pnl"], o["yes"]["pnl"]))
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["props_maker"]
    check("on the scoreboard as its own row", row["score"]["trades"] == 2 and close(row["score"]["pnl"], 4.45 - 2.03, 0.01), row["score"])
    check("the client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
