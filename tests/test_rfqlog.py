"""Combo price requests (RFQs): read-only production log. Hand-checkable."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import rfqlog as R  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


NOW = 1_795_500_000.0


def iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def rfq(rid, legs, contracts=100, cost=20.0, status="open", ts=NOW - 30):
    return {"id": rid, "creator_id": "x", "contracts_fp": f"{contracts:.2f}", "market_ticker": f"KXMVE-{rid}", "status": status,
            "created_ts": iso(ts), "target_cost_dollars": f"{cost:.4f}", "mve_collection_ticker": "KXMVENBASINGLEGAME",
            "mve_selected_legs": [{"event_ticker": t.rsplit("-", 1)[0], "market_ticker": t, "side": s} for t, s in legs]}


class Reader:
    def __init__(self):
        self.rows = []
        self.calls = []

    def paginate(self, path, key, params, max_items=None):
        self.calls.append((path, dict(params)))
        assert path == "/communications/rfqs" and key == "rfqs"
        yield from self.rows


class Pub:
    def __init__(self):
        self.q = {"A-1": ("0.60", "0.62"), "B-1": ("0.30", "0.34"), "C-1": ("0.10", "0.12")}
        self.res = {}

    def markets(self, status=None, tickers=None, max_items=None, **kw):
        return [{"ticker": t, "yes_bid_dollars": self.q.get(t, (None, None))[0], "yes_ask_dollars": self.q.get(t, (None, None))[1],
                 "result": self.res.get(t, "")} for t in tickers]


def main():
    # the production reader can only GET the RFQ list
    rd = R.RfqReader("https://api.example.test/trade-api/v2", "kid", "")
    bad = 0
    for m, p in (("POST", "/communications/quotes"), ("GET", "/portfolio/balance"), ("DELETE", "/communications/rfqs/abc"),
                 ("POST", "/communications/rfqs"), ("GET", "/portfolio/orders"), ("PUT", "/communications/quotes/x/confirm")):
        try:
            rd.request(m, p)
        except PermissionError:
            bad += 1
    check("the production reader refuses everything but GET of the RFQ list (no quotes, no orders, no account reads)", bad == 6)

    check("legs price: product of each chosen side's mid (A YES 61c x B NO 68c)", close(R.legs_price(
        [{"market_ticker": "A-1", "side": "yes"}, {"market_ticker": "B-1", "side": "no"}], {"A-1": {"yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62"},
                                                                                          "B-1": {"yes_bid_dollars": "0.30", "yes_ask_dollars": "0.34"}}), 0.61 * 0.68))
    check("outcome: won only when every leg lands on its side; lost as soon as one misses; undecided otherwise",
          R.outcome([{"market_ticker": "A", "side": "yes"}, {"market_ticker": "B", "side": "no"}], {"A": "yes", "B": "no"}) == "won"
          and R.outcome([{"market_ticker": "A", "side": "yes"}, {"market_ticker": "B", "side": "no"}], {"A": "no"}) == "lost"
          and R.outcome([{"market_ticker": "A", "side": "yes"}, {"market_ticker": "B", "side": "no"}], {"A": "yes"}) is None)

    rd2, pub = Reader(), Pub()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    lg = R.RfqLog(lambda: {}, wdb, pub, reader_factory=lambda: rd2)
    rd2.rows = [rfq("r1", [("A-1", "yes"), ("B-1", "no")], 100, 40.0), rfq("r2", [("A-1", "yes"), ("C-1", "yes")], 50, 4.0),
                rfq("r3", [("A-1", "yes"), ("Z-9", "yes")], 10, 1.0)]
    r = lg.scan(NOW)
    rows = {x["id"]: x for x in wdb.query("SELECT * FROM rfq_log")}
    check("new requests are stored with size, asked cost, legs and the legs' price when seen (a leg with no quote -> no price)",
          r["new"] == 3 and close(rows["r1"]["legs_mid"], 0.61 * 0.68) and close(rows["r2"]["legs_mid"], 0.61 * 0.11)
          and rows["r3"]["legs_mid"] is None and rows["r1"]["n_legs"] == 2 and close(rows["r1"]["target_cost"], 40.0), rows)
    rd2.rows[0]["status"] = "closed"
    rd2.rows[0]["cancelled_ts"] = iso(NOW + 30)
    r = lg.scan(NOW + 60)
    check("a request seen again is not stored twice; its closing is recorded", r["new"] == 0 and r["closed"] == 1
          and wdb.query("SELECT status FROM rfq_log WHERE id='r1'")[0]["status"] == "closed")
    check("nothing is decided within the hour or before the legs settle", lg.settle(NOW + 120) == 0)
    pub.res = {"A-1": "yes", "B-1": "no", "C-1": "no"}
    n = lg.settle(NOW + 7200)
    o = {x["id"]: x["outcome"] for x in wdb.query("SELECT id, outcome FROM rfq_log")}
    check("decided from the legs' results: r1 won, r2 lost, r3 waits for its last leg", n == 2 and o == {"r1": "won", "r2": "lost", "r3": None}, o)
    st = lg.status()
    rp, lp = st["requested_price"], st["legs_price"]
    # requester price per contract: r1 40c (won: +60c), r2 8c (lost: -8c) -> mean +26c
    check("did requesters overpay: per contract, their asked price and the legs' price against what happened",
          rp["n"] == 2 and close(rp["avg_price_c"], 24.0, 0.01) and close(rp["requester_edge_c"], 26.0, 0.01) and rp["hit_pct"] == 50.0
          and lp["n"] == 2 and close(lp["requester_edge_c"], 100 * ((1 - 0.61 * 0.68) + (0 - 0.61 * 0.11)) / 2, 0.01), (rp, lp))
    check("totals and most requested collections", st["totals"]["requests"] == 3 and st["totals"]["combos"] == 3
          and st["by_collection"][0]["n"] == 3 and st["totals"]["median_contracts"] == 50.0, st["totals"])
    nokey = R.RfqLog(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "n.db"), pub)
    try:
        nokey.scan(NOW)
        err = None
    except RuntimeError as e:
        err = str(e)
    check("without a production key it says so and reads nothing", err and "production API key" in err)
    check("the public client it is given has no order methods", not any(hasattr(pub, a) for a in ("create_order", "cancel_order", "create_quote")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
