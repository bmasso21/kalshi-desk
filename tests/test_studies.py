"""Edge studies on a small hand-built history where every answer can be checked by hand."""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import studies as ST  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def build(tmp):
    wdb = WarehouseDB(Path(tmp) / "w.db")
    T0 = 1_790_000_000.0
    wdb.execute("INSERT INTO wh_series VALUES('S', 'Weather', 'S', 'quadratic', 1.0, 'daily', '{}', 0)")
    wdb.execute("INSERT INTO wh_series VALUES('F', 'Other', 'F', 'quadratic_with_maker_fees', 1.0, 'daily', '{}', 0)")

    def mkt(t, ev, series, cat, close_ts, result, complete=1):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete) "
                    "VALUES(?,?,?,?, 'finalized', ?, ?, ?, 100, ?)", (t, ev, series, cat, close_ts - 86400 * 2, close_ts, result, complete))

    def trade(i, t, ts, yp, n, side):
        wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"t{i}", t, ts, yp, n, side, "bid" if side == "yes" else "ask"))

    def candle(t, end_ts, bid, ask):
        wdb.execute("INSERT INTO wh_candles(ticker, period, end_ts, bid_c, ask_c) VALUES(?, 60, ?, ?, ?)", (t, int(end_ts), bid, ask))
    i = 0
    # 10 events x 2 outcomes (A wins in 9 of them). Takers buy the favorite A at 80c and the longshot B at 20c... B also at 5c.
    for e in range(10):
        ct = T0 + e * 86400
        a_wins = e != 3
        mkt(f"E{e}-A", f"E{e}", "S", "Weather", ct, "yes" if a_wins else "no")
        mkt(f"E{e}-B", f"E{e}", "S", "Weather", ct, "no" if a_wins else "yes")
        for t, yp, n, side in ((f"E{e}-A", 0.80, 10, "yes"), (f"E{e}-B", 0.20, 10, "yes"), (f"E{e}-B", 0.95, 4, "no")):
            i += 1
            trade(i, t, ct - 2 * 3600, yp, n, side)
        # candles 3h before close: A 78/80... one hour later the set is mispriced on purpose in event 5
        candle(f"E{e}-A", ct - 6 * 3600, 0.68, 0.70)           # 6h before: a 70c favorite and a 31c longshot
        candle(f"E{e}-B", ct - 6 * 3600, 0.29, 0.31)
        candle(f"E{e}-A", ct - 3 * 3600, 0.93, 0.95)
        candle(f"E{e}-B", ct - 3 * 3600, 0.04, 0.06)
        mkt(f"W{e}", f"WE{e}", "S", "Weather2", ct, "no")         # empty book: bid 2c, ask 99c. YES at 99c is no favorite
        candle(f"W{e}", ct - 3 * 3600, 0.02, 0.99)
        if e == 5:
            candle(f"E{e}-A", ct - 2 * 3600, 0.60, 0.62)
            candle(f"E{e}-B", ct - 2 * 3600, 0.30, 0.32)       # asks sum to 0.94 -> buy both for under $1
        if e == 6:
            candle(f"E{e}-A", ct - 2 * 3600, 0.70, 0.72)
            candle(f"E{e}-B", ct - 2 * 3600, 0.40, 0.42)       # bids sum to 1.10 -> sell both for over $1
        if e == 7:
            candle(f"E{e}-A", ct - 2 * 3600, 0.50, 0.52)       # only one outcome quoted that hour: not a complete set
    # another category with maker fees, to check the filter and the maker fee
    mkt("X-1", "X", "F", "Other", T0 + 86400, "yes")
    trade(999, "X-1", T0, 0.50, 100, "yes")
    mkt("X-2", "X2", "F", "Other", T0 + 86400, "no", complete=2)      # a sampled market whose trade list was cut at the cap
    trade(998, "X-2", T0, 0.50, 7, "yes")
    return wdb, T0


def scalp_tests(tmp):
    """15-minute contracts with hand-made prints: every outcome below can be worked out on paper."""
    wdb = WarehouseDB(Path(tmp) / "s.db")
    wdb.execute("INSERT INTO wh_series VALUES('Q', 'Crypto', 'Q', 'quadratic', 1.0, '15min', '{}', 0)")
    O = 1_790_000_000.0
    n = [0]

    def mk(t, o, res, prints, complete=1, dur=900):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete) "
                    "VALUES(?,?, 'Q', 'Crypto', 'finalized', ?, ?, ?, 100, ?)", (t, t, o, o + dur, res, complete))
        for sec, yp, side in prints:
            n[0] += 1
            wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"s{n[0]}", t, o + sec, yp, 1, side, "bid" if side == "yes" else "ask"))
    # M1: 50c at 2 min; rises: 57c print at 4 min (taker target 56c+1c reached), ends YES
    mk("Q-1", O, "yes", [(10, .50, "yes"), (60, .50, "no"), (110, .50, "yes"), (130, .49, "no"), (200, .54, "yes"), (240, .57, "yes"), (600, .80, "yes"), (800, .95, "yes")])
    # M2: 50c at 2 min; falls straight to 10c, ends NO (YES buyer never reaches +10%)
    mk("Q-2", O + 900, "no", [(10, .50, "yes"), (60, .50, "no"), (110, .50, "yes"), (200, .40, "no"), (300, .30, "no"), (500, .10, "no"), (800, .03, "no")])
    mk("Q-3", O + 1800, "no", [(10, .50, "yes")] * 6, complete=2)          # cut at the cap: skipped
    mk("Q-4", O + 2700, "yes", [(10, .50, "yes")] * 6, dur=86400)          # a daily contract: not a short one
    conn = __import__("sqlite3").connect(str(Path(tmp) / "s.db"))
    conn.row_factory = __import__("sqlite3").Row
    r = ST.scalp(conn, {"series": ["Q"]})
    check("scalp: only the settled short contracts with full trade lists are replayed", r["markets"] == 2 and r["skipped_too_busy"] == 1, (r.get("markets"), r.get("skipped_too_busy")))
    tb = {(x["exec"], x["stop"], x["entry_min"], x["bucket"]): x for x in r["table"]}
    fee = lambda p: 0.07 * p * (1 - p)
    # taker, no stop, entry at 2 min, 40-59c bucket: 4 entries (YES and NO of M1 and M2).
    #  M1 YES: pay 51c, target 57c (51 x 1.1 = 56.1 -> 57c), needs a 58c print: reached at 600s (80c) -> +6c - fees
    #  M1 NO : pay 51c (NO at 50c), NO falls, held, lost: -51c - fee
    #  M2 YES: pay 51c, never up, held, lost.   M2 NO: pay 51c, NO rises to 60c at 200s -> target 57c hit -> +6c - fees
    a = tb[("taker", "none", 2, "40-59c")]
    win = 0.57 - 0.51 - fee(0.51) - fee(0.57)
    lose = -0.51 - fee(0.51)
    check("scalp taker, no stop: 2 of 4 reach +10% (after paying the spread), 2 lose the whole stake", a["entries"] == 4 and a["win_pct"] == 50.0
          and close(a["stat"]["ret_c"], 100 * (2 * win + 2 * lose) / 4, 0.002), a)
    # with a 25% stop: the losers sell when the side's price is at or under 38.25c: M1 NO at 600s (NO = 20c -> sells at 19c), M2 YES at 300s (30c -> 29c)
    b = tb[("taker", "-25%", 2, "40-59c")]
    l1 = 0.19 - 0.51 - fee(0.51) - fee(0.19)
    l2 = 0.29 - 0.51 - fee(0.51) - fee(0.29)
    check("scalp taker, 25% stop: losers are sold 1c under the print that triggers the stop", close(b["stat"]["ret_c"], 100 * (2 * win + l1 + l2) / 4, 0.002), b)
    # resting orders: buy limit 49c. M1 YES fills at 130s (49c, a seller hit it); target 54c (49 x 1.1 = 53.9): the 54c print at 200s was a buyer -> sold at 54c, no fees
    #  M1 NO: NO limit 49c needs NO <= 49c with a seller of NO (= a YES buyer at >= 51c): 200s (YES 54c -> NO 46c, below the limit) -> filled, then NO falls, lost 49c
    #  M2 YES: YES falls through 49c at 200s (40c) -> filled, lost 49c.   M2 NO: NO never trades at or under 49c within 2 minutes after 120s? 200s is 80s later: NO = 60c -> not filled
    m_ = tb[("maker", "none", 2, "40-59c")]
    check("scalp resting orders: no fee, one of four never fills, the fills are the ones the price moved against",
          m_["entries"] == 3 and m_["filled_pct"] == 75.0 and close(m_["stat"]["ret_c"], 100 * ((0.54 - 0.49) + 2 * (-0.49)) / 3, 0.002), m_)
    ov = {(x["exec"], x["stop"]): x for x in r["overall"]}
    check("scalp: how each trade ended is counted", ov[("taker", "none")]["ends"].get("target", 0) >= 2 and "held, lost" in ov[("taker", "none")]["ends"], ov[("taker", "none")]["ends"])
    check("scalp: nothing above 90c is entered (a +10% target would be over $1)", not any(x["bucket"] not in ("5-19c", "20-39c", "40-59c", "60-79c", "80-90c") for x in r["table"]))
    check("scalp: no short contracts in scope -> a note, not an error", ST.scalp(conn, {"series": ["NOPE"]})["markets"] == 0)
    conn.close()


def main():
    tmp = tempfile.mkdtemp()
    scalp_tests(tempfile.mkdtemp())
    wdb, T0 = build(tmp)
    st = ST.Studies(wdb)
    conn = st._ro()
    r = ST.longshot(conn, {"categories": ["Weather"]})
    bp = {x["bucket"]: x for x in r["by_price"]}
    check("longshot: 20 weather markets, three price buckets (the 5c NO trade is a taker paying 5c)", r["markets"] == 20 and set(bp) == {"1-9c", "20-29c", "80-89c"}, list(bp))
    fav = bp["80-89c"]
    fee80 = 0.07 * 0.8 * 0.2
    check("favorite at 80c: 100 contracts, won 90%", fav["contracts"] == 100 and fav["win_pct"] == 90.0 and fav["avg_price_c"] == 80.0, fav)
    check("favorite: taker return = 0.90 - 0.80 - fee (8.88c)", close(fav["taker"]["ret_c"], 100 * (0.9 - 0.8 - fee80), 0.002), fav["taker"])
    check("favorite: maker return is the mirror with no maker fee (-10c)", close(fav["maker"]["ret_c"], -10.0, 0.002), fav["maker"])
    lng = bp["20-29c"]
    check("longshot at 20c: won 10%; taker -11.12c, maker +10c", lng["win_pct"] == 10.0 and close(lng["taker"]["ret_c"], 100 * (0.1 - 0.2 - fee80), 0.002)
          and close(lng["maker"]["ret_c"], 10.0, 0.002), lng)
    tiny = bp["1-9c"]
    check("taker buying NO at 5c (YES price 95c) is counted at 5c and wins when the market settles NO", tiny["avg_price_c"] == 5.0 and tiny["contracts"] == 40
          and tiny["win_pct"] == 90.0, tiny)
    check("error bars are across events (10 of them), with a t-statistic", fav["taker"]["events"] == 10 and fav["taker"]["t"] is not None and fav["taker"]["se_c"] > 0, fav["taker"])
    ev_ret = [(1 if e != 3 else 0) - 0.8 - fee80 for e in range(10)]
    m = sum(ev_ret) / 10
    se = (sum((x - m) ** 2 for x in ev_ret) / 10 * (10 / 9)) ** 0.5 / 10 ** 0.5
    check("clustered standard error matches the hand calculation", close(fav["taker"]["se_c"], 100 * se, 0.01), (fav["taker"]["se_c"], 100 * se))
    check("split into halves (5 events each)", sum(x["contracts"] for x in r["first_half"]) + sum(x["contracts"] for x in r["second_half"]) == 240
          and {x["bucket"] for x in r["first_half"]} == set(bp))
    check("time-to-close split: every trade was 2h before the close", [x["when"] for x in r["by_time_to_close"]] == ["1-6h"], r["by_time_to_close"])
    check("the two side labels in the data are tabulated for a sanity check", {(x["outcome_side"], x["book_side"]) for x in r["side_labels"]} == {("yes", "bid"), ("no", "ask")})
    r2 = ST.longshot(conn, {"categories": ["Other"]})
    x = r2["by_price"][0]
    check("a market whose trade list was cut at the sample cap is left out of the trade study, and counted", r2["skipped_too_busy"] == 1 and len(r2["by_price"]) == 1)
    check("category filter + maker fee series: maker pays 0.0175 x .5 x .5 (taker -1.75c + 50c, maker -50.44c)", r2["markets"] == 1 and x["contracts"] == 100
          and close(x["taker"]["ret_c"], 100 * (1 - 0.5 - 0.07 * 0.25), 0.002) and close(x["maker"]["ret_c"], 100 * (0.5 - 1 - 0.0175 * 0.25), 0.002), x)
    check("empty scope is reported, not an error", ST.longshot(conn, {"categories": ["Nope"]})["markets"] == 0)

    lt = ST.late(conn, {"categories": ["Weather"]})
    rows = {(x["hours_before"], x["bucket"]): x for x in lt["rows"]}
    a = rows[(3, "93-95c")]
    # at 3h: A's YES ask .95 (9 of 10 win) and B's NO at 1-.04 = .96 (9 of 10 win)
    check("late: 3h before the close, the 95c side was buyable in all 10 events and won 9", a["buys"] == 10 and a["win_pct"] == 90.0 and a["avg_price_c"] == 95.0, a)
    check("late: return = 0.90 - 0.95 - fee (-5.33c) and the break-even win rate is shown", close(a["all"]["ret_c"], 100 * (0.9 - 0.95 - 0.07 * 0.95 * 0.05), 0.002)
          and close(a["breakeven_pct"], 100 * (0.95 + 0.07 * 0.95 * 0.05), 0.01), a)
    b = rows[(3, "96-97c")]
    check("late: the NO side (bought at 1 - bid = 96c) is included", b["buys"] == 10 and b["avg_price_c"] == 96.0, b)
    wide = ST.late(conn, {"categories": ["Weather2"]})
    check("late: a 99c ask on an empty book (bid 2c) is NOT counted as a favorite", wide["rows"] == [] and wide["max_spread_c"] == 3, wide["rows"])
    check("the biased per-event average is no longer reported", "taker_eq" not in bp["80-89c"] and "maker_eq" not in bp["80-89c"], list(bp["80-89c"]))
    check("late: no candle near 24h before the close -> no row invented", not any(h == 24 for h, _ in rows), list(rows))

    cb = ST.calib(conn, {"categories": ["Weather"]})
    cr = {(x["hours_before"], x["bucket"]): x for x in cb["rows"]}
    fav6 = cr[(6, "70-79c")]
    # 6h before: YES on A at 70c and NO on B at 1 - .29 = 71c, each wins in 9 of 10 events
    pavg = 0.705
    exp = 0.9 - pavg - 0.07 * (0.70 * 0.30 + 0.71 * 0.29) / 2
    check("blind buying, 6h before: the 70c sides (YES at 70c, NO at 71c) were bought 20 times and won 90%", fav6["buys"] == 20 and fav6["win_pct"] == 90.0
          and close(fav6["avg_price_c"], 70.5, 0.06), fav6)
    check("blind buying: return = win rate - price - fee (+18.0c here), with event-clustered error bars", close(fav6["all"]["ret_c"], 100 * exp, 0.01)
          and fav6["all"]["events"] == 10 and fav6["all"]["t"] is not None, (fav6["all"], 100 * exp))
    lng6 = cr[(6, "30-39c")]
    check("blind buying: the other sides (31c and 32c) win 10% and lose", lng6["buys"] == 20 and lng6["win_pct"] == 10.0 and lng6["all"]["ret_c"] < -20, lng6)
    check("blind buying: empty books are skipped and both halves are reported", not any(b == "90-99c" and x["avg_price_c"] > 98 for (h_, b), x in cr.items())
          and fav6["first_half"]["events"] + fav6["second_half"]["events"] == 10)
    bk = ST.baskets(conn, {"categories": ["Weather"]})
    by, sl = bk["buy_all_yes"], bk["sell_all_yes"]
    check("baskets: 10 events with exactly one winner; incomplete hours are not counted", bk["events"] == 10 and bk["complete_event_hours"] == 22 and bk["event_hours"] == 23, bk)
    p_buy = 1 - 0.94 - 0.07 * (0.62 * 0.38 + 0.32 * 0.68)
    check("baskets: buying both YES at 62c + 32c leaves 6c before fees, 2.83c after", by["hours_with_gap_after_fees"] == 1 and by["events_with_gap"] == 1
          and close(by["max_profit_c"], round(100 * p_buy, 2), 0.011), (by, p_buy))
    p_sell = 1.10 - 1 - 0.07 * (0.70 * 0.30 + 0.40 * 0.60)
    check("baskets: selling both YES at 70c + 40c leaves 10c before fees, 6.85c after", sl["hours_with_gap_after_fees"] == 1 and close(sl["max_profit_c"], round(100 * p_sell, 2), 0.011), (sl, p_sell))
    check("baskets: the normal hours (asks 95c + 6c, bids 93c + 4c) show no gap", by["hours_with_gap_before_fees"] == 1 and sl["hours_with_gap_before_fees"] == 1, (by, sl))
    conn.close()

    # background runner + storage
    rid = st.start("longshot", {"categories": ["Weather"]})
    for _ in range(100):
        if rid not in st.threads:
            break
        time.sleep(0.05)
    L = st.latest({"categories": ["Weather"]})
    check("study runs in the background and its result is stored under its scope", L["longshot"]["status"] == "done" and L["longshot"]["result"]["markets"] == 20
          and L["late"] is None and st.latest()["longshot"] is None, L["longshot"]["status"])
    wdb.execute("INSERT INTO wh_studies(ts, kind, status, params) VALUES(?, 'late', 'running', '{}')", (time.time(),))
    try:
        st.start("late")
        dup = False
    except RuntimeError:
        dup = True
    check("a study already running is not started twice", dup)
    st.recover()
    check("a study interrupted by a restart is marked, not left 'running' forever", st.latest()["late"]["status"] == "error")
    try:
        c2 = st._ro()
        c2.execute("DELETE FROM wh_trades")
        wrote = True
    except Exception:
        wrote = False
    check("the studies' database connection is read-only", wrote is False)


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
