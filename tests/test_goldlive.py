"""Live gold fair value (PAXG on Kraken minus an offset learned from Kalshi settlements) and the paper rule."""
import datetime as dt
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gapwatch as GW  # noqa: E402
from app import goldlive as GL  # noqa: E402
from app.goldfv import phi  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


CLOSE = 1_790_010_000.0 - (1_790_010_000.0 % 3600) + 3600     # a top-of-the-hour moment
NOW = CLOSE - 40 * 60                                           # 40 minutes left
iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def paxg_rows(now):
    """Zigzag PAXG +-1 around 3813 every minute (sigma 2) for 6 hours, plus the running (incomplete) minute."""
    start = int(now // 60) * 60 - 360 * 60
    rows = [[start + 60 * i, "0", "0", "0", str(3813.0 + (1 if i % 2 else -1)), "0", "1", 1] for i in range(361)]
    return rows                                                 # the last one starts at now//60*60: still running


class Resp:
    def __init__(self, j):
        self.j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self.j


class Session:
    def __init__(self, now):
        self.n = 0
        self.now = now

    def get(self, url, params=None, timeout=None):
        self.n += 1
        assert "kraken" in url and params["pair"] == "PAXGUSD"
        return Resp({"error": [], "result": {"PAXGUSD": paxg_rows(self.now), "last": 0}})


class Kalshi:
    """Public market data only: settled values and the open ladder."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def paginate(self, path, key, params, max_items=None):
        assert params["status"] == "settled"
        bars = {int(r[0]): float(r[4]) for r in self.rows}
        for h in range(1, 6):                                   # five past hours settled at PAXG - 13
            c = CLOSE - 3600 * h
            yield {"close_time": iso(c), "expiration_value": str(round(bars[int(c) - 60] - 13.0, 2))}

    def markets(self, status="open", series_ticker=None, max_items=None, **kw):
        self.calls += 1
        out = []
        for k, (yb, ya) in {3790: (.80, .82), 3800: (.40, .45), 3810: (.20, .24), 3830: (.00, .02)}.items():
            out.append({"ticker": f"G-T{k}", "strike_type": "greater", "floor_strike": k, "close_time": iso(CLOSE),
                        "yes_bid_dollars": str(yb), "yes_ask_dollars": str(ya)})
        return out


def main():
    ses = Session(NOW)
    k = Kalshi(paxg_rows(NOW))
    g = GL.GoldLive(k, session=ses)
    bars = g.bars(NOW)
    check("only COMPLETED one-minute bars are used (the running minute is dropped)", bars[-1][0] + 60 <= NOW and len(bars) == 360)
    g.bars(NOW + 5)
    check("PAXG is cached for 15 seconds", ses.n == 1)
    check("sigma from one-minute changes (zigzag +-1 -> 2)", close(GL.GoldLive.sigma(bars), 2.0, 1e-9))
    off, n = g.offset(NOW)
    check("offset to Kalshi's settlement price learned from Kalshi's own settled values (PAXG - 13)", close(off, 13.0, 0.011) and n == 5, (off, n))
    b = g.board(CLOSE, NOW)
    S = bars[-1][1] - off
    rows = {r["strike"]: r for r in b["rows"]}
    p3800 = phi((S - 3800) / (2.0 * math.sqrt(40)))
    fee = lambda a: 0.07 * a * (1 - a)
    check("board: gold = PAXG - offset, every strike priced with the model", close(b["gold"], round(S, 2), 0.01) and len(rows) == 4 and close(rows[3800]["fair_yes"], round(p3800, 4), 1e-4), b.get("gold"))
    check("edges use the REAL asks: YES at the ask, NO at 1 - YES bid, less the taker fee",
          close(rows[3800]["yes_edge"], round(p3800 - .45 - fee(.45), 4), 1e-4) and close(rows[3800]["no_edge"], round((1 - p3800) - .60 - fee(.60), 4), 1e-4), rows[3800])
    check("a NO side with no YES bid has no price (it would cost $1) -> no edge", rows[3830]["no_edge"] is None)
    stale = GL.GoldLive(k, session=Session(NOW))
    stale._bars = (NOW + 1000, [(int(NOW) - 900, 3813.0)] * 1)
    check("stale or missing gold -> a note, no numbers", "note" in stale.board(CLOSE, NOW + 1000) and not stale.board(CLOSE, NOW + 1000)["rows"])

    # ---- the paper rule ----
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    off = GW.GapWatch(lambda: {}, WarehouseDB(Path(tempfile.mkdtemp()) / "o.db"), k, fv=g)
    off.check("KXGOLDH", CLOSE, 40, NOW)
    check("the gold model rule is retired by default (no paper buys unless switched back on)", not off.db.query("SELECT 1 FROM gw_fv_picks"))
    gw = GW.GapWatch(lambda: {"gold_fv_rule": True}, wdb, k, fv=g)
    gw.check("KXGOLDH", CLOSE, 40, NOW)
    fv = {(r["ticker"], r["side"]): r for r in wdb.query("SELECT * FROM gw_fv_picks")}
    want = set()
    for kk, r in rows.items():
        for side in ("yes", "no"):
            e, a = r.get(f"{side}_edge"), r.get(f"{side}_ask")
            if e is not None and a is not None and e >= 0.02 and 0.20 <= a <= 0.80:
                want.add((f"G-T{int(kk)}", side))
    check("paper rule: every side 2c+ under the model's fair price at 20-80c is bought at its real ask, nothing else", set(fv) == want and len(want) >= 1, (sorted(fv), sorted(want)))
    gw.check("KXGOLDH", CLOSE, 20, CLOSE - 1200)
    check("the rule only runs 30-60 minutes before the close", all(r["check_min"] in (50, 40, 30) for r in wdb.query("SELECT check_min FROM gw_fv_picks")))

    class K2(Kalshi):
        def markets(self, status="open", series_ticker=None, tickers=None, max_items=None, **kw):
            if tickers:
                return [{"ticker": t, "result": "yes"} for t in tickers]
            return super().markets(status, series_ticker, max_items)
    gw.k = K2(k.rows)
    gw.settle(CLOSE + 600)
    st = wdb.query("SELECT side, ask, fee, pnl, status FROM gw_fv_picks")
    check("fair-value paper buys settle with the cushion ones: result less the ask and fee",
          all(r["status"] == "settled" and close(r["pnl"], (1 if r["side"] == "yes" else 0) - r["ask"] - r["fee"], 1e-5) for r in st), st)
    s = gw.summary()
    check("summary: the rule counted once per contract and side, and per check time", s["fv_rule_first_touch"]["n"] == len(want) and s["fv_open"] == 0, s["fv_rule_first_touch"])
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
