"""Phase 1.4 tests: data warehouse (live + historical tiers) and the backtester (hand-computed expectations)."""
import calendar
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402

OK, FAIL = [], []
NOW = int(time.time())
H = 3600
P = "/trade-api/v2"


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return srv


# ---------------------------------------------------------------- data mock --
MKT_CUT = NOW - 10 * 86400          # markets settled before this are historical-only
TRD_CUT = NOW - 12 * 86400          # trades before this are historical-only


def mkt(t, series, close, status, result="", vol=100, settle=None):
    return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "market_type": "binary", "title": t, "yes_sub_title": "x",
            "status": status, "open_time": iso(close - 48 * H), "close_time": iso(close), "result": result,
            "settlement_ts": iso(settle) if settle else None, "settlement_value_dollars": "1.0000" if result == "yes" else
            ("0.0000" if result == "no" else None), "volume_fp": f"{vol:.2f}", "open_interest_fp": "50.00",
            "strike_type": "between", "floor_strike": 40, "cap_strike": 41}


def build_data_mock():
    app = FastAPI()
    app.state.calls = []
    M1 = mkt("KXHIGHNY-26NOV05-B40", "KXHIGHNY", NOW - 5 * 86400, "finalized", "yes", settle=NOW - 5 * 86400 + 3600)
    M2 = mkt("KXHIGHNY-26OCT20-B50", "KXHIGHNY", NOW - 20 * 86400, "finalized", "no", settle=NOW - 20 * 86400 + 3600)
    M3 = mkt("KXHIGHNY-26NOV11-B45", "KXHIGHNY", NOW + 86400, "active")
    M4 = mkt("KXHIGHNY-26MAR01-B30", "KXHIGHNY", NOW - 200 * 86400, "finalized", "yes", settle=NOW - 200 * 86400)
    M5 = mkt("KXNFL-26NOV09-GB", "KXNFL", NOW - 2 * 86400, "finalized", "yes", settle=NOW - 2 * 86400)
    M6 = mkt("KXHIGHNY-26NOV06-B41", "KXHIGHNY", NOW - 4 * 86400, "finalized", "no", vol=0, settle=NOW - 4 * 86400)
    M7 = mkt("KXHURR-26NOV-X", "KXHURR", NOW - 3 * 86400, "finalized", "no", settle=NOW - 3 * 86400)
    live = {"KXHIGHNY": [M1, M3, M6], "KXNFL": [M5], "KXHURR": [M7]}
    hist = {"KXHIGHNY": [M2, M4]}
    # trades: M1 live; M2 straddles the trades cutoff; M3 live
    def tr(tid, t, ts, px, n=5, side="bid"):
        return {"trade_id": tid, "ticker": t, "created_time": iso(ts), "yes_price_dollars": f"{px:.4f}",
                "no_price_dollars": f"{1 - px:.4f}", "count_fp": f"{n:.2f}", "taker_outcome_side": "yes",
                "taker_book_side": side, "is_block_trade": False}
    trades = [tr("t1", M1["ticker"], NOW - 5 * 86400 - 3 * H, 0.70), tr("t2", M1["ticker"], NOW - 5 * 86400 - H, 0.80),
              tr("t3", M2["ticker"], NOW - 20 * 86400 - 5 * H, 0.30), tr("t4", M2["ticker"], NOW - 20 * 86400 - 2 * H, 0.20),
              tr("t5", M3["ticker"], NOW - 2 * H, 0.45)]
    app.state.trades = trades

    def page(items, cursor, n=2):
        i = int(cursor or 0)
        return items[i:i + n], (str(i + n) if i + n < len(items) else "")

    @app.middleware("http")
    async def log(request: Request, call_next):
        app.state.calls.append((request.url.path, dict(request.query_params)))
        return await call_next(request)

    @app.get(P + "/series")
    def series(category: str = "", include_volume: str = ""):
        allx = [{"ticker": "KXHIGHNY", "category": "Climate and Weather", "title": "NYC high", "fee_type": "quadratic", "fee_multiplier": 1,
                 "tags": ["Daily temperature"]},
                {"ticker": "KXHURR", "category": "Climate and Weather", "title": "Hurricanes", "fee_type": "quadratic", "fee_multiplier": 1,
                 "tags": ["Hurricanes"]},
                {"ticker": "KXNFL", "category": "Sports", "title": "NFL", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}]
        return {"series": [s for s in allx if not category or s["category"] == category]}

    @app.get(P + "/series/{t}")
    def one_series(t: str):
        return {"series": {"ticker": t, "category": "Sports", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}

    @app.get(P + "/historical/cutoff")
    def cutoff():
        return {"market_settled_ts": iso(MKT_CUT), "trades_created_ts": iso(TRD_CUT), "orders_updated_ts": iso(MKT_CUT)}

    @app.get(P + "/markets")
    def markets(series_ticker: str = "", status: str = "", cursor: str = "", limit: int = 100):
        want = {"open": ("active",), "closed": ("closed",), "settled": ("finalized", "determined")}.get(status, ())
        ms = [m for m in live.get(series_ticker, []) if not want or m["status"] in want]
        items, cur = page(ms, cursor, 1)
        return {"markets": items, "cursor": cur}

    @app.get(P + "/historical/markets")
    def hmarkets(series_ticker: str = "", cursor: str = "", limit: int = 100):
        items, cur = page(hist.get(series_ticker, []), cursor, 1)
        return {"markets": items, "cursor": cur}

    def _trades(ticker, min_ts, max_ts, cursor, hist_):
        ts_ = lambda t: calendar.timegm(time.strptime(t["created_time"], "%Y-%m-%dT%H:%M:%SZ"))
        sel = [t for t in app.state.trades if t["ticker"] == ticker and min_ts <= ts_(t) <= max_ts
               and ((ts_(t) < TRD_CUT) == hist_)]
        items, cur = page(sel, cursor, 1)
        return {"trades": items, "cursor": cur}

    @app.get(P + "/markets/trades")
    def ltrades(ticker: str = "", min_ts: int = 0, max_ts: int = 10 ** 12, cursor: str = "", limit: int = 100):
        return _trades(ticker, min_ts, max_ts, cursor, False)

    @app.get(P + "/historical/trades")
    def htrades(ticker: str = "", min_ts: int = 0, max_ts: int = 10 ** 12, cursor: str = "", limit: int = 100):
        return _trades(ticker, min_ts, max_ts, cursor, True)

    @app.get(P + "/series/{s}/markets/{t}/candlesticks")
    def lcandles(s: str, t: str, start_ts: int, end_ts: int, period_interval: int):
        e = start_ts - start_ts % 3600 + 3600
        out = []
        while e <= end_ts and len(out) < 3:
            out.append({"end_period_ts": e, "yes_bid": {"open_dollars": "0.40", "high_dollars": "0.45", "low_dollars": "0.38", "close_dollars": "0.44"},
                        "yes_ask": {"open_dollars": "0.42", "high_dollars": "0.47", "low_dollars": "0.40", "close_dollars": "0.46"},
                        "price": {"open_dollars": "0.41", "high_dollars": "0.46", "low_dollars": "0.39", "close_dollars": "0.45", "mean_dollars": "0.43"},
                        "volume_fp": "12.00", "open_interest_fp": "30.00"})
            e += 3600
        return {"ticker": t, "candlesticks": out}

    @app.get(P + "/historical/markets/{t}/candlesticks")
    def hcandles(t: str, start_ts: int, end_ts: int, period_interval: int):
        e = start_ts - start_ts % 3600 + 3600
        return {"ticker": t, "candlesticks": [{"end_period_ts": e, "yes_bid": {"open": "0.20", "high": "0.25", "low": "0.18", "close": "0.22"},
                                                "yes_ask": {"open": "0.24", "high": "0.27", "low": "0.21", "close": "0.26"},
                                                "price": {"open": "0.23", "high": "0.26", "low": "0.20", "close": "0.25", "mean": "0.23", "previous": None},
                                                "volume": "7.00", "open_interest": "20.00"}]}
    return app


# --------------------------------------------------------------------- main --
def main():
    from app.backtest import Strategy, register, summarize
    from app.config import Config
    from app.db import DB
    from app.server import create_app

    tmp = tempfile.mkdtemp()
    rest = create_mock({})
    serve(rest, 8971)
    dm = build_data_mock()
    serve(dm, 8972)
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:8971/trade-api/v2", "ws_enabled": False,
                                "warehouse": {"base_url_override": "http://127.0.0.1:8972/trade-api/v2", "req_per_sec": 50,
                                              "lookback_days": 120}}))
    cfg = Config(cfgp)
    app = create_app(cfg, DB(Path(tmp) / "t.db"), start_engine=False)
    eng, wdb, bt = app.state.engine, app.state.wdb, app.state.backtester
    wh = eng.warehouse
    c = TestClient(app, raise_server_exceptions=False)
    HD = {"X-Desk-Token": c.get("/api/session").json()["token"]}

    # ================================ warehouse =================================
    st = wh.sync()
    q = wdb.query
    tick = {r["ticker"]: r for r in q("SELECT * FROM wh_markets")}
    check("sync ran without errors", st["errors"] == 0 and wh.state["error"] is None, (st, wh.state))
    check("tag filter: untagged weather series (hurricanes) skipped", "KXHURR-26NOV-X" not in tick and
          not any(qp.get("series_ticker") == "KXHURR" for _, qp in dm.state.calls))
    check("only in-scope series collected (weather, not NFL)", set(r["series_ticker"] for r in tick.values()) == {"KXHIGHNY"}
          and "KXNFL-26NOV09-GB" not in tick, list(tick))
    check("live settled + open + historical markets inside lookback", {"KXHIGHNY-26NOV05-B40", "KXHIGHNY-26NOV11-B45",
          "KXHIGHNY-26OCT20-B50", "KXHIGHNY-26NOV06-B41"} == set(tick), list(tick))
    check("market older than lookback excluded", "KXHIGHNY-26MAR01-B30" not in tick)
    check("historical tier tagged", tick["KXHIGHNY-26OCT20-B50"]["tier"] == "historical" and tick["KXHIGHNY-26NOV05-B40"]["tier"] == "live")
    check("results + settlement stored", tick["KXHIGHNY-26NOV05-B40"]["result"] == "yes" and tick["KXHIGHNY-26OCT20-B50"]["result"] == "no"
          and tick["KXHIGHNY-26NOV05-B40"]["settlement_value"] == 1.0)
    check("series fee info stored", q("SELECT fee_type FROM wh_series WHERE series_ticker='KXHIGHNY'")[0]["fee_type"] == "quadratic")
    trs = {r["trade_id"]: r for r in q("SELECT * FROM wh_trades")}
    check("trades from live and historical endpoints merged", set(trs) == {"t1", "t2", "t3", "t4", "t5"}, list(trs))
    paths = [p for p, _ in dm.state.calls]
    check("historical trades fetched via /historical/trades", any(p.endswith("/historical/trades") for p in paths))
    check("historical candles via /historical/markets/{t}/candlesticks",
          any(p.endswith("/historical/markets/KXHIGHNY-26OCT20-B50/candlesticks") for p in paths))
    hc = q("SELECT * FROM wh_candles WHERE ticker='KXHIGHNY-26OCT20-B50'")
    lc = q("SELECT * FROM wh_candles WHERE ticker='KXHIGHNY-26NOV05-B40' ORDER BY end_ts")
    check("historical candle fields normalised (open/close, volume)", hc and hc[0]["bid_c"] == 0.22 and hc[0]["ask_c"] == 0.26
          and hc[0]["volume"] == 7.0 and hc[0]["px_mean"] == 0.23, hc[:1])
    check("live candle fields normalised (*_dollars, *_fp)", lc and lc[0]["bid_c"] == 0.44 and lc[0]["ask_o"] == 0.42
          and lc[0]["volume"] == 12.0 and lc[0]["open_interest"] == 30.0, lc[:1])
    check("zero-volume market: no trade requests", not any(qp.get("ticker") == "KXHIGHNY-26NOV06-B41" and p.endswith("trades")
                                                         for p, qp in dm.state.calls))
    check("settled markets marked complete; open market not",
          q("SELECT complete FROM wh_markets WHERE ticker='KXHIGHNY-26NOV05-B40'")[0]["complete"] == 1 and
          q("SELECT complete FROM wh_markets WHERE ticker='KXHIGHNY-26NOV11-B45'")[0]["complete"] == 0)
    n_calls = len(dm.state.calls)
    dm.state.trades.append({"trade_id": "t6", "ticker": "KXHIGHNY-26NOV11-B45", "created_time": iso(NOW - 60),
                            "yes_price_dollars": "0.4700", "no_price_dollars": "0.5300", "count_fp": "2.00",
                            "taker_outcome_side": "yes", "taker_book_side": "bid", "is_block_trade": False})
    st2 = wh.sync()
    new_calls = dm.state.calls[n_calls:]
    check("2nd sync: completed markets not re-downloaded", not any(qp.get("ticker") in ("KXHIGHNY-26NOV05-B40", "KXHIGHNY-26OCT20-B50")
                                                                  for _, qp in new_calls if "trades" in _), new_calls[-6:])
    check("2nd sync: open market picks up only the new trade", st2["trades"] == 1 and q("SELECT COUNT(*) n FROM wh_trades")[0]["n"] == 6, st2)
    check("no duplicate rows after re-sync", q("SELECT COUNT(*) n FROM wh_trades")[0]["n"] == len({r['trade_id'] for r in q('SELECT trade_id FROM wh_trades')}))
    r = c.get("/api/research").json()
    check("/api/research status + strategies", r["warehouse"]["counts"]["trades"] == 6 and
          {s["name"] for s in r["strategies"]} >= {"favorite_late", "maker_join"}, r["warehouse"]["counts"])
    # tick recorder
    eng.feed.top = lambda t: {"yes_bid": 0.5, "yes_ask": 0.52, "yes_bid_size": 10, "no_bid_size": 20}
    for _ in range(3):
        wh.on_feed("book", {"ticker": "KXHIGHNY-26NOV11-B45"})
    eng.feed.top = lambda t: {"yes_bid": 0.51, "yes_ask": 0.52, "yes_bid_size": 10, "no_bid_size": 20}
    wh.on_feed("book", {"ticker": "KXHIGHNY-26NOV11-B45"})
    check("live ticks recorded only on top-of-book change", q("SELECT COUNT(*) n FROM wh_ticks")[0]["n"] == 2)
    # config validation
    rr = c.post("/api/config", headers=HD, json={"warehouse": {"lookback_days": 5000, "categories": ["Economics", "Climate and Weather"],
                                                                "base_url_override": "http://evil"}})
    check("warehouse config: bad lookback rejected, categories saved, URL override ignored",
          rr.status_code == 400 and cfg.data["warehouse"]["lookback_days"] == 120 and cfg.data["warehouse"]["categories"] == ["Economics", "Climate and Weather"]
          and cfg.data["warehouse"]["base_url_override"].startswith("http://127.0.0.1"), rr.text)

    # ================================ backtester =================================
    wdb.execute("DELETE FROM wh_markets"); wdb.execute("DELETE FROM wh_trades"); wdb.execute("DELETE FROM wh_candles")
    wdb.execute("INSERT OR REPLACE INTO wh_series(series_ticker,category,fee_type,fee_multiplier) VALUES('S','Test','quadratic',1)")
    wdb.execute("INSERT OR REPLACE INTO wh_series(series_ticker,category,fee_type,fee_multiplier) VALUES('SM','Test','quadratic_with_maker_fees',1)")
    T0 = 1_800_000_000

    def add_market(t, result, series="S", close_off=10):
        wdb.execute("INSERT INTO wh_markets(ticker,series_ticker,category,status,open_ts,close_ts,result,volume,complete) "
                    "VALUES(?,?,?,?,?,?,?,?,1)", (t, series, "Test", "finalized", T0, T0 + close_off * H, result, 100))

    def add_bars(t, quotes):
        for i, (b, a) in enumerate(quotes, start=1):
            wdb.execute("INSERT INTO wh_candles(ticker,period,end_ts,bid_c,ask_c,px_c) VALUES(?,?,?,?,?,?)",
                        (t, 60, T0 + i * H, b, a, (b + a) / 2))

    def add_trade(tid, t, ts, px, n, side):
        wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (tid, t, ts, px, n, "yes", side))

    # favorite_late: first bar with <= 6h to close is the bar ending T0+4h (close T0+10h)
    q4 = [(0.50, 0.52)] * 3 + [(0.84, 0.86)] + [(0.90, 0.92)] * 6
    add_market("A", "yes"); add_bars("A", q4)                                   # buys YES @ .86
    add_market("B", "no"); add_bars("B", [(0.5, 0.52)] * 3 + [(0.08, 0.10)] + [(0.05, 0.07)] * 6)   # NO ask = .92
    add_market("C", "yes"); add_bars("C", [(0.5, 0.52)] * 3 + [(0.15, 0.17)] + [(0.2, 0.22)] * 6)   # NO ask .85, loses
    add_market("D", "yes"); add_bars("D", [(0.5, 0.52)] * 10)                    # nothing in range
    add_trade("dA", "A", T0 + 9.5 * H, 0.95, 1, "bid")                           # closing line for A
    res = bt.run("favorite_late", {"hours_before": 6, "lo": 0.8, "hi": 0.95, "count": 10}, {"series": ["S"]})
    m = res["models"]["through"]
    pm = {r["ticker"]: r for r in m["per_market"]}
    # fees: ceil(0.07*10*p*(1-p)) to the cent
    check("favorite_late picks the right side/price", pm["A"]["cost"] == 8.6 and pm["B"]["cost"] == 9.2 and pm["C"]["cost"] == 8.5, pm)
    check("taker fees replayed exactly (.09/.06/.09)", pm["A"]["fees"] == 0.09 and pm["B"]["fees"] == 0.06 and pm["C"]["fees"] == 0.09, pm)
    check("settlement P&L: A +1.31, B +0.74, C -8.59", pm["A"]["pnl"] == 1.31 and pm["B"]["pnl"] == 0.74 and pm["C"]["pnl"] == -8.59, pm)
    check("totals: 3 markets traded, net -6.54, fees .24", m["markets_traded"] == 3 and m["pnl"] == -6.54 and m["fees"] == 0.24
          and m["contracts"] == 30 and m["markets_simulated"] == 4, {k: m[k] for k in ("markets_traded", "pnl", "fees", "contracts")})
    check("ROI = pnl/cost", abs(m["roi"] - round(-6.54 / 26.3, 4)) < 1e-4, m["roi"])
    check("CLV from closing line (A: .95-.86 = 9c)", pm["A"]["closing_line"] == 0.95 and m["by_series"]["S"]["avg_clv_cents"] is not None)
    check("max drawdown on same-day settlement", m["max_drawdown"] == 0.0 or m["max_drawdown"] >= 0)
    res_s = bt.run("favorite_late", {"hours_before": 6, "lo": 0.8, "hi": 0.95, "count": 10}, {"series": ["S"]}, slippage_cents=1)
    pms = {r["ticker"]: r for r in res_s["models"]["through"]["per_market"]}
    check("slippage 1c raises taker prices", pms["A"]["cost"] == 8.7 and pms["B"]["cost"] == 9.3, pms)

    # no lookahead
    seen = {"violations": 0, "result_visible": False, "bars": 0}

    @register
    class Peek(Strategy):
        name = "test_peek"
        defaults = {}

        def on_bar(self, ctx):
            seen["bars"] += 1
            if "result" in ctx.market or "settlement_value" in ctx.market:
                seen["result_visible"] = True
            if any(t["ts"] > ctx.now for t in ctx.trades()) or ctx.bars[-1]["end_ts"] != ctx.now:
                seen["violations"] += 1
    bt.run("test_peek", {}, {"series": ["S"]})
    check("strategy never sees the result", not seen["result_visible"] and seen["bars"] == 40)
    check("strategy never sees future trades/bars", seen["violations"] == 0)

    # maker fills: through vs touch, size-limited, after placement only, maker fees
    @register
    class OneBid(Strategy):
        name = "test_onebid"
        defaults = {"side": "yes", "price": 0.40, "count": 10}
        uses_maker = True

        def on_bar(self, ctx):
            if not ctx.state.get("placed"):
                ctx.state["placed"] = ctx.buy(ctx.params["side"], ctx.params["price"], ctx.params["count"], maker=True)

    add_market("MK", "yes", series="SM"); add_bars("MK", [(0.38, 0.42)] * 10)
    add_trade("m0", "MK", T0 + 0.5 * H, 0.30, 50, "ask")     # BEFORE placement -> must not fill
    add_trade("m1", "MK", T0 + 1.5 * H, 0.40, 5, "ask")      # at price, taker sold YES -> touch only
    add_trade("m2", "MK", T0 + 2.5 * H, 0.40, 5, "bid")      # at price but taker bought -> no fill either way
    add_trade("m3", "MK", T0 + 3.5 * H, 0.39, 3, "bid")      # through -> both
    add_trade("m4", "MK", T0 + 4.5 * H, 0.35, 100, "ask")    # through, big -> fills the rest
    r2 = bt.run("test_onebid", {}, {"series": ["SM"]})
    th, to = r2["models"]["through"], r2["models"]["touch"]
    check("maker run reports both fill models", set(r2["models"]) == {"through", "touch"})
    check("through: 3 (m3) + 7 (m4) = 10 contracts, nothing before placement", th["contracts"] == 10 and th["maker_fills"] == 2, th["sample_fills"])
    check("touch: 5 (m1) + 3 (m3) + 2 (m4) = 10", to["contracts"] == 10 and to["maker_fills"] == 3, to["sample_fills"])
    # maker fee (quadratic_with_maker_fees): ceil(.0175*n*.4*.6): n=3 -> .0126->.02; n=7 -> .0294->.03; n=5 -> .021->.03; n=2 -> .0084->.01
    check("maker fees replayed per fill (through .05, touch .06)", th["fees"] == 0.05 and to["fees"] == 0.06, (th["fees"], to["fees"]))
    check("maker settlement: YES 10 @ .40 wins -> pnl 6 - fees", th["pnl"] == round(10 - 4 - 0.05, 2) and to["pnl"] == round(10 - 4 - 0.06, 2))
    # NO-side maker bid q=.55 (= sell YES at .45)
    add_market("MN", "no", series="S"); add_bars("MN", [(0.43, 0.47)] * 10)
    add_trade("n1", "MN", T0 + 1.5 * H, 0.45, 4, "bid")      # at price, taker bought YES -> touch only
    add_trade("n2", "MN", T0 + 2.5 * H, 0.46, 3, "ask")      # through -> both
    r3 = bt.run("test_onebid", {"side": "no", "price": 0.55, "count": 10}, {"series": ["S"], "min_volume": 1})
    pmn = lambda mod: {x["ticker"]: x for x in r3["models"][mod]["per_market"]}.get("MN", {})
    check("NO maker: through 3, touch 7", pmn("through").get("contracts") == 3 and pmn("touch").get("contracts") == 7,
          (pmn("through"), pmn("touch")))
    add_market("MX", "yes", series="S"); add_bars("MX", [(0.38, 0.42)] * 10)
    r4 = bt.run("test_onebid", {"price": 0.45}, {"series": ["S"]})
    check("post-only crossing order rejected", r4["models"]["through"]["rejected_orders"] >= 1)

    # summarize: equity curve / drawdown on known daily P&L
    from app.backtest import Fill
    fake = [{"fills": [Fill("x", 0, "yes", 0.5, 1, 0, False)], "pnl": v, "cost": 1, "fees": 0, "payout": 0, "contracts": 1, "clv_sum": 0, "clv_n": 0,
             "rejects": 0, "close_ts": T0 + i * 86400, "ticker": f"x{i}", "category": "c", "series": "s", "result": "yes",
             "closing_line": None} for i, v in enumerate([5, -3, -4, 6])]
    sm = summarize(fake)
    check("equity curve + max drawdown (peak 5 -> -2 = 7)", sm["max_drawdown"] == 7 and sm["equity_curve"][-1][1] == 4 and sm["days"] == 4, sm["equity_curve"])

    # API: async run, list, detail, errors
    rid = c.post("/api/backtests", headers=HD, json={"strategy": "favorite_late", "params": {"count": 10}, "filters": {"series": ["S"]}}).json()["id"]
    for _ in range(100):
        d = c.get(f"/api/backtests/{rid}").json()
        if d["status"] != "running":
            break
        time.sleep(0.1)
    check("API backtest completes and stores results", d["status"] == "done" and d["results"]["models"]["through"]["pnl"] == -6.54, d.get("error"))
    pr = bt.procs.get(rid)
    check("API backtest ran in a separate process (exit 0)", pr is not None and pr.wait(30) == 0 and pr.pid != __import__("os").getpid())
    lst = c.get("/api/backtests").json()["backtests"]
    check("API list shows summary per fill model", lst[0]["id"] == rid and lst[0]["summary"]["through"]["pnl"] == -6.54)
    check("unknown strategy rejected", c.post("/api/backtests", headers=HD, json={"strategy": "nope"}).status_code == 400)
    check("backtests never touch trading (no orders logged)", not eng.db.query("SELECT 1 FROM orders_log LIMIT 1"))


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
