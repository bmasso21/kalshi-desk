"""Kalshi against Polymarket: pairing the same game on both sites and the paper rule. Hand-checkable."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import polywatch as P  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


NOW = dt.datetime(2026, 11, 19, 20, 0, tzinfo=dt.timezone.utc).timestamp()
START = "2026-11-20 01:00:00+00"                                   # 5 hours after NOW


def pev(slug, names, bid, ask, typ="moneyline", start=START, closed=False):
    return {"slug": slug, "startTime": "2026-11-20T01:00:00Z",
            "markets": [{"sportsMarketType": typ, "outcomes": json.dumps(names), "bestBid": bid, "bestAsk": ask, "gameStartTime": start, "closed": closed, "active": True}]}


def kev(ev, codes, prices):
    return {"event_ticker": ev, "markets": [{"ticker": f"{ev}-{c}", "yes_bid_dollars": str(b), "yes_ask_dollars": str(a)} for c, (b, a) in zip(codes, prices)]}


def main():
    check("Polymarket's time format ('2026-11-20 01:00:00+00') is read", close(P._ts(START), NOW + 5 * 3600) and close(P._ts("2026-11-20T01:00:00Z"), NOW + 5 * 3600))
    evs = [pev("nba-lac-sas-2026-11-19", ["Clippers", "Spurs"], .40, .42),
           pev("nba-lac-sas-2026-11-19-exact-margin", ["Yes", "No"], .1, .2, "exact_margin"),
           pev("nba-nyk-min-2026-11-19", ["Knicks", "Timberwolves"], .04, .39),
           pev("nba-bos-mia-2026-11-19", ["Celtics", "Heat"], .60, .61, closed=True)]
    pg = P.poly_games(evs, "nba")
    check("only plain game-winner markets are taken (no 'exact margin' events, no closed markets), with date, codes, teams and prices",
          len(pg) == 2 and pg[0]["codes"] == ("LAC", "SAS") and pg[0]["teams"] == ("Clippers", "Spurs") and pg[0]["date"] == dt.date(2026, 11, 19)
          and close(pg[0]["start"], NOW + 5 * 3600), pg)
    kg = P.kalshi_games([kev("KXNBAGAME-26NOV19LACSA", ["LAC", "SA"], [(.33, .35), (.60, .62)]),
                         kev("KXNBAGAME-26NOV19NYKMIN", ["NYK", "MIN"], [(.20, .22), (.77, .79)]),
                         kev("KXNBAGAME-26NOV20LACSA", ["LAC", "SA"], [(.5, .52), (.48, .5)])])
    check("Kalshi game events are keyed by date and the two team codes in the ticker", len(kg) == 3 and (dt.date(2026, 11, 19), frozenset(("LAC", "SA"))) in kg)
    check("a shortened code is the same team ('SAS' / 'SA'); different teams of one city are not ('LAC' / 'LAL')",
          P.same_code("SAS", "SA") and P.same_code("NY", "NYK") and not P.same_code("LAC", "LAL") and not P.same_code("S", "SAS"))
    mt = P.pair(pg[0], kg)
    check("a game pairs with the Kalshi event of the same date and teams, each team with its own market",
          mt and mt[0] == "KXNBAGAME-26NOV19LACSA" and mt[1]["ticker"].endswith("-LAC") and mt[2]["ticker"].endswith("-SA"), mt)
    check("no Kalshi game that day -> no pairing", P.pair({**pg[0], "date": dt.date(2026, 11, 25)}, kg) is None)
    amb = dict(kg)
    amb[(dt.date(2026, 11, 19), frozenset(("LA", "SAS")))] = {"event": "X", "by_code": {"LA": {"ticker": "X-LA"}, "SAS": {"ticker": "X-SAS"}}}
    check("two Kalshi games that could both be it -> no pairing (never a guess)", P.pair(pg[0], amb) is None)

    # ---- the scanner ----
    class Resp:
        def __init__(self, j):
            self.j = j

        def raise_for_status(self):
            pass

        def json(self):
            return self.j

    class Ses:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, headers=None, timeout=None):
            self.n += 1
            assert "gamma-api.polymarket.com" in url and params["closed"] == "false"
            return Resp(evs if params["tag_slug"] == "nba" and params["offset"] == 0 else [])

    class K:
        def __init__(self):
            self.res = {}

        def paginate(self, path, key, params, max_items=None):
            assert path == "/events" and params["series_ticker"] == "KXNBAGAME"
            yield kev("KXNBAGAME-26NOV19LACSA", ["LAC", "SA"], [(.33, .35), (.60, .62)])
            yield kev("KXNBAGAME-26NOV19NYKMIN", ["NYK", "MIN"], [(.10, .12), (.77, .79)])

        def markets(self, status=None, tickers=None, max_items=None, **kw):
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
    k, ses = K(), Ses()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    pw = P.PolyWatch(lambda: {}, wdb, k, session=ses)
    r = pw.scan(NOW)
    b = {x["ticker"].rsplit("-", 1)[-1]: x for x in wdb.query("SELECT * FROM poly_board")}
    # Clippers: Polymarket 40/42 -> mid 41c; Kalshi ask 35c; fee 0.07*.35*.65 = 1.59c -> edge 4.41c. Spurs: mid 59c vs ask 62c -> negative.
    check("edge = Polymarket midpoint - Kalshi ask - Kalshi fee, per team (the second team is 1 minus the first)",
          close(b["LAC"]["edge"], .41 - .35 - .07 * .35 * .65) and close(b["SA"]["poly_bid"], .58) and close(b["SA"]["poly_ask"], .60) and b["SA"]["edge"] < 0, (b.get("LAC"), b.get("SA")))
    check("a wide Polymarket market (4/39c) gives no edge at all, however cheap Kalshi looks", b["NYK"]["edge"] is None and b["MIN"]["edge"] is None)
    pk = wdb.query("SELECT * FROM poly_picks")
    check("3c+ under Polymarket is bought once on paper at Kalshi's real ask", len(pk) == 1 and pk[0]["ticker"].endswith("-LAC") and close(pk[0]["price"], .35)
          and close(pk[0]["poly_mid"], .41) and r == {"games": 2, "paired": 2, "picks": 1}, (pk, r))
    pw.scan(NOW + 300)
    check("a buy is recorded once", len(wdb.query("SELECT * FROM poly_picks")) == 1)
    late = P.PolyWatch(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "l.db"), k, session=Ses())
    check("nothing is bought in the last 5 minutes before the start or after it", late.scan(NOW + 5 * 3600 - 200)["games"] == 0)
    check("nothing settles until an hour after the start", pw.settle(NOW + 5 * 3600 + 600) == 0)
    k.res = {"KXNBAGAME-26NOV19LACSA-LAC": "yes"}
    pw.settle(NOW + 9 * 3600)
    p = wdb.query("SELECT * FROM poly_picks")[0]
    check("settles on Kalshi's result: payout less the ask and fee", p["status"] == "settled" and close(p["pnl"], 1 - .35 - .07 * .35 * .65, 1e-5), p)
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["poly_gap"]
    check("the scoreboard scores it", row["score"]["trades"] == 1 and row["score"]["wins"] == 1, row["score"])
    check("the Kalshi client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
