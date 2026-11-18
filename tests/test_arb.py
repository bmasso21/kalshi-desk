"""Arbitrage scan: hand-checkable baskets and ladders, Kalshi's fee rounding, live order-book confirmation, settlement check."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import arb as A  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def mk(t, yb, ya, **kw):
    return {"ticker": t, "status": "active", "yes_bid_dollars": str(yb), "yes_ask_dollars": str(ya), **kw}


def main():
    check("fee: 7% x P x (1-P) per contract, rounded UP to the cent per order", A.fee(0.5, 1) == 0.02 and A.fee(0.5, 100) == 1.75 and A.fee(0.99, 1) == 0.01 and A.fee(1.0, 5) == 0)
    check("fee multiplier and fee-free series", A.fee(0.5, 100, 0.5) == 0.88 and A.fee(0.5, 100, 1, "zero") == 0)
    # two-team game, YES bids 0.58 + 0.47 = 1.05: NO A at 42c + NO B at 53c = 95c, pays $1 (one team wins) -> +5c less fees (2c + 2c)
    ev = {"event_ticker": "G1", "series_ticker": "S", "mutually_exclusive": True, "markets": [mk("G1-A", .58, .60), mk("G1-B", .47, .49)]}
    c = A.candidates(ev, {})
    nb = [x for x in c if x["kind"] == "no_basket"]
    check("NO basket found when YES bids add up above $1 + fees", len(nb) == 1 and [l["price"] for l in nb[0]["legs"]] == [0.42, 0.53] and nb[0]["payout_min"] == 1, c)
    ev2 = {**ev, "markets": [mk("G1-A", .52, .54), mk("G1-B", .47, .49)]}
    check("no basket when bids add to 99c (no free money)", not [x for x in A.candidates(ev2, {}) if x["kind"] == "no_basket"])
    ev3 = {**ev, "mutually_exclusive": False}
    check("nothing assumed for events Kalshi does not mark mutually exclusive", not [x for x in A.candidates(ev3, {}) if "basket" in x["kind"]])
    # three outcomes, one with no YES bid: it is left out; the other two still pay at least 1 of 2
    ev4 = {"event_ticker": "E", "series_ticker": "S", "mutually_exclusive": True, "markets": [mk("E-1", .60, .62), mk("E-2", .48, .50), mk("E-3", 0, .02)]}
    nb4 = [x for x in A.candidates(ev4, {}) if x["kind"] == "no_basket"]
    check("an outcome with no YES bid is left out of the NO basket (the rest still pay k - 1)", nb4 and len(nb4[0]["legs"]) == 2 and nb4[0]["payout_min"] == 1, nb4)
    # YES basket: asks 30 + 30 + 30 = 90c -> candidate, never 'locked'
    ev5 = {"event_ticker": "Y", "series_ticker": "S", "mutually_exclusive": True, "markets": [mk("Y-1", .28, .30), mk("Y-2", .28, .30), mk("Y-3", .28, .30)]}
    yb = [x for x in A.candidates(ev5, {}) if x["kind"] == "yes_basket"]
    check("YES basket under $1 is a candidate that is NOT treated as locked (needs one outcome to be certain)", yb and yb[0]["locked"] is False and yb[0]["payout_min"] == 0, yb)
    # ladder: above 100 asks 40c, above 110 bids 45c -> buy YES 100 @40 + NO 110 @55 = 95c, every outcome pays >= $1
    lad = {"event_ticker": "L", "series_ticker": "S", "mutually_exclusive": False, "markets": [
        mk("L-100", .38, .40, strike_type="greater", floor_strike=100), mk("L-110", .45, .47, strike_type="greater", floor_strike=110),
        mk("L-120", .10, .12, strike_type="greater", floor_strike=120)]}
    for m in lad["markets"]:
        m["ticker"] = "L-X-" + m["ticker"].split("-")[1]
    lc = [x for x in A.candidates(lad, {}) if x["kind"] == "ladder"]
    ppg = {"event_ticker": "P", "series_ticker": "S", "mutually_exclusive": False, "markets": [
        mk("P-GREEN-20", .15, .17, strike_type="greater", floor_strike=20.3), mk("P-IRVING-24", .30, .32, strike_type="greater", floor_strike=24.0)]}
    check("different underlyings in one event (each player's average) are never treated as a ladder", not A.candidates(ppg, {}))
    check("ladder: a higher strike priced above a lower one -> YES low + NO high", len(lc) == 1 and lc[0]["legs"][0] == {"ticker": "L-X-100", "side": "yes", "price": 0.40}
          and lc[0]["legs"][1]["ticker"] == "L-X-110" and close(lc[0]["legs"][1]["price"], 0.55), lc)
    # confirm at the order book: sizes cap the sets; a vanished price kills it
    books = {"L-X-100": {"yes_asks": [(0.40, 30)], "no_asks": [(0.62, 5)]}, "L-X-110": {"yes_asks": [(0.47, 9)], "no_asks": [(0.55, 12)]}}
    ok = A.confirm(lc[0], books)
    per = 1 - 0.95 - 0.02 - 0.02
    tot = 12 * 0.05 - (A.fee(0.40, 12) + A.fee(0.55, 12))
    check("confirmed at live top of book: sets = smallest size, fees per order rounded up", ok and ok["sets"] == 12 and close(ok["profit_per_set"], round(per, 4)) and close(ok["profit_total"], round(tot, 2)), ok)
    books["L-X-110"]["no_asks"] = [(0.61, 12)]
    check("if the live price no longer gives a profit, nothing is recorded", A.confirm(lc[0], books) is None)

    # ---- the scanner end to end ----
    class K:
        def __init__(self):
            self.results = {}

        def paginate(self, path, key, params, max_items=None):
            if path == "/series":
                yield {"ticker": "S", "fee_type": "quadratic", "fee_multiplier": 1}
            else:
                assert params["status"] == "open" and params["with_nested_markets"] == "true"
                yield from [ev, lad]

        def orderbooks(self, tickers):
            ob = {"G1-A": {"yes_dollars": [["0.58", "20"]], "no_dollars": [["0.40", "20"]]}, "G1-B": {"yes_dollars": [["0.47", "15"]], "no_dollars": [["0.51", "15"]]},
                  "L-X-100": {"yes_dollars": [["0.38", "5"]], "no_dollars": [["0.60", "30"]]}, "L-X-110": {"yes_dollars": [["0.45", "12"]], "no_dollars": [["0.53", "9"]]}}
            return {t: ob[t] for t in tickers}

        def markets(self, status=None, tickers=None, max_items=None, **kw):
            return [{"ticker": t, "result": self.results.get(t, "")} for t in tickers]
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    k = K()
    sc = A.ArbScanner(lambda: {"arb_pause_sec": 0}, wdb, k)
    r = sc.scan()
    opps = {o["kind"]: o for o in wdb.query("SELECT * FROM arb_opps")}
    check("scan: both opportunities confirmed at the books and recorded with sizes", r["confirmed"] == 2 and set(opps) == {"no_basket", "ladder"}
          and opps["no_basket"]["sets"] == 15 and opps["ladder"]["sets"] == 12 and r["error"] is None, (r, list(opps)))
    check("the scanner never touches order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order", "amend_order")))
    sc.scan()
    check("seen again -> counted, not duplicated", wdb.query("SELECT COUNT(*) n, MAX(seen) s FROM arb_opps")[0] == {"n": 2, "s": 2})
    k.results = {"G1-A": "yes", "G1-B": "no", "L-X-100": "yes", "L-X-110": "no"}
    sc.settle()
    st = {o["kind"]: o for o in wdb.query("SELECT * FROM arb_opps")}
    nb_cost = st["no_basket"]["cost"]
    check("after settlement the REAL payout is checked: NO basket paid $1 (B lost); ladder paid $2 (finished between the strikes: both legs win)",
          st["no_basket"]["status"] == "settled" and st["no_basket"]["payout_real"] == 1.0 and close(st["no_basket"]["pnl_real"], round(1 - nb_cost - sum(A.fee(l["price"], 1) for l in json.loads(st["no_basket"]["legs"])), 4))
          and st["ladder"]["payout_real"] == 2.0, st)
    b = SB.board(wdb)
    arbrow = {x["key"]: x for x in b["strategies"]}["arb_locked"]
    check("settled arbitrage shows on the scoreboard like any strategy", arbrow["score"]["trades"] == 2 and arbrow["score"]["pnl"] > 0, arbrow["score"])
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
