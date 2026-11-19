"""Gas paper test: tomorrow's daily gas contracts against the trend model, at real asks. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gasfv as GS  # noqa: E402
from app import gaswatch as GW  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.goldfv import phi  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def et(y, m, d, hh=0, mm=0, off=5):
    return (dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc) + dt.timedelta(hours=off)).timestamp()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    D = dt.date
    # AAA figures: zigzag +-1c for 40 days ending Nov 17 at 3.80, then (optionally) Nov 18 = 3.79
    days = [D(2026, 10, 9) + dt.timedelta(days=i) for i in range(40)]
    hist = {d: 3.80 + (0.01 if (len(days) - 1 - i) % 2 else 0.0) for i, d in enumerate(days)}
    assert days[-1] == D(2026, 11, 17) and close(hist[days[-1]], 3.80)

    class K:
        def __init__(self):
            self.vals = dict(hist)
            self.open = []
            self.res = {}
            self.calls = 0

        def paginate(self, path, key, params, max_items=None):
            assert path == "/markets" and params["status"] == "settled" and params["series_ticker"] == "KXAAAGASD"
            self.calls += 1
            for d, v in self.vals.items():                       # day d's figure sits on the contract that closed 11:59 p.m. the evening before
                yield {"close_time": iso(et(d.year, d.month, d.day) - 60), "expiration_value": str(v)}

        def markets(self, status="open", series_ticker=None, tickers=None, max_items=None, **kw):
            if tickers:
                return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
            return self.open

    def mk(strike, yb, ya, day=(2026, 11, 19)):
        return {"ticker": f"KXAAAGASD-T{strike}", "strike_type": "greater", "floor_strike": strike, "close_time": iso(et(*day) - 60),
                "yes_bid_dollars": str(yb), "yes_ask_dollars": str(ya)}

    k = K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    gw = GW.GasWatch(lambda: {}, wdb, k)
    k.open = [mk(3.78, .60, .64), mk(3.80, .30, .34)]
    noon18 = et(2026, 11, 18, 12)
    r = gw.scan(noon18)
    check("today's AAA figure not in yet -> nothing priced, nothing bought", r["waiting"] and not wdb.query("SELECT 1 FROM gas_picks") and "waiting" in gw.state["note"], (r, gw.state["note"]))

    # Nov 18's figure arrives: 3.79. Last 3 changes: -1, +1, -1 -> trend -1/3c -> expects 3.7867 for Nov 19
    k.vals[D(2026, 11, 18)] = 3.79
    gw._vals = (0.0, {})
    g = GS.Gas(k.vals)
    mu, sd = g.forecast(D(2026, 11, 18), 1, True)
    k.open = [mk(3.78, .60, .64),                      # trend says P(>3.78): YES at 64c, NO at 40c
              mk(3.80, .30, .34),                      # P(>3.80) low: NO at 70c
              mk(3.90, .00, .02),                      # NO has no price (no YES bid); YES at 2c is under the 20c floor
              mk(3.70, .97, .99),                      # YES 99c over the 95c cap; NO at 3c under the floor
              mk(3.79, .40, .60, day=(2026, 11, 20))]  # a later day's contract: ignored
    r = gw.scan(noon18)
    b = {x["strike"]: x for x in wdb.query("SELECT * FROM gas_board")}
    p78 = phi((mu - 3.78) / sd)
    check("board: every contract for TOMORROW priced with the trend model (and the no-trend one beside it); other days ignored",
          r["rows"] == 4 and set(b) == {3.78, 3.80, 3.90, 3.70} and close(b[3.78]["fair"], round(p78, 4), 1e-4), (r, sorted(b)))
    fee = lambda a: 0.07 * a * (1 - a)
    check("edges at the REAL asks: YES at the ask, NO at 1 - YES bid, less the fee; no YES bid -> NO has no price",
          close(b[3.78]["yes_edge"], p78 - .64 - fee(.64), 1e-6) and close(b[3.78]["no_edge"], (1 - p78) - .40 - fee(.40), 1e-6) and b[3.90]["no_edge"] is None, b[3.78])
    picks = {(x["strike"], x["side"]): x for x in wdb.query("SELECT * FROM gas_picks")}
    want = set()
    for s_, x in b.items():
        for side, e, price in (("yes", x["yes_edge"], x["yes_ask"]), ("no", x["no_edge"], round(1 - x["yes_bid"], 4) if x["yes_bid"] else None)):
            if e is not None and price is not None and e >= 0.05 and 0.20 <= price <= 0.95:
                want.add((s_, side))
    check("the rule: 5c+ under the trend model at 20-95c is bought once at its real ask; nothing else", set(picks) == want and len(want) >= 1, (sorted(picks), sorted(want)))
    gw.scan(noon18 + 600)
    check("a buy is recorded once per contract and side", len(wdb.query("SELECT * FROM gas_picks")) == len(want))
    check("AAA history is re-read at most every 20 minutes", k.calls == 2, k.calls)

    # buying hours
    wdb2 = WarehouseDB(Path(tempfile.mkdtemp()) / "w2.db")
    gw2 = GW.GasWatch(lambda: {}, wdb2, k)
    r2 = gw2.scan(et(2026, 11, 18, 19))
    check("after 6 p.m. Eastern the board still updates but nothing is bought", r2["rows"] == 4 and r2["picks"] == 0 and "outside buying hours" in gw2.state["note"], (r2, gw2.state["note"]))
    gw3 = GW.GasWatch(lambda: {"gas_enabled": False}, WarehouseDB(Path(tempfile.mkdtemp()) / "w3.db"), k)
    check("switched off -> no buys", gw3.scan(noon18)["picks"] == 0)

    # settlement
    check("nothing settles before the contract closes", gw.settle(noon18 + 3600) == 0)
    k.res = {"KXAAAGASD-T3.78": "no", "KXAAAGASD-T3.8": "no"}
    n = gw.settle(et(2026, 11, 19, 8))
    st = wdb.query("SELECT * FROM gas_picks")
    check("settles on Kalshi's result: payout less the ask and fee", n == len(want) and all(
        x["status"] == "settled" and close(x["pnl"], (1 if x["side"] == x["result"] else 0) - x["price"] - x["fee"], 1e-6) for x in st), st)
    s = gw.status()
    check("status: YES and NO results kept apart", s["by_side"]["no"]["settled"] + s["by_side"]["yes"]["settled"] == len(want)
          and s["by_side"]["no"]["won"] == s["by_side"]["no"]["settled"] and s["by_side"]["yes"]["won"] == 0, s["by_side"])
    rows = {x["key"]: x for x in SB.board(wdb)["strategies"]}
    check("the scoreboard scores gas NO and gas YES as two strategies", rows["gas_no"]["score"]["trades"] == s["by_side"]["no"]["settled"]
          and rows["gas_yes"]["score"].get("trades", 0) == s["by_side"]["yes"]["settled"], (rows["gas_no"]["score"], rows["gas_yes"]["score"]))
    check("the client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
