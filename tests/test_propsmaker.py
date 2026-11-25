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


NOW = 1_795_200_000.0
C1 = NOW + 6 * 3600
T = "KXNBAPTSPROT-26NOV25LALBOS-LJAMES-25"


class K:
    def __init__(self):
        self.calls, self.res, self.trades = [], {}, {}

    def paginate(self, path, key, params, max_items=None):
        self.calls.append((path, params.get("series_ticker") or params.get("ticker")))
        if path == "/events":
            assert params["status"] == "open" and params["series_ticker"] in P.PROP_SERIES, params
            if params["series_ticker"] == "KXNBAPTSPROT":
                yield {"event_ticker": "KXNBAPTSPROT-26NOV25LALBOS", "markets": [
                    {"ticker": T, "close_time": iso(C1), "market_type": "binary", "status": "active"},
                    {"ticker": T[:-2] + "40", "close_time": iso(NOW + 40 * 3600), "market_type": "binary", "status": "active"}]}
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
    pm = P.PropsMaker(lambda: {}, wdb, k)
    r = pm.scan(NOW)
    o = {x["side"]: x for x in wdb.query("SELECT * FROM prop_orders")}
    check("every prop series is read directly (5 calls), no category walk", [c for c in k.calls if c[0] == "/events"] == [("/events", s) for s in P.PROP_SERIES], k.calls)
    check("one resting bid per side at the best bid on a prop closing in 1-24 h; one closing in 40 h is left alone",
          set(o) == {"yes", "no"} and close(o["yes"]["price"], .40) and o["yes"]["queue_ahead"] == 120 and close(o["no"]["price"], .55) and r["placed"] == 2, (r, o))
    check("cancelled one hour before the close", close(o["yes"]["cancel_ts"], C1 - 3600))
    check("props are kept in their own table: the Mentions table is untouched",
          not wdb.query("SELECT name FROM sqlite_master WHERE name='men_orders'") or not wdb.query("SELECT 1 FROM men_orders"))

    def kt(ts, yp, n, side):
        return {"created_time": iso(ts), "yes_price_dollars": str(yp), "count_fp": str(n), "taker_outcome_side": side}
    # YES bid 40c with 120 ahead: 125 sold into it -> 5 ours filled. NO bid 55c (YES 45c): a YES trade at 46c takes the level -> full 10.
    k.trades = {T: [kt(NOW + 100, .40, 125, "no"), kt(NOW + 200, .46, 1, "yes")]}
    k.res = {T: "no"}
    pm.settle(C1 + 600)
    o = {x["side"]: x for x in wdb.query("SELECT * FROM prop_orders")}
    check("replayed after settlement: YES filled 5, NO filled 10", o["yes"]["filled"] == 5 and o["no"]["filled"] == 10, o)
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
