"""Fed decisions vs fed funds futures (FedWatch method): worked by hand."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import fedwatch as F  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def main():
    check("futures symbols: Dec 2026 = ZQZ26.CBT, Jan 2027 = ZQF27.CBT", F.futures_symbol(2026, 12) == "ZQZ26.CBT" and F.futures_symbol(2027, 1) == "ZQF27.CBT")
    s = F.split_steps(0.27)
    check("+27 bp expected -> 92% one hike, 8% two", close(s[1], 0.92) and close(s[2], 0.08), s)
    o = F.to_outcomes(F.split_steps(-0.05))
    check("-5 bp expected -> 20% cut 25, 80% hold", close(o["C25"], 0.2) and close(o["H0"], 0.8) and close(sum(o.values()), 1.0), o)
    # Dec 9 meeting, 31-day month: avg 4.07 with r0 3.88 for 9 days -> r1 = (31*4.07 - 9*3.88)/22
    r1 = (31 * 4.07 - 9 * 3.88) / 22
    m = F.implied([dt.date(2026, 12, 9)], {(2026, 12): 4.07}, 3.88)
    check("meeting rate from the month's average: r1 = (N*avg - d*r0)/(N-d)", close(m[0]["r1"], round(r1, 4), 1e-4) and close(m[0]["delta_bp"], round(100 * (r1 - 3.88), 1), 0.05), m)
    m2 = F.implied([dt.date(2027, 1, 27), dt.date(2027, 3, 17)], {(2027, 1): 4.10, (2027, 2): 4.33, (2027, 3): 4.41}, 4.15)
    check("a meeting late in the month (4 days after) uses next month's average; the next meeting starts from it",
          close(m2[0]["r1"], 4.33) and close(m2[1]["r0"], 4.33) and close(m2[1]["r1"], (31 * 4.41 - 17 * 4.33) / 14, 1e-4), m2)
    check("a meeting with no futures price stops the chain", F.implied([dt.date(2030, 5, 5)], {}, 4.0) == [])

    # ---- the scanner ----
    class Resp:
        def __init__(self, j, code=200):
            self.j, self.status_code = j, code

        def raise_for_status(self):
            pass

        def json(self):
            return self.j

    class Ses:
        def get(self, url, params=None, headers=None, timeout=None):
            if "newyorkfed" in url:
                return Resp({"refRates": [{"percentRate": 3.88}]})
            sym = url.rsplit("/", 1)[-1]
            px = {"ZQZ26.CBT": 95.93, "ZQF27.CBT": 95.83}.get(sym)
            return Resp({"chart": {"result": [{"meta": {"regularMarketPrice": px}}] if px else []}})

    def mk(t, yb, ya):
        return {"ticker": t, "yes_bid_dollars": str(yb), "yes_ask_dollars": str(ya), "close_time": "2026-12-09T18:59:00Z"}

    class K:
        def __init__(self):
            self.res = {}

        def paginate(self, path, key, params, max_items=None):
            assert params["series_ticker"] == "KXFEDDECISION"
            yield {"event_ticker": "KXFEDDECISION-26DEC", "markets": [mk("FD-C25", 0, .01), mk("FD-H0", .03, .04), mk("FD-H25", .80, .82), mk("FD-H26", .05, .06)]}

        def markets(self, status=None, tickers=None, max_items=None):
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    k = K()
    fw = F.FedWatch(lambda: {}, wdb, k, session=Ses())
    r = fw.scan()
    b = {x["outcome"]: x for x in wdb.query("SELECT * FROM fed_board")}
    p25 = F.to_outcomes(F.split_steps(r1 - 3.88))["H25"]
    check("board: every outcome priced from the futures (Dec avg 4.07, effective 3.88)", r["meetings"] == 1 and close(b["H25"]["fair"], round(p25, 4), 1e-4), (r, b.get("H25")))
    picks = {(x["outcome"], x["side"]): x for x in wdb.query("SELECT * FROM fed_picks")}
    # futures ~93% on +25 vs Kalshi ask 82c -> YES H25 edge ~ 0.93 - 0.82 - fee > 3c
    check("a 3c+ edge after fees becomes a paper pick at the real ask", ("H25", "yes") in picks and close(picks[("H25", "yes")]["price"], 0.82), sorted(picks))
    check("prices under 3c are never picked", all(x["price"] >= 0.03 for x in picks.values()))
    fw.scan()
    check("a pick is recorded once", len(wdb.query("SELECT * FROM fed_picks")) == len(picks))
    k.res = {"FD-H25": "yes", "FD-H0": "no", "FD-H26": "no", "FD-C25": "no"}
    fw.settle()
    p = wdb.query("SELECT * FROM fed_picks WHERE outcome='H25' AND side='yes'")[0]
    check("settles on Kalshi's result less the ask and fee", p["status"] == "settled" and close(p["pnl"], 1 - 0.82 - 0.07 * 0.82 * 0.18, 1e-5), p)
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["fed"]
    check("Fed picks appear on the scoreboard", row["score"]["trades"] >= 1, row["score"])
    check("the client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    class Flaky:
        def __init__(self, fails):
            self.fails, self.n = fails, 0

        def get(self, url, **kw):
            self.n += 1
            if self.n <= self.fails:
                raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

            class R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"refRates": [{"percentRate": 3.88}]}
            return R()
    fl = F.FedWatch(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "fl.db"), None, session=Flaky(1))
    check("a dropped idle connection is retried once (the half-hourly check no longer fails on 'connection reset')", close(fl.effr(), 3.88) and fl.http.n == 2)
    fl2 = F.FedWatch(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "fl2.db"), None, session=Flaky(2))
    try:
        fl2.effr()
        twice = False
    except ConnectionError:
        twice = True
    check("a connection that fails twice is still an error (no endless retry)", twice and fl2.http.n == 2)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
