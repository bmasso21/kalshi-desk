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
    fee = lambda p: 0.07 * p * (1 - p)
    import bisect

    def paths(ticker, side):
        m = conn.execute("SELECT open_ts o, result res FROM wh_markets WHERE ticker=?", (ticker,)).fetchone()
        rows = conn.execute("SELECT ts, yes_price yp, taker_outcome_side tos, taker_book_side tbs FROM wh_trades WHERE ticker=? ORDER BY ts", (ticker,)).fetchall()
        tr = ST._compress(rows)
        t0 = m["o"] + 120
        i0 = bisect.bisect_right([t[0] for t in tr], t0)
        yp0 = tr[i0 - 1][1]
        return ST._scalp_paths(tr, i0, t0, side, yp0 if side == "yes" else round(1 - yp0, 4), m["res"] == "yes", 0.07, 0.0)
    # entry at 2 minutes, last print 50c.  Tap orders pay 51c.
    #  M1 YES: +10% target is 57c (51 x 1.1 = 56.1 -> 57c), needs a 58c print: the 80c print at 600s -> +6c less two fees
    #  M1 NO : NO only falls: held, lost 51c and the fee.   M2 YES: same.   M2 NO: NO is 60c at 200s -> target hit
    win = 0.57 - 0.51 - fee(0.51) - fee(0.57)
    lose = -0.51 - fee(0.51)
    a1, a2, a3, a4 = paths("Q-1", "yes")["taker"], paths("Q-1", "no")["taker"], paths("Q-2", "yes")["taker"], paths("Q-2", "no")["taker"]
    check("scalp tap orders, +10%, no stop: reaches the target only after paying the spread; the others lose the whole stake",
          close(a1[(0.10, None)][0], win) and close(a2[(0.10, None)][0], lose) and close(a3[(0.10, None)][0], lose) and close(a4[(0.10, None)][0], win), (a1[(0.10, None)], a2[(0.10, None)]))
    # 25% stop: sell when the side is at or under 38.25c: M1 NO at 600s (NO = 20c -> sold at 19c), M2 YES at 300s (30c -> sold at 29c)
    check("scalp tap orders, 25% stop: losers are sold 1c under the print that triggers the stop",
          close(a2[(0.10, 0.25)][0], 0.19 - 0.51 - fee(0.51) - fee(0.19)) and close(a3[(0.10, 0.25)][0], 0.29 - 0.51 - fee(0.51) - fee(0.29)) and close(a1[(0.10, 0.25)][0], win), a2[(0.10, 0.25)])
    # other targets on M1 YES (cost 51c): +5% = 54c needs 55c -> the 57c print at 240s; +60% = 82c needs 83c -> the 95c print; hold = settles YES at $1
    check("scalp: each profit target is its own exit; holding pays the settlement and one fee",
          close(a1[(0.05, None)][0], 0.54 - 0.51 - fee(0.51) - fee(0.54)) and close(a1[(0.60, None)][0], 0.82 - 0.51 - fee(0.51) - fee(0.82))
          and close(a1[(None, None)][0], 1 - 0.51 - fee(0.51)) and close(a3[(None, None)][0], lose), {k: round(v[0], 4) for k, v in a1.items() if k[1] is None})
    # a stop that triggers BEFORE a far target is reached wins the race: M2 NO with +60% (cost 51c -> 82c needs 83c: NO is 90c at 500s) vs none -> target at 500s
    check("scalp: target and stop race by time", close(a4[(0.60, 0.25)][0], 0.82 - 0.51 - fee(0.51) - fee(0.82)) and close(a3[(0.60, 0.50)][0], 0.09 - 0.51 - fee(0.51) - fee(0.09)), (a4[(0.60, 0.25)], a3[(0.60, 0.50)]))
    # resting orders: buy limit 49c.  M1 YES fills at 130s (49c print, a seller); +10% target 54c: the 54c print at 200s was a buyer -> sold at 54c, no fees
    #  M1 NO: fills at 200s (NO 46c, through the limit), then falls: lost 49c.  M2 YES: fills at 200s (40c), lost 49c.
    #  M2 NO: never trades at or under 49c inside 2 minutes -> never filled
    m1, m2, m3, m4 = paths("Q-1", "yes")["maker"], paths("Q-1", "no")["maker"], paths("Q-2", "yes")["maker"], paths("Q-2", "no")["maker"]
    check("scalp resting orders: no fee, one of four never fills, and the fills are the ones the price moved against",
          close(m1[(0.10, None)][0], 0.05) and close(m2[(0.10, None)][0], -0.49) and close(m3[(0.10, None)][0], -0.49) and m4 is None, (m1[(0.10, None)], m4))
    ex = {(x["exec"], x["target"], x["stop"]): x for x in r["exits"]}
    check("scalp: the exits table covers every target x stop for both order types (2 x 6 x 3)", len(ex) == 36 and ex[("taker", "+10%", "none")]["all"]["entries"] > 0
          and ex[("maker", "hold", "none")]["all"]["unfilled"] > 0, len(ex))
    check("scalp: first and second half are reported separately (winners are picked on one, checked on the other)",
          ex[("taker", "hold", "none")]["first_half"]["entries"] + ex[("taker", "hold", "none")]["second_half"]["entries"] == ex[("taker", "hold", "none")]["all"]["entries"]
          and r["search"]["combinations_tested"] == 0 and r["min_n"] == 150, r["search"])
    check("scalp: tables by signal, price, minute and series are present", all(r[k] for k in ("signals", "prices", "minutes", "by_series")) and {x["signal"] for x in r["signals"]} <= {"rising", "falling", "flat"})
    check("scalp: nothing above 90c is entered", not any(x["bucket"] not in ("5-19c", "20-39c", "40-59c", "60-79c", "80-90c") for x in r["prices"]))
    check("scalp: no short contracts in scope -> a note, not an error", ST.scalp(conn, {"series": ["NOPE"]})["markets"] == 0)
    # the clustered statistic: 3 clusters with returns (1,1), (0,), (-1,-1,-1) -> mean -1/6
    acc = ST._Acc()
    for g_, r_ in ((1, 1), (1, 1), (2, 0), (3, -1), (3, -1), (3, -1)):
        acc.add(g_, r_, 0.5)
    o = acc.out()
    mean = -1 / 6
    se = (((2 - 2 * mean) ** 2 + (0 - mean) ** 2 + (-3 - 3 * mean) ** 2) * 3 / 2) ** 0.5 / 6
    check("scalp: standard error clusters contracts that end together", o["entries"] == 6 and o["events"] == 3 and close(o["ret_c"], 100 * mean, 0.001) and close(o["t"], round(mean / se, 2), 0.011), o)
    conn.close()


def hourly_tests(tmp):
    """Hourly contracts: entries are counted back from the close (30 = buy at :30 for the top-of-the-hour close)."""
    wdb = WarehouseDB(Path(tmp) / "h.db")
    wdb.execute("INSERT INTO wh_series VALUES('G', 'Commodities', 'G', 'quadratic', 1.0, 'hourly', '{}', 0)")
    O = 1_790_000_000.0
    n = [0]

    def mk(t, o, res, prints, dur=3600, complete=1):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete) "
                    "VALUES(?,?, 'G', 'Commodities', 'finalized', ?, ?, ?, 100, ?)", (t, t, o, o + dur, res, complete))
        for sec, yp, side in prints:
            n[0] += 1
            wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"h{n[0]}", t, o + sec, yp, 1, side, "bid" if side == "yes" else "ask"))
    # one print a minute at 64c from the open, then 90c in the last 4 minutes; settles YES
    mk("G-1", O, "yes", [(60 * i, .64, "yes") for i in range(1, 56)] + [(3360, .90, "yes")])
    mk("G-2", O + 3600, "no", [(60 * i, .64, "yes") for i in range(1, 56)] + [(3360, .10, "no")])
    mk("G-3", O + 7200, "yes", [(60, .5, "yes")] * 6, dur=600)            # a 10-minute contract: not an hourly one
    conn = __import__("sqlite3").connect(str(Path(tmp) / "h.db"))
    conn.row_factory = __import__("sqlite3").Row
    r = ST.hourly(conn, {"series": ["G"]})
    check("hourly: only contracts at least 30 minutes long are replayed, entries counted from the close",
          r["markets"] == 2 and r["entry_from"] == "close" and 30 in r["entry_minutes"], (r.get("markets"), r.get("entry_from")))
    mins = {(x["exec"], x["entry_min"], x["target"]): x for x in r["minutes"]}
    a = mins[("taker", 30, "hold")]["all"]
    fee = 0.07 * 0.65 * 0.35
    # at 30 minutes out YES costs 65c (64c + 1c): one wins 35c, one loses 65c; NO costs 37c: one loses, one wins 63c; each pays its fee
    exp = ((1 - .65 - fee) + (-.65 - fee) + (1 - .37 - 0.07 * .37 * .63) + (-.37 - 0.07 * .37 * .63)) / 4
    check("hourly: buying at :30 and holding pays the settlement less the ask and one fee", a["entries"] == 4 and close(a["ret_c"], round(100 * exp, 3), 0.002), a)
    check("hourly: the 50-minute entry exists for an hour-long contract; nothing is entered before the open",
          ("taker", 50, "hold") in mins and all(x["entry_min"] in ST.HOURLY_ENTRY_MIN for x in r["minutes"]))
    check("scalp still ignores hourly contracts", ST.scalp(conn, {"series": ["G"]})["markets"] == 1)
    conn.close()


def gap_tests(tmp):
    """Where the underlying sat versus the strike, read off the strike ladder."""
    wdb = WarehouseDB(Path(tmp) / "g.db")
    wdb.execute("INSERT INTO wh_series VALUES('G', 'Commodities', 'G', 'quadratic', 1.0, 'hourly', '{}', 0)")
    O = 1_790_000_000.0
    n = [0]

    def mk(t, o, k, res, prints):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete, strike_type, floor_strike) "
                    "VALUES(?,?, 'G', 'Commodities', 'finalized', ?, ?, ?, 100, 1, 'greater', ?)", (t, t, o, o + 3600, res, k))
        for sec, yp in prints:
            n[0] += 1
            wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"g{n[0]}", t, o + sec, yp, 1, "yes", "bid"))
    every = lambda yp: [(60 * i, yp) for i in range(1, 60)]
    # one hour, three strikes: 3840 at 90c, 3850 at 70c, 3860 at 30c -> 50/50 level = 3850 + (0.2/0.4)*10 = 3855
    mk("G-A-T3840", O, 3840.0, "yes", every(.90))
    mk("G-A-T3850", O, 3850.0, "yes", every(.70))
    mk("G-A-T3860", O, 3860.0, "no", every(.30))
    conn = __import__("sqlite3").connect(str(Path(tmp) / "g.db"))
    conn.row_factory = __import__("sqlite3").Row
    ladder = []
    for k, t in ((3840.0, "G-A-T3840"), (3850.0, "G-A-T3850"), (3860.0, "G-A-T3860")):
        rows = conn.execute("SELECT ts, yes_price FROM wh_trades WHERE ticker=? ORDER BY ts", (t,)).fetchall()
        ladder.append((k, [r[0] for r in rows], [r[1] for r in rows]))
    check("gap: the market's gold level is where 'above X' is a 50/50 bet (3855 between the 70c and 30c strikes)", close(ST._ladder_mid(ladder, O + 1800), 3855.0))
    check("gap: no level when every strike is on one side of 50c or the prints are stale",
          ST._ladder_mid([(1.0, [O], [.9]), (2.0, [O], [.8])], O + 10) is None and ST._ladder_mid(ladder, O + 3600 + 5000) is None)
    r = ST.gap(conn, {"series": ["G"]}, [{"ticker": "G-A-T3850", "ts": O + 1800}, {"ticker": "G-NOPE", "ts": O}])
    pr = r["probes"]
    check("gap: one of your buys is placed on the ladder (gold ~3855, strike 3850, $5 above, 30 minutes left)",
          close(pr[0]["gold_est"], 3855.0) and close(pr[0]["gap"], 5.0) and pr[0]["min_left"] == 30.0 and pr[1]["note"] == "contract not collected yet", pr)
    g30 = {x["gap"]: x["all"] for x in r["by_minute_gap"] if x["entry_min"] == 30}
    fee = lambda c: 0.07 * c * (1 - c)
    # at :30 -> 3840 YES (gap 15) costs 91c, wins; 3850 YES (gap 5) costs 71c, wins; 3860 NO (gap 5) costs 71c, wins
    check("gap: the favoured side is bought (YES above the level, NO below), bucketed by the cushion",
          g30["$15-25"]["entries"] == 1 and close(g30["$15-25"]["ret_c"], round(100 * (1 - .91 - fee(.91)), 3), 0.002)
          and g30["$5-10"]["entries"] == 2 and close(g30["$5-10"]["ret_c"], round(100 * (1 - .71 - fee(.71)), 3), 0.002), g30)
    conn.close()


def main():
    tmp = tempfile.mkdtemp()
    scalp_tests(tempfile.mkdtemp())
    hourly_tests(tempfile.mkdtemp())
    gap_tests(tempfile.mkdtemp())
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


def newmkt_test():
    import sqlite3
    tmp = tempfile.mkdtemp()
    wdb = WarehouseDB(Path(tmp) / "n.db")
    O = 1_790_000_000.0
    wdb.execute("INSERT INTO wh_series VALUES('S', 'Weather', 'S', 'quadratic', 1.0, 'daily', '{}', 0)")
    for t, life, res in (("A-1", 3 * 86400, "yes"), ("SHORT-1", 3600, "yes")):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete) "
                    "VALUES(?,?, 'S', 'Weather', 'finalized', ?, ?, ?, 100, 1)", (t, t.split("-")[0], O, O + life, res))
    for i, (t, ts, yp, n, side) in enumerate((("A-1", O + 600, 0.40, 10, "yes"),            # 10 minutes old: YES bought at 40c, wins
                                              ("A-1", O + 2 * 86400, 0.70, 10, "no"),       # 2 days old: NO bought at 30c, loses
                                              ("A-1", O - 5, 0.50, 10, "yes"),              # before the open: ignored
                                              ("SHORT-1", O + 600, 0.40, 10, "yes"))):      # a one-hour contract: not in the study
        wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"n{i}", t, ts, yp, n, side, "bid" if side == "yes" else "ask"))
    conn = sqlite3.connect(str(Path(tmp) / "n.db"))
    conn.row_factory = sqlite3.Row
    r = ST.newmkt(conn, {})
    age = {x["age"]: x for x in r["by_age"]}
    check("newmkt: only markets open a day or more; trades grouped by the market's age", r["markets"] == 1 and set(age) == {"first hour", "1-3 days"}, r)
    check("newmkt: first-hour taker bought YES at 40c and won: +60c less the 1.68c fee; the maker on the other side lost 60c",
          close(age["first hour"]["taker"]["ret_c"], 100 * (0.6 - 0.07 * 0.4 * 0.6), 0.01) and close(age["first hour"]["maker"]["ret_c"], -60.0, 0.01), age["first hour"])
    check("newmkt: the 2-day-old NO at 30c lost: -30c less the fee; contracts counted",
          close(age["1-3 days"]["taker"]["ret_c"], -100 * (0.3 + 0.07 * 0.7 * 0.3), 0.01) and age["1-3 days"]["contracts"] == 10, age["1-3 days"])
    pr = {x["price"]: [y["age"] for y in x["rows"]] for x in r["by_age_and_price"]}
    check("newmkt: split by price paid and by category too", pr["20-79c"] == ["first hour", "1-3 days"] and r["by_category"][0]["category"] == "Weather", pr)
    check("newmkt is a named study and only runs when asked for", "newmkt" in ST.RUN and "newmkt" in ST.EXTRA and "newmkt" not in ST.KINDS)


def newflow_test():
    import sqlite3
    tmp = tempfile.mkdtemp()
    wdb = WarehouseDB(Path(tmp) / "f.db")
    O = 1_790_000_000.0
    wdb.execute("INSERT INTO wh_series VALUES('S', 'Weather', 'S', 'quadratic', 1.0, 'daily', '{}', 0)")
    for t, res in (("A-1", "yes"), ("B-1", "no"), ("C-1", "yes")):
        wdb.execute("INSERT INTO wh_markets(ticker, event_ticker, series_ticker, category, status, open_ts, close_ts, result, volume, complete) "
                    "VALUES(?,?, 'S', 'Weather', 'finalized', ?, ?, ?, 100, 1)", (t, t.split("-")[0], O, O + 2 * 86400, res))
    rows = (("A-1", O + 60, 0.40, "yes"),          # signal: YES at 40c
            ("A-1", O + 90, 0.45, "yes"),          # 5c dearer: not within 1c of the 40c signal (and no later YES within 1c of 45c)
            ("A-1", O + 300, 0.41, "yes"),         # 4 minutes after the first signal, 1c dearer: the follower's entry at 41c; wins
            ("B-1", O + 100, 0.70, "no"),          # signal: NO at 30c
            ("B-1", O + 100 + 900, 0.70, "no"),    # same price but 15 minutes later: too late for 10 minutes, in time for 30; wins (result no)
            ("C-1", O + 5000, 0.50, "yes"),        # first trade after the first hour: no signal
            ("C-1", O + 5100, 0.50, "yes"))
    for i, (t, ts, yp, side) in enumerate(rows):
        wdb.execute("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,0)", (f"f{i}", t, ts, yp, 5, side, "bid" if side == "yes" else "ask"))
    conn = sqlite3.connect(str(Path(tmp) / "f.db"))
    conn.row_factory = sqlite3.Row
    r = ST.newflow(conn, {})
    rule = {(x["wait_s"], x["slip_c"]): x for x in r["rules"]}
    check("newflow: signals are first-hour taker trades, one per market and side; a market first traded later has none",
          r["signals"] == 2 and r["markets_with_first_hour_trades"] == 2 and r["signal_itself"]["entries"] == 2, r)
    check("newflow: the follower buys the same side at the next real trade within 10 minutes and 1c: 41c on A, wins 59c less the fee",
          rule[(600, 1)]["entries"] == 1 and close(rule[(600, 1)]["ret_c"], 100 * (0.59 - 0.07 * 0.41 * 0.59), 0.01), rule[(600, 1)])
    check("newflow: with no slip the 41c trade is not a follow of the 40c signal but IS one of the 45c signal (cheaper is fine); nothing within 2 minutes",
          rule[(600, 0)]["entries"] == 1 and rule[(600, 0)]["avg_price_c"] == 41.0 and rule[(120, 1)]["entries"] == 0, (rule[(600, 0)], rule[(120, 1)]))
    check("newflow: a 30-minute wait also catches B's NO at 30c (won): two entries",
          rule[(1800, 1)]["entries"] == 2 and rule[(1800, 1)]["win_pct"] == 100.0 and rule[(1800, 1)]["events"] == 2, rule[(1800, 1)])
    check("newflow: the main rule is broken down by half, side, price and category",
          r["by_side"][0]["entries"] == 1 and r["by_category"][0]["category"] == "Weather" and r["first_half"]["entries"] + r["second_half"]["entries"] == 1, r)
    check("newflow is a named study and only runs when asked for", "newflow" in ST.RUN and "newflow" in ST.EXTRA and "newflow" not in ST.KINDS)


if __name__ == "__main__":
    main()
    newmkt_test()
    newflow_test()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
