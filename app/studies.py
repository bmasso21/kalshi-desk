"""Edge studies (v3.8): three questions asked of the stored market history. Read-only - nothing is traded, and the
work runs on its own read-only database connection so the paper test and the collectors are never blocked.

  longshot   Favorite-longshot bias. Every public trade in a settled market: what the TAKER paid (in the taker's side's
             price), whether that side won, and so what takers and makers earned per contract, by price bucket.
                 taker return = win - price - taker fee          maker return = price - win - maker fee
             (the maker holds the other side at 1 - price, so before fees one's gain is the other's loss).
  calib      Buying blind at fixed times. The trade-level study shows what the people who traded earned - and they chose
             their moment. This asks the question a rule can act on: at 24 / 12 / 6 / 3 / 1 hours before the close, in
             every market with a real two-sided quote (3c or tighter), buy 1 contract of each side at its ask and hold.
             Return by price paid. If favorites are genuinely underpriced it shows here; if the trade-level profits
             came from good timing, it does not.
  late       Late-stage drift. At 24 / 12 / 6 / 3 / 1 hours before a market closes, if one side could be BOUGHT for
             90-99c (hourly closing quote, bid and ask no more than 3c apart - a 99c ask on an empty book is a
             placeholder, not a favorite), buy 1 contract and hold to settlement: win - ask - taker fee.
  baskets    Outcome sets that don't add up. For events whose markets turned out mutually exclusive and exhaustive
             (exactly one settled YES), every hour: could you buy every YES for under $1, or sell every YES (buy every
             NO) for over $1, after taker fees?

Fees: taker 0.07 x multiplier x p x (1-p) per contract; maker 0.0175 / 0.035 x the same for series with maker fees,
else 0. Per-order rounding (up to 1c per order) is NOT included, so small orders do slightly worse than shown.

Honest error bars: trades in the same event rise and fall together (one day's temperature decides every bucket), so
the standard error is computed across EVENTS, not trades. |t| under 2 means "could easily be luck".
Each table is also split into the earlier and later half of the period: an effect that only shows in one half is not
an effect. Quotes in `late` and `baskets` are hourly snapshots without size - they show whether a gap existed, not how
many contracts you could have had.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import traceback

SCHEMA = """
CREATE TABLE IF NOT EXISTS wh_studies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, status TEXT, params TEXT, result TEXT, error TEXT, duration REAL
);
"""
KINDS = ("longshot", "calib", "late", "baskets")
TAKER = 0.07
MAKER_SQL = "CASE s.fee_type WHEN 'quadratic_with_maker_fees' THEN 0.0175 WHEN 'quadratic_with_combo_maker_fees' THEN 0.035 ELSE 0 END"
HOURS = (24, 12, 6, 3, 1)
LATE_MAX_SPREAD = 0.03      # a 9xc ask on an empty book is a placeholder, not a favorite: require a real two-sided quote
LATE_BUCKETS = ((0.90, 0.925, "90-92c"), (0.925, 0.955, "93-95c"), (0.955, 0.975, "96-97c"), (0.975, 0.995, "98-99c"))


def _stat(groups: dict) -> dict:
    """groups: event -> [weight, sum of (return x weight)]. Weighted mean return (cents) and its event-clustered t."""
    w = sum(g[0] for g in groups.values())
    if w <= 0:
        return {"n": 0, "events": 0, "ret_c": None, "se_c": None, "t": None}
    mean = sum(g[1] for g in groups.values()) / w
    var = sum((g[1] - mean * g[0]) ** 2 for g in groups.values())
    k = len(groups)
    se = math.sqrt(var * (k / (k - 1))) / w if k > 1 else None
    return {"n": round(w, 2), "events": k, "ret_c": round(100 * mean, 3), "se_c": round(100 * se, 3) if se else None,
            "t": round(mean / se, 2) if se else None}


def _where(flt: dict, alias="m") -> tuple[str, list]:
    sql, args = f"{alias}.result IN ('yes','no')", []
    if flt.get("categories"):
        sql += f" AND {alias}.category IN ({','.join('?' * len(flt['categories']))})"
        args += list(flt["categories"])
    if flt.get("series"):
        sql += f" AND {alias}.series_ticker IN ({','.join('?' * len(flt['series']))})"
        args += list(flt["series"])
    return sql, args


def longshot(conn, flt: dict | None = None) -> dict:
    flt = flt or {}
    w, args = _where(flt)
    span = conn.execute(f"SELECT MIN(m.close_ts) a, MAX(m.close_ts) b, COUNT(*) n, SUM(m.complete = 2) cut FROM wh_markets m WHERE {w} AND m.complete IN (1, 2)", args).fetchone()
    if not span["n"]:
        return {"markets": 0, "note": "no settled markets with stored trades in scope"}
    mid = (span["a"] + span["b"]) / 2
    ty = ("CASE WHEN t.taker_outcome_side='yes' THEN 1 WHEN t.taker_outcome_side='no' THEN 0 "
          "WHEN t.taker_book_side='bid' THEN 1 WHEN t.taker_book_side='ask' THEN 0 END")
    sql = f"""
    SELECT ev, b, hb, half, SUM(n) n, SUM(tp*n) px, SUM(win*n) wins, SUM(tfee*n) tfee, SUM(mfee*n) mfee, COUNT(*) trades FROM (
      SELECT COALESCE(m.event_ticker, m.ticker) ev, t.count n,
             CASE WHEN {ty}=1 THEN t.yes_price ELSE 1-t.yes_price END tp,
             CASE WHEN ({ty}=1 AND m.result='yes') OR ({ty}=0 AND m.result='no') THEN 1.0 ELSE 0.0 END win,
             MIN(9, CAST((CASE WHEN {ty}=1 THEN t.yes_price ELSE 1-t.yes_price END) * 10 AS INT)) b,
             CASE WHEN m.close_ts - t.ts < 3600 THEN 0 WHEN m.close_ts - t.ts < 21600 THEN 1 WHEN m.close_ts - t.ts < 86400 THEN 2 ELSE 3 END hb,
             CASE WHEN m.close_ts < ? THEN 0 ELSE 1 END half,
             {TAKER} * COALESCE(s.fee_multiplier, 1) * t.yes_price * (1 - t.yes_price) tfee,
             ({MAKER_SQL}) * COALESCE(s.fee_multiplier, 1) * t.yes_price * (1 - t.yes_price) mfee
      FROM wh_trades t JOIN wh_markets m ON m.ticker = t.ticker LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
      WHERE {w} AND m.complete = 1 AND t.yes_price > 0 AND t.yes_price < 1 AND t.count > 0 AND {ty} IS NOT NULL AND t.ts <= m.close_ts
    ) GROUP BY ev, b, hb, half"""
    rows = conn.execute(sql, [mid] + args).fetchall()
    hb_names = ["under 1h", "1-6h", "6-24h", "over 24h"]

    def table(pred, key):
        tk, mk, meta = {}, {}, {}
        for r in rows:
            if not pred(r):
                continue
            k = key(r)
            tk.setdefault(k, {}).setdefault(r["ev"], [0.0, 0.0])
            mk.setdefault(k, {}).setdefault(r["ev"], [0.0, 0.0])
            tk[k][r["ev"]][0] += r["n"]
            tk[k][r["ev"]][1] += r["wins"] - r["px"] - r["tfee"]
            mk[k][r["ev"]][0] += r["n"]
            mk[k][r["ev"]][1] += r["px"] - r["wins"] - r["mfee"]
            x = meta.setdefault(k, [0.0, 0.0, 0.0, 0])
            x[0] += r["n"]
            x[1] += r["px"]
            x[2] += r["wins"]
            x[3] += r["trades"]
        out = []
        for k in sorted(meta):
            n, px, wins, trades = meta[k]
            out.append({"key": k, "contracts": round(n), "trades": trades, "avg_price_c": round(100 * px / n, 1),
                        "win_pct": round(100 * wins / n, 1), "taker": _stat(tk[k]), "maker": _stat(mk[k])})
        return out
    lab = lambda b: f"{b * 10 if b else 1}-{b * 10 + 9}c"
    by_price = [{**x, "bucket": lab(x["key"])} for x in table(lambda r: True, lambda r: r["b"])]
    halves = [[{**x, "bucket": lab(x["key"])} for x in table(lambda r, h=h: r["half"] == h, lambda r: r["b"])] for h in (0, 1)]
    by_time = [{**x, "when": hb_names[x["key"]]} for x in table(lambda r: True, lambda r: r["hb"])]
    overall = table(lambda r: True, lambda r: 0)
    san = conn.execute(f"SELECT t.taker_outcome_side o, t.taker_book_side b, COUNT(*) n FROM wh_trades t JOIN wh_markets m ON m.ticker=t.ticker "
                       f"WHERE {w} GROUP BY 1,2", args).fetchall()
    return {"markets": span["n"] - (span["cut"] or 0), "skipped_too_busy": span["cut"] or 0,
            "from": span["a"], "to": span["b"], "split_at": mid, "by_price": by_price, "first_half": halves[0],
            "second_half": halves[1], "by_time_to_close": by_time, "overall": overall[0] if overall else None,
            "side_labels": [{"outcome_side": r["o"], "book_side": r["b"], "trades": r["n"]} for r in san]}


def late(conn, flt: dict | None = None) -> dict:
    flt = flt or {}
    w, args = _where(flt)
    span = conn.execute(f"SELECT MIN(m.close_ts) a, MAX(m.close_ts) b, COUNT(*) n FROM wh_markets m WHERE {w}", args).fetchone()
    if not span["n"]:
        return {"markets": 0, "note": "no settled markets in scope"}
    mid = (span["a"] + span["b"]) / 2
    out = []
    for h in HOURS:
        sql = f"""
        SELECT COALESCE(m.event_ticker, m.ticker) ev, m.result res, m.close_ts ct, c.bid_c bid, c.ask_c ask, COALESCE(s.fee_multiplier, 1) mult
        FROM wh_markets m LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
        JOIN wh_candles c ON c.ticker = m.ticker AND c.period = 60 AND c.end_ts = (
            SELECT MAX(c2.end_ts) FROM wh_candles c2 WHERE c2.ticker = m.ticker AND c2.period = 60 AND c2.end_ts <= m.close_ts - ?)
        WHERE {w} AND c.end_ts >= m.close_ts - ?"""
        rows = conn.execute(sql, [h * 3600] + args + [(h + 1.5) * 3600]).fetchall()
        for lo, hi, name in LATE_BUCKETS:
            g = {"all": {}, 0: {}, 1: {}}
            wins = n = 0
            px = 0.0
            for r in rows:
                if not r["bid"] or not r["ask"] or not (0 < r["bid"] < r["ask"] < 1) or r["ask"] - r["bid"] > LATE_MAX_SPREAD + 1e-9:
                    continue                    # empty or wide book: the quote is not a price anyone agreed on
                for price, won in ((r["ask"], r["res"] == "yes"), (1 - r["bid"], r["res"] == "no")):
                    if not (lo <= price < hi):
                        continue
                    ret = (1.0 if won else 0.0) - price - TAKER * r["mult"] * price * (1 - price)
                    for k in ("all", 0 if r["ct"] < mid else 1):
                        e = g[k].setdefault(r["ev"], [0.0, 0.0])
                        e[0] += 1
                        e[1] += ret
                    n += 1
                    wins += won
                    px += price
            if n:
                out.append({"hours_before": h, "bucket": name, "buys": n, "avg_price_c": round(100 * px / n, 1), "win_pct": round(100 * wins / n, 2),
                            "breakeven_pct": None, **{"all": _stat(g["all"]), "first_half": _stat(g[0]), "second_half": _stat(g[1])}})
    for r in out:      # win rate needed to break even after the fee at the average price
        p = r["avg_price_c"] / 100
        r["breakeven_pct"] = round(100 * (p + TAKER * p * (1 - p)), 2)
    return {"markets": span["n"], "from": span["a"], "to": span["b"], "split_at": mid, "rows": out, "max_spread_c": round(100 * LATE_MAX_SPREAD)}


def calib(conn, flt: dict | None = None) -> dict:
    flt = flt or {}
    w, args = _where(flt)
    span = conn.execute(f"SELECT MIN(m.close_ts) a, MAX(m.close_ts) b, COUNT(*) n FROM wh_markets m WHERE {w}", args).fetchone()
    if not span["n"]:
        return {"markets": 0, "note": "no settled markets in scope"}
    mid = (span["a"] + span["b"]) / 2
    out = []
    for h in HOURS:
        sql = f"""
        SELECT COALESCE(m.event_ticker, m.ticker) ev, m.result res, m.close_ts ct, c.bid_c bid, c.ask_c ask, COALESCE(s.fee_multiplier, 1) mult
        FROM wh_markets m LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
        JOIN wh_candles c ON c.ticker = m.ticker AND c.period = 60 AND c.end_ts = (
            SELECT MAX(c2.end_ts) FROM wh_candles c2 WHERE c2.ticker = m.ticker AND c2.period = 60 AND c2.end_ts <= m.close_ts - ?)
        WHERE {w} AND c.end_ts >= m.close_ts - ?"""
        g: dict = {}
        meta: dict = {}
        for r in conn.execute(sql, [h * 3600] + args + [(h + 1.5) * 3600]):
            if not r["bid"] or not r["ask"] or not (0 < r["bid"] < r["ask"] < 1) or r["ask"] - r["bid"] > LATE_MAX_SPREAD + 1e-9:
                continue
            for price, won in ((r["ask"], r["res"] == "yes"), (1 - r["bid"], r["res"] == "no")):
                b = min(9, int(price * 10 + 1e-9))
                ret = (1.0 if won else 0.0) - price - TAKER * r["mult"] * price * (1 - price)
                for k in ("all", 0 if r["ct"] < mid else 1):
                    e = g.setdefault((b, k), {}).setdefault(r["ev"], [0.0, 0.0])
                    e[0] += 1
                    e[1] += ret
                x = meta.setdefault(b, [0, 0, 0.0])
                x[0] += 1
                x[1] += won
                x[2] += price
        for b in sorted(meta):
            n, wins, px = meta[b]
            p = px / n
            out.append({"hours_before": h, "bucket": f"{b * 10 if b else 1}-{b * 10 + 9}c", "buys": n, "avg_price_c": round(100 * p, 1),
                        "win_pct": round(100 * wins / n, 2), "breakeven_pct": round(100 * (p + TAKER * p * (1 - p)), 2),
                        "all": _stat(g.get((b, "all"), {})), "first_half": _stat(g.get((b, 0), {})), "second_half": _stat(g.get((b, 1), {}))})
    return {"markets": span["n"], "from": span["a"], "to": span["b"], "split_at": mid, "rows": out, "max_spread_c": round(100 * LATE_MAX_SPREAD)}


def baskets(conn, flt: dict | None = None) -> dict:
    flt = flt or {}
    w, args = _where(flt)
    conn.execute("DROP TABLE IF EXISTS temp.ev")
    conn.execute(f"""CREATE TEMP TABLE ev AS SELECT m.event_ticker ev, COUNT(*) n, SUM(m.result='yes') ny, MIN(m.close_ts) ct
                     FROM wh_markets m WHERE {w} AND m.event_ticker IS NOT NULL GROUP BY m.event_ticker HAVING n >= 2 AND ny = 1""", args)
    n_ev = conn.execute("SELECT COUNT(*) n FROM ev").fetchone()["n"]
    if not n_ev:
        return {"events": 0, "note": "no events with exactly one YES outcome in scope"}
    rows = conn.execute(f"""
        SELECT e.ev ev, c.end_ts ts, e.n n, COUNT(*) k,
               SUM(c.ask_c) sa, SUM(c.bid_c) sb, MIN(c.ask_c) mina, MAX(c.ask_c) maxa, MIN(c.bid_c) minb,
               SUM({TAKER} * COALESCE(s.fee_multiplier, 1) * c.ask_c * (1 - c.ask_c)) fa,
               SUM({TAKER} * COALESCE(s.fee_multiplier, 1) * c.bid_c * (1 - c.bid_c)) fb
        FROM ev e JOIN wh_markets m ON m.event_ticker = e.ev LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
        JOIN wh_candles c ON c.ticker = m.ticker AND c.period = 60 AND c.end_ts <= e.ct
        GROUP BY e.ev, c.end_ts""").fetchall()
    hours = full = 0
    res = {"buy_all_yes": {"hours": 0, "events": set(), "sum": 0.0, "max": 0.0, "gross": 0},
           "sell_all_yes": {"hours": 0, "events": set(), "sum": 0.0, "max": 0.0, "gross": 0}}
    hist = {"buy_all_yes": {}, "sell_all_yes": {}}
    for r in rows:
        hours += 1
        if r["k"] != r["n"]:
            continue                                # not every outcome had a candle that hour
        full += 1
        if r["mina"] is not None and 0 < r["mina"] and r["maxa"] < 1:        # every YES can be bought
            gross = 1 - r["sa"]
            if gross > 1e-9:
                res["buy_all_yes"]["gross"] += 1
            p = gross - r["fa"]
            if p > 1e-9:
                x = res["buy_all_yes"]
                x["hours"] += 1
                x["events"].add(r["ev"])
                x["sum"] += p
                x["max"] = max(x["max"], p)
                hist["buy_all_yes"][min(5, int(p * 100))] = hist["buy_all_yes"].get(min(5, int(p * 100)), 0) + 1
        if r["minb"] is not None and r["minb"] > 0:                          # every YES can be sold (every NO bought)
            gross = r["sb"] - 1
            if gross > 1e-9:
                res["sell_all_yes"]["gross"] += 1
            p = gross - r["fb"]
            if p > 1e-9:
                x = res["sell_all_yes"]
                x["hours"] += 1
                x["events"].add(r["ev"])
                x["sum"] += p
                x["max"] = max(x["max"], p)
                hist["sell_all_yes"][min(5, int(p * 100))] = hist["sell_all_yes"].get(min(5, int(p * 100)), 0) + 1
    out = {}
    for k, x in res.items():
        out[k] = {"hours_with_gap_after_fees": x["hours"], "hours_with_gap_before_fees": x["gross"], "events_with_gap": len(x["events"]),
                  "avg_profit_c": round(100 * x["sum"] / x["hours"], 2) if x["hours"] else None, "max_profit_c": round(100 * x["max"], 2),
                  "pct_of_hours": round(100 * x["hours"] / full, 3) if full else None,
                  "by_size_c": {("5+" if b == 5 else f"{b}-{b + 1}"): n for b, n in sorted(hist[k].items())}}
    return {"events": n_ev, "event_hours": hours, "complete_event_hours": full, **out}


# NOTE: a "per-event average" (every event counted once) was removed in 3.9.1. It is biased by construction for
# price buckets: the eventual winner's price always crosses the upper buckets and the loser's the lower ones, so
# averaging event by event shows favorites winning and longshots losing even in a perfectly fair market.


# ---------------------------------------------------------------- scalp --
EXTRA = ("scalp",)                   # run only when asked for: it applies to short contracts (15 minutes and the like)
SCALP_ENTRY_MIN = (2, 5, 10)         # minutes after the contract opens
SCALP_STOPS = (None, 0.25, 0.50)     # sell if the price falls this fraction below what was paid; None = hold to the end
SCALP_TARGET = 0.10                  # take profit at +10%
SCALP_BUCKETS = ((0.05, 0.20, "5-19c"), (0.20, 0.40, "20-39c"), (0.40, 0.60, "40-59c"), (0.60, 0.80, "60-79c"), (0.80, 0.9001, "80-90c"))
SCALP_MAKER_WAIT = 120               # seconds a resting buy order waits before giving up


def _scalp_one(tr, i0, t_entry, side, p0, res_yes, taker_rate, maker_rate):
    """One entry (one side of one contract at one moment), every variant in a single pass over the later prints.
    tr: [(ts, yes_price, taker_bought_yes)], i0: first print after the entry moment. Returns {(exec, stop): outcome}.
    Prices are per contract in dollars. Deliberately conservative: a taker pays 1c over the last print and sells 1c
    under the print that triggers a stop; a +10% target counts only once a print is 1c THROUGH it (taker exit) or a
    buyer lifts it / trades through it (resting exit)."""
    win = 1.0 if (side == "yes") == res_yes else 0.0
    fee = lambda rate, px: rate * px * (1 - px)
    out, live = {}, []
    for ex in ("taker", "maker"):
        cost = round(p0 + 0.01, 4) if ex == "taker" else round(p0 - 0.01, 4)
        if cost <= 0.0 or cost >= 0.99:
            continue
        for stop in SCALP_STOPS:
            live.append({"ex": ex, "stop": stop, "cost": cost, "in": ex == "taker", "target": math.ceil(cost * (1 + SCALP_TARGET) * 100 - 1e-9) / 100,
                         "fees": fee(taker_rate, cost) if ex == "taker" else 0.0})
    for j in range(i0, len(tr)):
        if not live:
            break
        ts, yp, tby = tr[j]
        px = yp if side == "yes" else 1 - yp
        taker_bought_side = tby == (side == "yes")
        for v in list(live):
            if not v["in"]:                                   # resting buy 1c under the last print
                if ts - t_entry > SCALP_MAKER_WAIT:
                    out[(v["ex"], v["stop"])] = {"how": "unfilled"}
                    live.remove(v)
                elif px < v["cost"] - 1e-9 or (abs(px - v["cost"]) < 1e-9 and not taker_bought_side):
                    v["in"] = True
                    v["fees"] = fee(maker_rate, v["cost"])
                continue
            if v["ex"] == "taker":
                hit = px >= v["target"] + 0.01 - 1e-9
            else:
                hit = px > v["target"] + 1e-9 or (abs(px - v["target"]) < 1e-9 and taker_bought_side)
            if hit and v["target"] < 1.0:
                f2 = fee(taker_rate if v["ex"] == "taker" else maker_rate, v["target"])
                out[(v["ex"], v["stop"])] = {"how": "target", "ret": v["target"] - v["cost"] - v["fees"] - f2, "cost": v["cost"]}
                live.remove(v)
            elif v["stop"] is not None and px <= v["cost"] * (1 - v["stop"]) + 1e-9:
                sell = max(0.01, px - 0.01)
                out[(v["ex"], v["stop"])] = {"how": "stop", "ret": sell - v["cost"] - v["fees"] - fee(taker_rate, sell), "cost": v["cost"]}
                live.remove(v)
    for v in live:
        if not v["in"]:
            out[(v["ex"], v["stop"])] = {"how": "unfilled"}
        else:
            out[(v["ex"], v["stop"])] = {"how": "held, won" if win else "held, lost", "ret": win - v["cost"] - v["fees"], "cost": v["cost"]}
    return out


def scalp(conn, flt: dict | None = None) -> dict:
    """Short contracts replayed trade by trade: buy a side a few minutes in, sell at +10%, with and without a stop."""
    import bisect
    flt = flt or {}
    w, args = _where(flt)
    mk = conn.execute(f"""SELECT m.ticker, m.series_ticker ser, m.open_ts o, m.close_ts c, m.result res, s.fee_type ft, COALESCE(s.fee_multiplier, 1) mult
                          FROM wh_markets m LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
                          WHERE {w} AND m.complete = 1 AND m.close_ts - m.open_ts BETWEEN 240 AND 1500""", args).fetchall()
    cut = conn.execute(f"SELECT COUNT(*) n FROM wh_markets m WHERE {w} AND m.complete = 2 AND m.close_ts - m.open_ts BETWEEN 240 AND 1500", args).fetchone()["n"]
    if not mk:
        return {"markets": 0, "note": "no settled short contracts (4 to 25 minutes long) with stored trades in scope", "skipped_too_busy": cut}
    cells, ser_cells, mix = {}, {}, {}
    used = 0

    def add(store, key, grp, o):
        c = store.setdefault(key, {"g": {}, "n": 0, "wins": 0, "unfilled": 0, "cost": 0.0})
        if o["how"] == "unfilled":
            c["unfilled"] += 1
            return
        g = c["g"].setdefault(grp, [0.0, 0.0])
        g[0] += 1
        g[1] += o["ret"]
        c["n"] += 1
        c["wins"] += 1 if o["ret"] > 0 else 0
        c["cost"] += o["cost"]
    for m in mk:
        rows = conn.execute("SELECT ts, yes_price yp, taker_outcome_side tos, taker_book_side tbs FROM wh_trades WHERE ticker=? AND ts<=? ORDER BY ts", (m["ticker"], m["c"])).fetchall()
        tr = [(r["ts"], r["yp"], (r["tos"] == "yes") if r["tos"] in ("yes", "no") else (r["tbs"] == "bid")) for r in rows if r["yp"] is not None and 0 < r["yp"] < 1]
        if len(tr) < 5:
            continue
        used += 1
        tss = [t[0] for t in tr]
        mrate = {"quadratic_with_maker_fees": 0.0175, "quadratic_with_combo_maker_fees": 0.035}.get(m["ft"], 0.0) * m["mult"]
        trate = TAKER * m["mult"]
        for em in SCALP_ENTRY_MIN:
            t0 = m["o"] + em * 60
            if t0 >= m["c"] - 60:
                continue
            i0 = bisect.bisect_right(tss, t0)
            if i0 == 0 or t0 - tss[i0 - 1] > 90:               # no recent print to price the entry from
                continue
            yp0 = tr[i0 - 1][1]
            for side in ("yes", "no"):
                p0 = yp0 if side == "yes" else round(1 - yp0, 4)
                b = next((lab for lo, hi, lab in SCALP_BUCKETS if lo <= p0 < hi), None)
                if not b:
                    continue
                for (ex, stop), o in _scalp_one(tr, i0, t0, side, p0, m["res"] == "yes", trate, mrate).items():
                    sk = "none" if stop is None else f"-{int(stop * 100)}%"
                    grp = int(m["c"])                          # contracts that end at the same moment are one cluster
                    add(cells, (ex, sk, em, b), grp, o)
                    add(cells, (ex, sk, "all", "all"), grp, o)
                    add(ser_cells, (m["ser"], ex, sk), grp, o)
                    if o["how"] != "unfilled":
                        mx = mix.setdefault((ex, sk), {})
                        mx[o["how"]] = mx.get(o["how"], 0) + 1

    def row(c):
        st = _stat(c["g"])
        tot = c["n"] + c["unfilled"]
        return {"entries": c["n"], "win_pct": round(100 * c["wins"] / c["n"], 1) if c["n"] else None,
                "avg_cost_c": round(100 * c["cost"] / c["n"], 1) if c["n"] else None,
                "ret_pct_of_stake": round(100 * sum(g[1] for g in c["g"].values()) / c["cost"], 2) if c["cost"] else None,
                "filled_pct": round(100 * c["n"] / tot, 1) if tot else None, "stat": st}
    order = {lab: i for i, (_, _, lab) in enumerate(SCALP_BUCKETS)}
    table = [{"exec": k[0], "stop": k[1], "entry_min": k[2], "bucket": k[3], **row(c)} for k, c in cells.items() if k[2] != "all"]
    table.sort(key=lambda x: (x["exec"] != "taker", x["stop"], x["entry_min"], order[x["bucket"]]))
    overall = [{"exec": k[0], "stop": k[1], **row(c), "ends": mix.get((k[0], k[1]), {})} for k, c in cells.items() if k[2] == "all"]
    overall.sort(key=lambda x: (x["exec"] != "taker", x["stop"]))
    by_series = [{"series": k[0], "exec": k[1], "stop": k[2], **row(c)} for k, c in ser_cells.items()]
    by_series.sort(key=lambda x: (x["series"], x["exec"] != "taker", x["stop"]))
    return {"markets": used, "skipped_too_busy": cut, "from": min(m["c"] for m in mk), "to": max(m["c"] for m in mk),
            "target_pct": int(SCALP_TARGET * 100), "entry_minutes": list(SCALP_ENTRY_MIN), "maker_wait_sec": SCALP_MAKER_WAIT,
            "overall": overall, "table": table, "by_series": by_series}


RUN = {"longshot": longshot, "calib": calib, "late": late, "baskets": baskets, "scalp": scalp}


class Studies:
    def __init__(self, wdb):
        self.db = wdb
        with wdb.lock:
            wdb.conn.executescript(SCHEMA)
        self.threads: dict[int, threading.Thread] = {}

    def _ro(self):
        conn = sqlite3.connect(f"file:{self.db.path}?mode=ro", uri=True, check_same_thread=False, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self, kind: str, filters: dict | None = None) -> int:
        if kind not in RUN:
            raise ValueError("unknown study")
        if self.db.query("SELECT 1 FROM wh_studies WHERE status='running' AND kind=? AND params=?", (kind, json.dumps(filters or {}))):
            raise RuntimeError(f"the {kind} study is already running for this scope")
        cur = self.db.execute("INSERT INTO wh_studies(ts, kind, status, params) VALUES(?,?, 'running', ?)",
                              (time.time(), kind, json.dumps(filters or {})))
        rid = cur.lastrowid
        th = threading.Thread(target=self._run, args=(rid, kind, filters or {}), daemon=True, name=f"study-{kind}")
        self.threads[rid] = th
        th.start()
        return rid

    def _run(self, rid, kind, flt):
        t0 = time.time()
        try:
            conn = self._ro()
            try:
                res = RUN[kind](conn, flt)
            finally:
                conn.close()
            self.db.execute("UPDATE wh_studies SET status='done', result=?, duration=? WHERE id=?",
                            (json.dumps(res), round(time.time() - t0, 1), rid))
        except Exception:
            self.db.execute("UPDATE wh_studies SET status='error', error=?, duration=? WHERE id=?",
                            (traceback.format_exc()[-1500:], round(time.time() - t0, 1), rid))
        finally:
            self.threads.pop(rid, None)

    def recover(self):
        """Studies that were running when the app stopped can't have finished."""
        self.db.execute("UPDATE wh_studies SET status='error', error='interrupted by a restart - run it again' WHERE status='running'")

    def latest(self, filters: dict | None = None) -> dict:
        """The newest run of each study for one scope (the same filters), default = everything stored."""
        key = json.dumps(filters or {})
        out = {}
        for k in KINDS + EXTRA:
            r = self.db.query("SELECT * FROM wh_studies WHERE kind=? AND params=? ORDER BY id DESC LIMIT 1", (k, key))
            done = self.db.query("SELECT * FROM wh_studies WHERE kind=? AND params=? AND status='done' ORDER BY id DESC LIMIT 1", (k, key))
            x = r[0] if r else None
            if x:
                x = {**x, "params": json.loads(x["params"] or "{}"), "result": json.loads(x["result"]) if x["result"] else None}
                if x["status"] != "done" and done:
                    x["previous"] = {"ts": done[0]["ts"], "result": json.loads(done[0]["result"]), "duration": done[0]["duration"]}
            out[k] = x
        return out
