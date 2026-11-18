"""Hourly gold fair value: model price from gold itself vs prices Kalshi takers actually paid. Hand-checkable cases."""
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import goldfv as G  # noqa: E402
from app import studies as ST  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


H0 = 1_790_006_400.0                 # a top-of-the-hour moment


def zigzag_bars(start, n, base, step=1.0):
    """Futures alternating base+step / base-step every minute: one-minute changes of exactly +-2*step -> sigma = 2*step."""
    return {int(start + 60 * i): base + (step if i % 2 else -step) for i in range(n)}


def main():
    # ---- the pieces ----
    g = G.Gold({0: 100.0, 60: 101.0, 120: 102.0})
    check("gold is read one full minute late: at t=150 the last COMPLETED bar is the one starting at 60 (closed at 120)", g.at(150) == 101.0 and g.at(180) == 102.0)
    check("stale gold (no bar for over 3 minutes) is not used", g.at(180 + 200) is None)
    gz = G.Gold(zigzag_bars(0, 200, 3800.0, 1.0))
    check("sigma = std of one-minute changes over the window before the moment (zigzag +-2 -> 2)", close(gz.sigma(200 * 60), 2.0, 1e-9))
    offs = [(1000.0, 2.5), (2000.0, 2.7), (3000.0, 2.6), (4000.0, 9.9)]
    check("the futures->Pyth offset uses only settlements BEFORE the moment (median), none of this hour's",
          close(G.offset_at(offs, 3500), 2.6) and G.offset_at(offs, 2500) is None and G.offset_at(offs, 3000.0) is None)
    check("normal probability", close(G.phi(0), 0.5) and close(G.phi(1.96), 0.975, 1e-3))

    # ---- the whole replay ----
    # 6 hours of zigzag futures around 3802.6 (Pyth = futures - 2.6 = 3800 on average), sigma = 2 per sqrt(minute)
    n = 8 * 60
    start = H0 - 2 * 3600
    bars = zigzag_bars(start, n, 3802.6, 1.0)
    settles = {H0 + 3600 * k: bars[int(H0 + 3600 * k - 60)] - 2.6 for k in range(0, 6)}      # Pyth settled at futures - 2.6
    # hour 4 (close H0+4h): gold at the moment is 3800 +-1. Strike 3790: with 30 min left, z = (S-3790)/(2*sqrt(30)) ~ 0.91 -> ~82%
    c4 = H0 + 4 * 3600
    t30 = c4 - 1800
    S = bars[int(t30) - 120] - 2.6                                   # the bar completed at t30 - 60 started at t30 - 120
    p = G.phi((S - 3790) / (2.0 * math.sqrt(30)))
    markets = [{"ticker": "G-3790", "close": c4, "strike": 3790.0, "result": "yes"},
               {"ticker": "G-3810", "close": c4, "strike": 3810.0, "result": "no"},
               {"ticker": "G-EARLY", "close": H0, "strike": 3800.0, "result": "yes"}]
    trades = {"G-3790": [(t30, 0.60, "yes"),                    # paid 60c for an ~82% chance: a big positive edge
                         (t30 + 60, 0.95, "yes"),               # paid 95c: negative edge
                         (c4 - 10, 0.99, "yes")],               # under 30 seconds left: ignored
              "G-3810": [(t30, 0.30, "no")],                    # bought NO at 1 - 0.30 = 70c
              "G-EARLY": [(H0 - 600, 0.5, "yes")]}              # before any settlement -> no offset yet -> skipped
    r = G.core(markets, trades, G.Gold(bars), settles, ST._Acc)
    check("trades priced only when gold, sigma and an earlier offset exist; under 30 seconds left is ignored",
          r["trades"] == 3 and r["skipped_no_gold_data"] == 1, (r.get("trades"), r.get("skipped_no_gold_data")))
    check("the offset is learned from Kalshi's settlements (2.6) and reproduces Pyth exactly here", close(r["offset"]["median"], 2.6, 1e-6) and close(r["offset"]["tracking_error_usd"], 0.0, 1e-6), r["offset"])
    eb = {x["edge"]: x["all"] for x in r["by_edge"]}
    fee = lambda x: 0.07 * x * (1 - x)
    e1 = p - 0.60 - fee(0.60)
    check("a 60c buy of an ~82% chance lands in the 10c+ edge bucket and returns 1 - 0.60 - fee", e1 > 0.10 and eb["10c+"]["entries"] == 1
          and close(eb["10c+"]["ret_c"], round(100 * (1 - .60 - fee(.60)), 3), 0.002), (round(p, 3), eb.get("10c+")))
    s = {x["min_edge_c"]: x["all"] for x in r["strategy_first_trade"]}
    # 3810 NO at 70c: model ~82% -> edge ~10.5c less a hair: clears 2c and 5c; the 95c buy on 3790 is never a second entry
    check("the strategy counts the FIRST qualifying trade per contract and side only", s[2]["entries"] == 2 and s[5]["entries"] == 2 and s[10]["entries"] <= 2
          and close(s[2]["avg_cost_c"], 65.0, 0.01), s)
    check("calibration table compares the model's chance with what happened", sum(x["n"] for x in r["calibration"]) == 3 and all("actual_yes_pct" in x for x in r["calibration"]))
    check("too few settlements to learn the offset -> a note, not a guess", G.core(markets, trades, G.Gold(bars), {H0: 1.0}, ST._Acc)["trades"] == 0)

    # ---- the study wrapper, with fake data sources (no network) ----
    tmp = tempfile.mkdtemp()
    wdb = WarehouseDB(Path(tmp) / "w.db")
    wdb.execute("INSERT INTO wh_series VALUES('KXGOLDH', 'Commodities', 'G', 'quadratic', 1.0, 'hourly', '{}', 0)")
    for mk in markets[:2]:
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete, strike_type, floor_strike) "
                    "VALUES(?,?, 'KXGOLDH', 'Commodities', 'finalized', ?, ?, ?, 10, 1, 'greater', ?)", (mk["ticker"], mk["ticker"], mk["close"] - 3600, mk["close"], mk["result"], mk["strike"]))
        for i, (ts, yp, side) in enumerate(trades[mk["ticker"]]):
            wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"{mk['ticker']}{i}", mk["ticker"], ts, yp, 1, side, "bid" if side == "yes" else "ask"))
    import datetime as dt

    class FakeClient:
        def paginate(self, path, key, params, max_items=None):
            assert path == "/markets" and params["status"] == "settled"
            for c, v in settles.items():
                yield {"close_time": dt.datetime.fromtimestamp(c, dt.timezone.utc).isoformat(), "expiration_value": str(v)}

    class Resp:
        def __init__(self, j):
            self.j = j

        def raise_for_status(self):
            pass

        def json(self):
            return self.j

    class FakeSession:
        def __init__(self):
            self.urls = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.urls.append(url)
            ks = sorted(k for k in bars if params["period1"] <= k < params["period2"])
            return Resp({"chart": {"result": [{"timestamp": ks, "indicators": {"quote": [{"close": [bars[k] for k in ks]}]}}]}})
    G.time.sleep = lambda s: None
    conn = __import__("sqlite3").connect(str(Path(tmp) / "w.db"))
    conn.row_factory = __import__("sqlite3").Row
    sess = FakeSession()
    r2 = ST.goldfv(conn, {"series": ["KXGOLDH"]}, session=sess, client=FakeClient())
    check("study: warehouse trades + futures + Kalshi settlements give the same answer as the core", r2.get("trades") == 3 and r2["futures_symbol"] == "GCZ26.CMX"
          and "GCZ26.CMX" in sess.urls[0], (r2.get("trades"), r2.get("note"), r2.get("futures_symbol")))
    check("the client it is given only reads markets (no order methods exist on it)", not any(hasattr(FakeClient, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
