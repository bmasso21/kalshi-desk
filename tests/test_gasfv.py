"""AAA gas fair value: what AAA had already published vs the prices takers paid. Hand-checkable cases."""
import datetime as dt
import math
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gasfv as GS  # noqa: E402
from app import studies as ST  # noqa: E402
from app.goldfv import phi  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def et(y, m, d, hh=0, mm=0, off=5):
    """An Eastern wall-clock moment as a timestamp (off = hours behind UTC: 5 in winter, 4 in summer)."""
    return (dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc) + dt.timedelta(hours=off)).timestamp()


def main():
    D = dt.date
    # a contract for Nov 18's figure closes at 11:59 p.m. ET on Nov 17
    c18 = et(2026, 11, 17, 23, 59)
    check("a contract closing 11:59 p.m. ET settles on the NEXT day's AAA figure", GS.value_date(c18) == D(2026, 11, 18))
    check("a day's figure is public from 8 a.m. ET; before that only yesterday's",
          GS.known_date(et(2026, 11, 17, 9)) == D(2026, 11, 17) and GS.known_date(et(2026, 11, 17, 7, 59)) == D(2026, 11, 16))

    check("Eastern time by rule, no time-zone database: summer is UTC-4, winter UTC-5, switching at 2 a.m. on the right Sundays",
          GS.et(et(2026, 7, 4, 12, off=4)).hour == 12 and GS.et(et(2026, 1, 15, 12)).hour == 12
          and GS.et(et(2026, 3, 8, 1, 59)).hour == 1 and GS.et(et(2026, 3, 8, 3, 0, off=4)).hour == 3
          and GS.et(et(2026, 11, 1, 1, 30, off=4)).hour == 1 and GS.et(et(2026, 11, 1, 3)).hour == 3
          and GS.value_date(et(2026, 7, 3, 23, 59, off=4)) == D(2026, 7, 4))
    # 30 days rising exactly 1c a day: Oct 19 = 3.50 ... Nov 17 = 3.79
    days = [D(2026, 10, 19) + dt.timedelta(days=i) for i in range(30)]
    g = GS.Gas({d: 3.50 + 0.01 * i for i, d in enumerate(days)})
    L = D(2026, 11, 17)
    mu, sd = g.forecast(L, 1, True)
    check("trend model: latest + days x average of the last 3 daily changes; a perfect streak leaves only the floor as doubt",
          close(mu, 3.80) and close(sd, GS.SIGMA_FLOOR), (mu, sd))
    mu0, sd0 = g.forecast(L, 2, False)
    check("no-trend model: expects the latest figure; doubt = how far the figure has moved in that many days (2c in 2 days)",
          close(mu0, 3.79) and close(sd0, 0.02), (mu0, sd0))
    gap = GS.Gas({d: v for d, v in g.v.items() if d != D(2026, 11, 16)})
    check("a missing day inside the last 3 changes -> no trend forecast (no guess)", gap.forecast(L, 1, True) == (None, None))
    check("the newest figure may be up to 3 days old, no older", g.latest(D(2026, 11, 20))[0] == L and g.latest(D(2026, 11, 21))[0] is None)
    check("too little history -> no forecast", GS.Gas({d: 3.5 for d in days[:4]}).forecast(days[3], 1, False) == (None, None))

    # ---- the replay ----
    # zigzag history: +1c, -1c ... so no-trend 1-day doubt is exactly 1c. Latest (Nov 17) = 3.80
    zz = GS.Gas({d: 3.80 + (0.01 if i % 2 else 0.0) - (0.01 if (len(days) - 1) % 2 else 0.0) for i, d in enumerate(days)})
    v17 = zz.v[L]
    t = et(2026, 11, 17, 12)                                        # noon Nov 17: Nov 17's figure is public, 1 day left
    p = phi((v17 - 3.79) / 0.01)                                    # strike 1c below the latest: ~84%
    markets = [{"ticker": "G-379", "close": c18, "strike": 3.79, "result": "yes"},
               {"ticker": "G-381", "close": c18, "strike": 3.81, "result": "no"},
               {"ticker": "G-OLD", "close": et(2026, 10, 20, 23, 59), "strike": 3.5, "result": "yes"}]
    trades = {"G-379": [(t, 0.60, "yes"),                           # paid 60c for ~84%: big edge
                        (t + 60, 0.95, "yes"),                      # paid 95c: negative edge
                        (c18 + 5, 0.50, "yes")],                    # after the close: ignored
              "G-381": [(t, 0.30, "no")],                           # NO at 70c when NO is ~84%: edge ~ +12c
              "G-OLD": [(et(2026, 10, 20, 12), 0.5, "yes")]}        # no history yet -> skipped
    r = GS.core(markets, trades, zz, ST._Acc)
    nt = r["no_trend"]
    fee = 0.07 * 0.6 * 0.4
    e10 = next(x for x in nt["by_edge"] if x["edge"] == "10c+")
    check("no-trend replay: 3 usable trades, the old one skipped (no AAA history before it)", nt["trades"] == 3 and r["skipped_no_aaa_figure"] >= 1, (nt["trades"], r["skipped_no_aaa_figure"]))
    check("edge = model chance - price paid - fee; the 60c YES and the 70c NO both land in 10c+ and both won",
          close(p, phi(1.0)) and e10["all"]["entries"] == 2 and e10["all"]["win_pct"] == 100.0, e10)
    s2 = next(x for x in nt["strategy_first_trade"] if x["min_edge_c"] == 2)
    check("the rule takes the FIRST qualifying trade per contract and side, return = payout - price - fee",
          s2["all"]["entries"] == 2 and close(s2["all"]["ret_c"], 100 * ((1 - 0.6 - fee) + (1 - 0.7 - 0.07 * 0.7 * 0.3)) / 2, 0.01), s2)
    check("both contracts settle on the same day -> one event for the statistics", s2["all"]["events"] == 1, s2["all"])
    check("AAA summary: typical daily move 1c", close(r["aaa"]["typical_daily_move_c"], 1.0, 1e-6) and r["aaa"]["days"] == 30, r["aaa"])
    check("too little AAA history -> a note, not a guess", GS.core(markets, trades, GS.Gas({L: 3.8}), ST._Acc)["trades"] == 0)

    # ---- through the warehouse ----
    tmp = tempfile.mkdtemp()
    wdb = WarehouseDB(Path(tmp) / "w.db")
    for mk in markets[:2]:
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete, strike_type, floor_strike) "
                    "VALUES(?,?, 'KXAAAGASD', 'Economics', 'finalized', ?, ?, ?, 10, 1, 'greater', ?)", (mk["ticker"], "EV", mk["close"] - 50000, mk["close"], mk["result"], mk["strike"]))
        for i, (ts, yp, side) in enumerate(trades[mk["ticker"]]):
            wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"{mk['ticker']}{i}", mk["ticker"], ts, yp, 1, side, "bid" if side == "yes" else "ask"))

    class FakeClient:
        def __init__(self):
            self.series = []

        def paginate(self, path, key, params, max_items=None):
            assert path == "/markets" and params["status"] == "settled"
            self.series.append(params["series_ticker"])
            if params["series_ticker"] != "KXAAAGASD":
                return
            for d, v in zz.v.items():                                # each day's figure, on the contract that closed the evening before
                c = et(d.year, d.month, d.day, off=4 if d < dt.date(2026, 11, 1) else 5) - 60
                yield {"close_time": dt.datetime.fromtimestamp(c, dt.timezone.utc).isoformat(), "expiration_value": str(v)}
    conn = sqlite3.connect(str(Path(tmp) / "w.db"))
    conn.row_factory = sqlite3.Row
    fc = FakeClient()
    r2 = ST.gasfv(conn, {}, client=fc)
    check("study: stored trades + AAA history rebuilt from Kalshi's settlement values of all three gas series = the core's answer",
          r2["no_trend"]["trades"] == 3 and sorted(fc.series) == sorted(ST.GAS_SERIES) and r2["aaa"]["days"] == 30, (r2.get("note"), r2.get("aaa"), fc.series))
    check("gasfv is a named study and only runs when asked for", "gasfv" in ST.RUN and "gasfv" in ST.EXTRA and "gasfv" not in ST.KINDS)
    check("the client it is given only reads markets", not any(hasattr(FakeClient, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
