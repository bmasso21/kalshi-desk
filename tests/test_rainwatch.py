"""Rain observed (paper): YES after the official station reports rain today. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import rainwatch as RW  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


DS = dt.datetime(2026, 11, 25, 5, tzinfo=dt.timezone.utc).timestamp()           # midnight EST, Nov 25
NOW = DS + 15 * 3600                                                              # 3 pm Eastern
H = lambda h, m=51: DS + h * 3600 + m * 60


def rule(cli):
    return f"If the total precipitation at CLI{cli} in Somewhere in Nov 25, 2026 is strictly greater than 0 inches, then the market resolves to Yes."


class K:
    def __init__(self):
        self.m = {"NYC": ("0.90", "300", "active"), "PHL": ("0.99", "500", "active"), "BOS": ("0.40", "500", "active"), "ATL": ("0.80", "20", "active")}
        self.res = {}
        self.calls = []

    def markets(self, status=None, event_ticker=None, tickers=None, max_items=None, **kw):
        if tickers:
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
        self.calls.append(event_ticker)
        assert event_ticker == "KXRAIN-26NOV25", event_ticker
        return [{"ticker": f"KXRAIN-26NOV25-{c}", "event_ticker": "KXRAIN-26NOV25", "rules_primary": rule(c), "strike_type": "greater",
                 "floor_strike": 0, "yes_ask_dollars": a, "yes_ask_size_fp": sz, "status": st, "close_time": "2026-11-26T05:00:00Z"}
                for c, (a, sz, st) in self.m.items()]


def main():
    obs = {"NYC": [(H(0), 0.05), (H(9), 0.0), (H(10), 0.03)],      # 0.05 in the first hour (ignored); 0.03 at 10:51 am: rain
           "PHL": [(H(8), 0.01)],                                    # rain, but YES already 99c
           "BOS": [(H(11), 0.00)],                                   # dry so far: nothing, however cheap YES is
           "ATL": [(H(7), 0.02)]}                                    # rain; only 20 contracts offered at 80c
    calls = []

    def fetch(aids, a, b):
        calls.append(len(aids))
        return obs
    k = K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    rw = RW.RainWatch(lambda: {}, wdb, k, fetch=fetch)
    r = rw.scan(NOW)
    pk = {p["cli"]: p for p in wdb.query("SELECT * FROM rain_picks")}
    days = {d["cli"]: d for d in wdb.query("SELECT * FROM rain_days")}
    check("one archive request covers every station the rain markets name", calls == [len({a for a, _ in RW.RAIN_STATIONS.values()})], calls)
    check("rain in the first hour of the day is ignored; a later 0.03 in counts (NYC bought at 90c, 100 contracts, fee rounded up)",
          close(pk["NYC"]["price"], 0.90) and pk["NYC"]["size"] == 100 and close(pk["NYC"]["fee"], RW.order_fee(0.90, 100))
          and close(pk["NYC"]["first_rain_ts"], H(10)), pk.get("NYC"))
    check("YES already at 99c is recorded as already priced, not bought; a dry city is not touched however cheap",
          "PHL" not in pk and days["PHL"]["status"] == "already_priced" and "BOS" not in pk and "BOS" not in days, days)
    check("fewer than 100 offered: buys only what is offered (20 at 80c)", pk["ATL"]["size"] == 20 and close(pk["ATL"]["price"], 0.80), pk.get("ATL"))
    check("one Kalshi call for the day's rain event; markets matched by the station in their rules", k.calls == ["KXRAIN-26NOV25"] and r == {"rainy": 3, "picks": 2}, (k.calls, r))
    rw.scan(NOW + 600)
    check("a market is bought once", len(wdb.query("SELECT * FROM rain_picks")) == 2)
    check("nothing settles before the close", rw.settle(NOW + 3600) == 0)
    k.res = {"KXRAIN-26NOV25-NYC": "yes", "KXRAIN-26NOV25-ATL": "yes"}
    rw.settle(dt.datetime(2026, 11, 26, 6, tzinfo=dt.timezone.utc).timestamp())
    pk = {p["cli"]: p for p in wdb.query("SELECT * FROM rain_picks")}
    check("settles on Kalshi's result: 100 x (1 - 0.90) - fee", pk["NYC"]["status"] == "settled" and close(pk["NYC"]["pnl"], 10 - RW.order_fee(0.90, 100), 1e-6), pk["NYC"])
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["rain_observed"]
    check("the scoreboard scores it per contract", row["score"]["trades"] == 2 and row["score"]["wins"] == 2, row["score"])
    check("the Kalshi client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
