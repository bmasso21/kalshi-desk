"""Mentions paper test: resting bids at the back of the queue, filled only by real trades. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import mentions as M  # noqa: E402
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


def tr(ts, yp, n, taker):
    return {"ts": ts, "yes_price": yp, "count": n, "taker": taker}


def main():
    check("Mentions markets are recognised by their series name", M.is_mention("KXNVDAMENTION-26NOV20-AI") and not M.is_mention("KXNBAGAME-26NOV20-X"))
    check("which outcome the taker bought: the stated side, else from the book side (bid = bought YES)",
          M.taker_side({"taker_outcome_side": "no"}) == "no" and M.taker_side({"taker_book_side": "bid"}) == "yes" and M.taker_side({}) is None)
    # our YES bid at 30c, 50 ahead, 10 contracts, placed at t=100, cancelled at t=1000
    R = lambda trades, side="yes", price=.30, ahead=50: M.replay(side, price, ahead, 10, 100, 1000, trades)
    check("a taker selling into our price uses up the queue ahead first: 40 traded, 50 ahead -> no fill", R([tr(200, .30, 40, "no")]) == 0)
    check("...the next 15 take the last 10 ahead and fill 5 of ours", R([tr(200, .30, 40, "no"), tr(300, .30, 15, "no")]) == 5)
    check("...and we never fill more than our size", R([tr(200, .30, 500, "no")]) == 10)
    check("a trade THROUGH our price (29c) means the whole level was taken: filled in full", R([tr(200, .29, 1, "no")]) == 10)
    check("takers buying YES trade with the other book, not our YES bid", R([tr(200, .30, 500, "yes")]) == 0)
    check("trades before the order was placed or after it was cancelled do not count", R([tr(50, .30, 500, "no"), tr(1000, .30, 500, "no")]) == 0)
    check("trades at other prices above ours do not reach us", R([tr(200, .31, 500, "no")]) == 0)
    # NO bid at 60c sits at YES price 40c: hit by takers buying YES at 40c, taken out by a YES trade above 40c
    check("a NO bid is hit by takers buying YES at 1 - price; a YES trade above that fills it in full",
          R([tr(200, .40, 55, "yes")], "no", .60) == 5 and R([tr(200, .41, 1, "yes")], "no", .60) == 10 and R([tr(200, .40, 55, "no")], "no", .60) == 0)

    # ---- the whole thing ----
    NOW = 1_795_200_000.0
    C1 = NOW + 5 * 3600                                           # closes in 5 hours: in the window

    class K:
        def __init__(self):
            self.res, self.trades, self.calls = {}, {}, []

        def paginate(self, path, key, params, max_items=None):
            self.calls.append(path)
            if path == "/events":
                assert params["status"] == "open" and params["with_nested_markets"] == "true" and params["series_ticker"] in ("KXNVDAMENTION", "KXQUIETMENTION")
                if params["series_ticker"] == "KXNVDAMENTION":
                    yield {"event_ticker": "KXNVDAMENTION-26NOV20", "markets": [
                        {"ticker": "KXNVDAMENTION-26NOV20-AI", "close_time": iso(C1), "market_type": "binary", "status": "active"},
                        {"ticker": "KXNVDAMENTION-26NOV20-CHINA", "close_time": iso(C1), "market_type": "binary", "status": "active"},
                        {"ticker": "KXNVDAMENTION-26NOV20-LATER", "close_time": iso(NOW + 30 * 3600), "market_type": "binary", "status": "active"},
                        {"ticker": "KXNVDAMENTION-26NOV20-SOON", "close_time": iso(NOW + 1800), "market_type": "binary", "status": "active"}]}
            elif path == "/markets/trades":
                for x in self.trades.get(params["ticker"], []):
                    yield x

        def series_list(self, category=None):
            assert category == "Mentions"
            self.calls.append("/series")
            return [{"ticker": "KXQUIETMENTION", "volume": 0}, {"ticker": "KXNVDAMENTION", "volume": 5000}]

        def orderbooks(self, tickers):
            assert set(tickers) <= {"KXNVDAMENTION-26NOV20-AI", "KXNVDAMENTION-26NOV20-CHINA"}, tickers   # only markets closing in 1-24 h
            return {"KXNVDAMENTION-26NOV20-AI": {"yes_dollars": [["0.3000", "50"], ["0.2900", "10"]], "no_dollars": [["0.6500", "20"]]},
                    "KXNVDAMENTION-26NOV20-CHINA": {"yes_dollars": [["0.0100", "900"]], "no_dollars": []}}

        def markets(self, status=None, tickers=None, max_items=None, **kw):
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]

        def series(self, ticker):
            return {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}

    k = K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    mp = M.MentionsPaper(lambda: {}, wdb, k)
    r = mp.scan(NOW)
    o = {(x["ticker"].rsplit("-", 1)[-1], x["side"]): x for x in wdb.query("SELECT * FROM men_orders")}
    check("first scan of the day walks EVERY Mentions series once and keeps the ones with open markets",
          k.calls.count("/series") == 1 and k.calls.count("/events") == 2 and mp._series[1] == ["KXNVDAMENTION"] and mp.state["open_series"] == 1, (k.calls, mp._series))
    check("a market's end is the earlier of its expected expiration and its scheduled close",
          M.end_time({"close_time": iso(NOW + 9 * 86400), "expected_expiration_time": iso(NOW + 5 * 3600)}) == NOW + 5 * 3600 and M.end_time({}) is None)
    check("the soonest-ending markets are reported for diagnosis", mp.state["soonest"][0]["ends_in_h"] == 0.5, mp.state["soonest"])
    check("one resting bid per side at the best bid, behind the size already showing; markets closing too soon or too late and prices under 3c are left alone",
          set(o) == {("AI", "yes"), ("AI", "no")} and close(o[("AI", "yes")]["price"], .30) and o[("AI", "yes")]["queue_ahead"] == 50
          and close(o[("AI", "no")]["price"], .65) and o[("AI", "no")]["queue_ahead"] == 20 and r["placed"] == 2, (r, sorted(o)))
    check("unfilled size is cancelled one hour before the close", close(o[("AI", "yes")]["cancel_ts"], C1 - 3600))
    mp.scan(NOW + 600)
    check("a market is quoted once", len(wdb.query("SELECT * FROM men_orders")) == 2)
    check("later scans that day look only at the series with open markets", k.calls.count("/series") == 1 and k.calls.count("/events") == 3, k.calls)
    check("nothing settles (and no trades are read) before the market closes", mp.settle(C1 - 10) == 0 and "/markets/trades" not in k.calls)

    def kt(ts, yp, n, side):
        return {"created_time": iso(ts), "yes_price_dollars": str(yp), "count_fp": str(n), "taker_outcome_side": side}
    # YES bid 30c: 60 sold into it -> 50 ahead, 10 ours. NO bid 65c (YES level 35c): only 15 bought there -> 20 ahead, no fill.
    k.trades = {"KXNVDAMENTION-26NOV20-AI": [kt(NOW + 100, .30, 60, "no"), kt(NOW + 200, .35, 15, "yes")]}
    k.res = {"KXNVDAMENTION-26NOV20-AI": "yes"}
    n = mp.settle(C1 + 600)
    o = {x["side"]: x for x in wdb.query("SELECT * FROM men_orders")}
    # maker fee: 0.0175 x 10 x 0.3 x 0.7 = 0.03675 -> rounded up to the cent = 0.04
    check("after settlement the real trades are replayed once: the YES bid filled 10, the NO bid stayed unfilled",
          n == 2 and o["yes"]["status"] == "filled" and o["yes"]["filled"] == 10 and o["no"]["status"] == "unfilled" and k.calls.count("/markets/trades") == 1, o)
    check("filled contracts are held to settlement: 10 x (1 - 0.30) less the maker fee (rounded up to the cent)",
          close(o["yes"]["fee"], 0.04) and close(o["yes"]["pnl"], 10 * 0.70 - 0.04), (o["yes"]["fee"], o["yes"]["pnl"]))
    s = mp.status()
    empty = M.MentionsPaper(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "e.db"), k).status()["totals"]
    check("with nothing placed yet the totals are zeros, never blanks", empty["resting"] == 0 and empty["filled"] == 0 and empty["unfilled"] == 0 and empty["won"] == 0, empty)
    check("status: filled / unfilled / resting counts and the result", s["totals"]["filled"] == 1 and s["totals"]["unfilled"] == 1 and close(s["totals"]["pnl"], 6.96)
          and close(s["totals"]["staked"], 3.0) and s["totals"]["events"] == 1, s["totals"])
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["mentions_maker"]
    check("filled orders appear on the scoreboard, one event per speech", row["score"]["trades"] == 1 and row["score"]["events"] == 1 and close(row["score"]["pnl"], 6.96, 0.01), row["score"])
    check("the client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
