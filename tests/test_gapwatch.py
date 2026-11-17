"""Hourly cushion watch: paper buys at the real ask when the underlying sits far from the strike; no orders exist."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gapwatch as GW  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


CLOSE = 1_790_002_800.0          # a top-of-the-hour moment (divisible by 3600)
ISO = dt.datetime.fromtimestamp(CLOSE, dt.timezone.utc).isoformat().replace("+00:00", "Z")


class FakeKalshi:
    """Public market data only. Any order method would be an AttributeError."""
    def __init__(self):
        self.calls = []
        self.results = {}
        # strikes 3830..3880 every 10; gold ~3855: 3850 at 68/72, 3860 at 28/32 -> level = 3850 + (0.70-0.5)/(0.70-0.30)*10 = 3855
        q = {3830: (.97, .98), 3840: (.90, .92), 3850: (.68, .72), 3860: (.28, .32), 3870: (.06, .08), 3880: (.01, .03)}
        self.book = [{"ticker": f"G-T{k}", "strike_type": "greater", "floor_strike": k, "close_time": ISO,
                      "yes_bid_dollars": str(b), "yes_ask_dollars": str(a), "last_price_dollars": str(round(b + .01, 2))} for k, (b, a) in q.items()]
        self.book.append({"ticker": "G-OTHER", "strike_type": "greater", "floor_strike": 3800, "close_time": "2030-01-01T00:00:00Z",
                          "yes_bid_dollars": "0.5", "yes_ask_dollars": "0.6"})

    def markets(self, status="open", event_ticker=None, series_ticker=None, tickers=None, exclude_combos=True, max_items=None):
        self.calls.append((status, series_ticker, tuple(tickers or ())))
        if tickers:
            return [{"ticker": t, "result": self.results.get(t, "")} for t in tickers]
        return list(self.book)


def main():
    tmp = tempfile.mkdtemp()
    wdb = WarehouseDB(Path(tmp) / "w.db")
    k = FakeKalshi()
    cfg = {}
    gw = GW.GapWatch(lambda: cfg, wdb, k)
    check("the client it is given has no way to place orders", not any(hasattr(k, a) for a in ("create_order", "cancel_order", "amend_order")))
    check("level read off live quotes: 'above X' is 50/50 at 3855", close(GW.level_from_quotes([(3850, .70), (3860, .30), (3840, .91)]), 3855.0))
    check("no level when every quote is on one side of 50c", GW.level_from_quotes([(1, .9), (2, .8)]) is None)
    r = gw.check("KXGOLDH", CLOSE, 30, CLOSE - 1800)
    picks = {p["ticker"]: p for p in wdb.query("SELECT * FROM gw_picks")}
    # cushions from 3855: 3830 +25 (YES), 3840 +15 (YES), 3850/3860 5 (skipped), 3870 -15 (NO), 3880 -25 (NO)
    check("contracts for other closes are ignored; the level is 3855", r["strikes"] == 6 and close(r["level"], 3855.0), r)
    check("only cushions of $10+ are bought, the favoured side each time",
          set(picks) == {"G-T3830", "G-T3840", "G-T3870", "G-T3880"} and picks["G-T3840"]["side"] == "yes" and picks["G-T3870"]["side"] == "no", sorted(picks))
    check("paper buys are at the REAL ask: YES ask 92c; the NO side's ask is 1 - YES bid (1 - 0.06 = 94c)",
          close(picks["G-T3840"]["ask"], .92) and close(picks["G-T3870"]["ask"], .94) and close(picks["G-T3840"]["gap"], 15.0), (picks["G-T3840"]["ask"], picks["G-T3870"]["ask"]))
    check("the last trade is kept for the side bought, to measure how far the ask sits above it",
          close(picks["G-T3840"]["last"], .91) and close(picks["G-T3870"]["last"], 1 - .07), (picks["G-T3840"]["last"], picks["G-T3870"]["last"]))
    gw.check("KXGOLDH", CLOSE, 30, CLOSE - 1790)
    check("a contract is recorded once per check time", len(wdb.query("SELECT 1 FROM gw_picks")) == 4)
    # due(): the 30-minute check runs once in its two-minute window
    check("checks fire at their minute before the top of the hour, once", gw.due(CLOSE - 1800 + 30) == [(CLOSE, 30)] and gw.due(CLOSE - 1800 - 30) == [])
    gw.done.add((CLOSE, 30))
    check("a check already run is not repeated", gw.due(CLOSE - 1800 + 60) == [])
    # settlement: gold finished at 3863 -> 3830 YES wins, 3840 YES wins, 3870 NO wins, 3880 NO wins
    k.results = {"G-T3830": "yes", "G-T3840": "yes", "G-T3870": "no", "G-T3880": "no"}
    check("nothing settles before the close", gw.settle(CLOSE - 60) == 0)
    n = gw.settle(CLOSE + 600)
    p = {x["ticker"]: x for x in wdb.query("SELECT * FROM gw_picks")}
    fee = lambda a: 0.07 * a * (1 - a)
    check("settled at the result less the ask and one taker fee", n == 4 and close(p["G-T3840"]["pnl"], 1 - .92 - fee(.92), 1e-5) and p["G-T3840"]["status"] == "settled", p["G-T3840"])
    k.results["G-T3870"] = "yes"
    wdb.execute("UPDATE gw_picks SET status='open', pnl=NULL WHERE ticker='G-T3870'")
    gw.settle(CLOSE + 700)
    check("a loss costs the whole ask and the fee", close(wdb.query("SELECT pnl FROM gw_picks WHERE ticker='G-T3870'")[0]["pnl"], -.94 - fee(.94), 1e-5))
    s = gw.summary()
    check("summary per cushion bucket, per check time, and the $15+ rule counted once per contract",
          s["by_bucket"]["$15-25"]["n"] == 2 and s["by_bucket"]["$25+"]["n"] == 2 and s["rule_15_first_touch"]["n"] == 4 and s["rule_15_first_touch"]["ask_over_last_c"] is not None, s)
    cfg["enabled"] = False
    k.calls.clear()
    gw.tick(CLOSE - 1200 + 10)
    check("switched off -> no market reads at all", k.calls == [])
    k2 = FakeKalshi()
    k2.book = [b for b in k2.book if b["ticker"] in ("G-T3830", "G-T3840")]
    gw2 = GW.GapWatch(lambda: {}, wdb, k2)
    r2 = gw2.check("KXGOLDH", CLOSE, 20, CLOSE - 1200)
    check("no level readable -> nothing bought, and the reason is logged", r2["picks"] == 0 and r2["level"] is None and "level" in r2["note"], r2)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
