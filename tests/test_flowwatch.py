"""Early flow (paper): following large trades in a new weather market's first hour. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import flowwatch as F  # noqa: E402
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


OPEN = dt.datetime(2026, 11, 21, 15, 0, tzinfo=dt.timezone.utc).timestamp()      # 10:00 a.m. Eastern
CLOSE = OPEN + 39 * 3600


class K:
    """Kalshi stand-in: two weather series; KXHIGHNY has one event of three markets opening at OPEN."""
    def __init__(self):
        self.calls = []
        self.q = {"A": (0.40, 0.42, 0), "B": (0.20, 0.25, 0), "C": (0.70, 0.72, 0)}     # yes bid, yes ask, volume
        self.trades = {}
        self.res = {}

    def series_list(self, category=None):
        assert category == "Climate and Weather"
        self.calls.append("series")
        return [{"ticker": "KXHIGHNY"}, {"ticker": "KXOLDSTORM"}]

    def _mk(self, c):
        b, a, v = self.q[c]
        return {"ticker": f"KXHIGHNY-26NOV22-{c}", "event_ticker": "KXHIGHNY-26NOV22", "open_time": iso(OPEN), "close_time": iso(CLOSE),
                "market_type": "binary", "yes_bid_dollars": str(b), "yes_ask_dollars": str(a), "volume_fp": str(v)}

    def paginate(self, path, key, params, max_items=None):
        self.calls.append((path, params.get("series_ticker") or params.get("ticker")))
        if path == "/markets":
            if params["series_ticker"] == "KXHIGHNY":
                yield from (self._mk(c) for c in self.q)
                yield {"ticker": "KXHIGHNY-26NOV22-SHORT", "event_ticker": "KXHIGHNY-26NOV22", "open_time": iso(OPEN), "close_time": iso(OPEN + 7200)}
            else:                                                      # a series whose last market closed weeks ago
                yield {"ticker": "KXOLDSTORM-1", "open_time": iso(OPEN - 40 * 86400), "close_time": iso(OPEN - 30 * 86400)}
        elif path == "/markets/trades":
            assert params["min_ts"] == int(OPEN)
            yield from self.trades.get(params["ticker"], [])

    def markets(self, status=None, event_ticker=None, tickers=None, max_items=None, **kw):
        if tickers:
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
        self.calls.append(("event", event_ticker))
        assert event_ticker == "KXHIGHNY-26NOV22"
        return [self._mk(c) for c in self.q]


def tr(i, ts, yp, n, side):
    return {"trade_id": f"t{i}", "created_time": iso(ts), "yes_price_dollars": str(yp), "count_fp": str(n), "taker_side": side}


def main():
    check("the cost of a side now: the YES ask, or 1 minus the YES bid for NO; nothing without a quote",
          close(F.side_ask({"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"}, "yes"), .42) and close(F.side_ask({"yes_bid_dollars": "0.40"}, "no"), .60)
          and F.side_ask({"yes_bid_dollars": "0.0000"}, "no") is None and F.side_ask({}, "yes") is None)
    k = K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    fw = F.FlowWatch(lambda: {}, wdb, k)
    r = fw.scan(OPEN - 3600)
    check("markets about to open are found ahead of time (one request per weather series); a 2-hour contract is not one of them",
          set(fw.upcoming) == {f"KXHIGHNY-26NOV22-{c}" for c in "ABC"} and r["watching"] == 0 and close(fw.state["next_open"], OPEN), (fw.upcoming, r))
    check("after the full walk only series still in use are kept", fw._series[1] == ["KXHIGHNY"] and fw.state["series"] == 1, fw._series)
    n0 = len(k.calls)
    fw.scan(OPEN - 3540)
    check("before the open nothing is polled", len(k.calls) == n0, k.calls[n0:])
    check("the weather series are re-listed every 5 minutes (rain markets are created only 10 minutes before they open)", F.DISCOVER_EVERY == 300)

    # 10:05: A has traded 80 contracts in small lots; B one trade of 150 YES at 24c; C one of 300 NO at 29c (YES price 71c)
    T = OPEN + 300
    k.q = {"A": (0.40, 0.42, 80), "B": (0.24, 0.25, 150), "C": (0.72, 0.74, 300)}
    k.trades = {"KXHIGHNY-26NOV22-B": [tr(1, T - 20, 0.24, 150, "yes"), tr(2, T - 25, 0.24, 99, "yes")],
                "KXHIGHNY-26NOV22-C": [tr(3, T - 30, 0.71, 300, "no")]}
    n0 = len(k.calls)
    r = fw.scan(T)
    sg = {s["ticker"][-1]: s for s in wdb.query("SELECT * FROM flow_signals")}
    pk = {p["ticker"][-1]: p for p in wdb.query("SELECT * FROM flow_picks")}
    check("one call for the event; trades are read only for markets whose volume grew by 100+ (not A)",
          [c for c in k.calls[n0:] if c[0] != "/markets"] == [("event", "KXHIGHNY-26NOV22"), ("/markets/trades", "KXHIGHNY-26NOV22-B"), ("/markets/trades", "KXHIGHNY-26NOV22-C")], k.calls[n0:])
    check("a signal is ONE trade of 100+ contracts (the 99 is not); its price is what the buyer paid (NO at a 71c YES price = 29c)",
          set(sg) == {"B", "C"} and sg["B"]["side"] == "yes" and close(sg["B"]["price"], .24) and sg["C"]["side"] == "no" and close(sg["C"]["price"], .29) and r["signals"] == 2, sg)
    # B: YES ask 25c <= 24c + 1c -> bought at 25c. C: NO costs 1 - 0.72 = 28c <= 29c + 1c -> bought at 28c.
    check("the same side is bought once on paper at the real ask when it is within 1c of the trade",
          set(pk) == {"B", "C"} and close(pk["B"]["price"], .25) and close(pk["C"]["price"], .28) and close(pk["B"]["fee"], .07 * .25 * .75)
          and pk["C"]["side"] == "no" and r["picks"] == 2, pk)

    # 10:20: A gets a 200-lot YES at 42c, but the ask has already moved to 45c; it stays there
    T2 = OPEN + 1200
    k.q["A"] = (0.44, 0.45, 280)
    k.trades["KXHIGHNY-26NOV22-A"] = [tr(4, T2 - 10, 0.42, 200, "yes")]
    fw.scan(T2)
    a = wdb.query("SELECT * FROM flow_signals WHERE ticker LIKE '%-A'")[0]
    check("a side that has run away (45c ask after a 42c trade) is not bought; the lowest ask seen is kept",
          a["status"] == "waiting" and close(a["best_ask"], .45) and len(wdb.query("SELECT * FROM flow_picks")) == 2, a)
    fw.scan(T2 + 300)
    k.q["A"] = (0.41, 0.42, 280)                                        # ...and it comes back only after the 10 minutes are up
    fw.scan(T2 + 700)
    a = wdb.query("SELECT * FROM flow_signals WHERE ticker LIKE '%-A'")[0]
    check("after 10 minutes the signal expires unbought, however cheap the side gets later", a["status"] == "expired" and len(wdb.query("SELECT * FROM flow_picks")) == 2, a)

    # a large trade after the first hour is not a signal; a second large trade on B does not buy B twice
    T3 = OPEN + 3700
    k.q["B"] = (0.24, 0.25, 600)
    k.trades["KXHIGHNY-26NOV22-B"] += [tr(5, OPEN + 3500, 0.25, 200, "yes"), tr(6, OPEN + 3650, 0.25, 250, "yes")]
    fw.scan(T3)
    check("signals come only from the first hour, and a market and side is bought once",
          len(wdb.query("SELECT * FROM flow_signals WHERE ticker LIKE '%-B'")) == 2 and len(wdb.query("SELECT * FROM flow_picks")) == 2)
    r = fw.scan(OPEN + 3600 + 600 + 60)
    check("70 minutes after the open the market is no longer watched", r["watching"] == 0)

    # a restart: the tracker starts mid-hour and first sees a trade that is already 20 minutes old
    k2 = K()
    k2.q = {"A": (0.40, 0.42, 500), "B": (0.20, 0.25, 0), "C": (0.70, 0.72, 0)}
    k2.trades = {"KXHIGHNY-26NOV22-A": [tr(9, OPEN + 600, 0.42, 500, "yes")]}
    db2 = WarehouseDB(Path(tempfile.mkdtemp()) / "r.db")
    f2 = F.FlowWatch(lambda: {}, db2, k2)
    f2.scan(OPEN + 1800)
    check("a large trade first seen after its 10 minutes (the app was restarted) is recorded as seen too late and never bought",
          db2.query("SELECT status FROM flow_signals")[0]["status"] == "late" and not db2.query("SELECT 1 FROM flow_picks") and f2.status()["signals"]["seen_too_late"] == 1)

    # settlement
    check("nothing settles before the close", fw.settle(CLOSE - 10) == 0)
    k.res = {"KXHIGHNY-26NOV22-B": "no", "KXHIGHNY-26NOV22-C": "no"}
    fw.settle(CLOSE + 600)
    pk = {p["ticker"][-1]: p for p in wdb.query("SELECT * FROM flow_picks")}
    check("settles on Kalshi's result: B's YES loses the 25c and fee, C's NO pays 72c less the fee",
          close(pk["B"]["pnl"], -.25 - .07 * .25 * .75, 1e-5) and close(pk["C"]["pnl"], 1 - .28 - .07 * .28 * .72, 1e-5), pk)
    st = fw.status()
    check("status: totals, followed / not buyable counts, the rule",
          st["totals"]["settled"] == 2 and st["totals"]["won"] == 1 and st["signals"]["bought"] == 2 and st["signals"]["expired"] == 1 and st["signals"]["already_held"] == 1
          and st["rule"] == {"min_contracts": 100, "first_minutes": 60, "wait_minutes": 10, "slip_c": 1}, (st["totals"], st["signals"]))
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["flow_weather"]
    check("the scoreboard scores it, one event per weather day", row["score"]["trades"] == 2 and row["score"]["wins"] == 1, row["score"])
    off = F.FlowWatch(lambda: {"flow_enabled": False}, WarehouseDB(Path(tempfile.mkdtemp()) / "o.db"), k)
    check("the Kalshi client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")) and off.opt("flow_enabled", True) is False)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
