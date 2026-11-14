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
SCALP_STOPS = (None, 0.25, 0.50)     # sell if the price falls this fraction below what was paid; None = no stop
SCALP_TARGETS = (0.05, 0.10, 0.20, 0.35, 0.60, None)   # take profit at +x%; None = hold to the end
SCALP_BUCKETS = ((0.05, 0.20, "5-19c"), (0.20, 0.40, "20-39c"), (0.40, 0.60, "40-59c"), (0.60, 0.80, "60-79c"), (0.80, 0.9001, "80-90c"))
SCALP_MAKER_WAIT = 120               # seconds a resting buy order waits before giving up
SCALP_MOVE = 0.05                    # "rising"/"falling": the side has moved this much since the contract's first trade
SCALP_MIN_N = 150                    # a combination needs this many trades in EACH half before it is ranked


def _tname(g):
    return "hold" if g is None else f"+{int(round(g * 100))}%"


def _sname(st):
    return "none" if st is None else f"-{int(round(st * 100))}%"


class _Acc:
    """Mean return with a standard error that treats contracts ending at the same moment as one cluster, in O(1) memory.
    Entries must arrive grouped by cluster (the replay walks contracts in closing order)."""
    __slots__ = ("n", "s", "cost", "wins", "unf", "k", "ss", "sn", "nn", "cg", "cs", "cn")

    def __init__(self):
        self.n = self.s = self.cost = self.wins = self.unf = self.k = self.ss = self.sn = self.nn = self.cs = self.cn = 0
        self.cg = None

    def _fold(self):
        if self.cn:
            self.k += 1
            self.ss += self.cs * self.cs
            self.sn += self.cs * self.cn
            self.nn += self.cn * self.cn
        self.cs = self.cn = 0

    def add(self, grp, ret, cost):
        if grp != self.cg:
            self._fold()
            self.cg = grp
        self.cs += ret
        self.cn += 1
        self.n += 1
        self.s += ret
        self.cost += cost
        self.wins += 1 if ret > 0 else 0

    def out(self):
        self._fold()
        self.cg = None
        if not self.n:
            return {"entries": 0, "unfilled": self.unf, "win_pct": None, "ret_c": None, "t": None, "on_stake_pct": None, "events": 0, "avg_cost_c": None}
        mean = self.s / self.n
        var = self.ss - 2 * mean * self.sn + mean * mean * self.nn
        se = math.sqrt(max(var, 0) * (self.k / (self.k - 1))) / self.n if self.k > 1 else None
        return {"entries": self.n, "unfilled": self.unf, "win_pct": round(100 * self.wins / self.n, 1), "ret_c": round(100 * mean, 3),
                "t": round(mean / se, 2) if se else None, "on_stake_pct": round(100 * self.s / self.cost, 2) if self.cost else None,
                "events": self.k, "avg_cost_c": round(100 * self.cost / self.n, 1),
                "filled_pct": round(100 * self.n / (self.n + self.unf), 1)}


def _scalp_paths(tr, i0, t_entry, side, p0, res_yes, taker_rate, maker_rate):
    """One entry (one side of one contract at one moment): the outcome of EVERY target x stop combination, for tap
    orders and for resting orders, in two passes over the later prints.
    tr: [(ts, yes_price, taker_bought_yes)], i0: first print after the entry moment.
    Returns {exec: None (resting buy never filled) | {(target, stop): (return, cost)}}.
    Deliberately conservative: a taker pays 1c over the last print and sells 1c under the print that triggers a stop;
    a target counts for a taker only once a print is 1c THROUGH it, for a resting sell once a buyer lifts it or the
    price trades through it."""
    win = 1.0 if (side == "yes") == res_yes else 0.0
    yes = side == "yes"
    fee = lambda rate, px: rate * px * (1 - px)
    res = {}
    for ex in ("taker", "maker"):
        cost = round(p0 + 0.01, 4) if ex == "taker" else round(p0 - 0.01, 4)
        if cost <= 0.0 or cost >= 0.99:
            continue
        j = i0
        if ex == "maker":                                    # resting buy 1c under the last print
            filled = False
            while j < len(tr):
                ts, yp, tby = tr[j]
                if ts - t_entry > SCALP_MAKER_WAIT:
                    break
                px = yp if yes else 1 - yp
                j += 1
                if px < cost - 1e-9 or (abs(px - cost) < 1e-9 and tby != yes):
                    filled = True
                    break
            if not filled:
                res[ex] = None
                continue
        f_in = fee(taker_rate if ex == "taker" else maker_rate, cost)
        tg = sorted((math.ceil(cost * (1 + g) * 100 - 1e-9) / 100, g) for g in SCALP_TARGETS if g is not None)
        tg = [(T, g) for T, g in tg if T < 1.0]
        sp = sorted(((cost * (1 - st), st) for st in SCALP_STOPS if st is not None), reverse=True)
        t_hit, s_hit = {}, {}
        ti = si = 0
        for k in range(j, len(tr)):
            if ti >= len(tg) and si >= len(sp):
                break
            ts, yp, tby = tr[k]
            px = yp if yes else 1 - yp
            if ex == "taker":
                eff = px - 0.01
            else:
                eff = px if tby == yes else px - 0.0001      # at the limit price it fills only when a buyer lifts it
            while ti < len(tg) and eff >= tg[ti][0] - 1e-9:
                t_hit[tg[ti][1]] = (k, tg[ti][0])
                ti += 1
            while si < len(sp) and px <= sp[si][0] + 1e-9:
                s_hit[sp[si][1]] = (k, max(0.01, px - 0.01))
                si += 1
        out = {}
        for g in SCALP_TARGETS:
            for st in SCALP_STOPS:
                th, sh = t_hit.get(g), s_hit.get(st)
                if th and (not sh or th[0] < sh[0]):
                    r = th[1] - cost - f_in - fee(taker_rate if ex == "taker" else maker_rate, th[1])
                elif sh:
                    r = sh[1] - cost - f_in - fee(taker_rate, sh[1])
                else:
                    r = win - cost - f_in
                out[(g, st)] = (r, cost)
        res[ex] = out
    return res


def _compress(rows):
    """Consecutive prints at the same price from the same side carry no extra information for this replay."""
    tr, last = [], None
    for r in rows:
        yp = r["yp"]
        if yp is None or not 0 < yp < 1:
            continue
        tby = (r["tos"] == "yes") if r["tos"] in ("yes", "no") else (r["tbs"] == "bid")
        if last != (yp, tby):
            tr.append((r["ts"], yp, tby))
            last = (yp, tby)
    return tr


def scalp(conn, flt: dict | None = None) -> dict:
    """Short contracts replayed trade by trade over a grid: when to buy, which price, rising or falling, which profit
    target, which stop, tap or resting orders. Winners are picked on the first half of the dates and checked on the second."""
    import bisect
    flt = flt or {}
    w, args = _where(flt)
    mk = conn.execute(f"""SELECT m.ticker, m.series_ticker ser, m.open_ts o, m.close_ts c, m.result res, s.fee_type ft, COALESCE(s.fee_multiplier, 1) mult
                          FROM wh_markets m LEFT JOIN wh_series s ON s.series_ticker = m.series_ticker
                          WHERE {w} AND m.complete = 1 AND m.close_ts - m.open_ts BETWEEN 240 AND 1500 ORDER BY m.close_ts""", args).fetchall()
    cut = conn.execute(f"SELECT COUNT(*) n FROM wh_markets m WHERE {w} AND m.complete = 2 AND m.close_ts - m.open_ts BETWEEN 240 AND 1500", args).fetchone()["n"]
    if not mk:
        return {"markets": 0, "note": "no settled short contracts (4 to 25 minutes long) with stored trades in scope", "skipped_too_busy": cut}
    mid = mk[len(mk) // 2]["c"]                                # half the contracts end before this moment
    grid, marg = {}, {}
    used = 0
    for m in mk:
        rows = conn.execute("SELECT ts, yes_price yp, taker_outcome_side tos, taker_book_side tbs FROM wh_trades WHERE ticker=? AND ts<=? ORDER BY ts", (m["ticker"], m["c"])).fetchall()
        if len(rows) < 5:
            continue
        tr = _compress(rows)
        if not tr:
            continue
        used += 1
        tss = [t[0] for t in tr]
        mrate = {"quadratic_with_maker_fees": 0.0175, "quadratic_with_combo_maker_fees": 0.035}.get(m["ft"], 0.0) * m["mult"]
        trate = TAKER * m["mult"]
        half = 0 if m["c"] < mid else 1
        grp = int(m["c"])
        yref = tr[0][1]
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
                mv = (yp0 - yref) if side == "yes" else (yref - yp0)
                sig = "rising" if mv >= SCALP_MOVE - 1e-9 else "falling" if mv <= -SCALP_MOVE + 1e-9 else "flat"
                for ex, outs in _scalp_paths(tr, i0, t0, side, p0, m["res"] == "yes", trate, mrate).items():
                    if outs is None:
                        for key in (("exit", ex, "hold", "none"), ("series", m["ser"], ex, "hold")):
                            marg.setdefault(key, [_Acc(), _Acc(), _Acc()])[2].unf += 1
                        continue
                    for (g, st), (r, cost) in outs.items():
                        tn, sn = _tname(g), _sname(st)
                        c = grid.get((ex, em, b, sig, tn, sn))
                        if c is None:
                            c = grid[(ex, em, b, sig, tn, sn)] = [_Acc(), _Acc()]
                        c[half].add(grp, r, cost)
                        keys = [("exit", ex, tn, sn)]
                        if st is None:
                            keys += [("signal", ex, sig, tn), ("price", ex, b, tn), ("minute", ex, em, tn), ("series", m["ser"], ex, tn)]
                        for key in keys:
                            a3 = marg.get(key)
                            if a3 is None:
                                a3 = marg[key] = [_Acc(), _Acc(), _Acc()]
                            a3[half].add(grp, r, cost)
                            a3[2].add(grp, r, cost)

    def tab(kind, names):
        out = []
        for key, a3 in marg.items():
            if key[0] != kind:
                continue
            out.append({**dict(zip(names, key[1:])), "all": a3[2].out(), "first_half": a3[0].out(), "second_half": a3[1].out()})
        return out
    order_t = {_tname(g): i for i, g in enumerate(SCALP_TARGETS)}
    order_s = {_sname(x): i for i, x in enumerate(SCALP_STOPS)}
    order_b = {lab: i for i, (_, _, lab) in enumerate(SCALP_BUCKETS)}
    exits = sorted(tab("exit", ("exec", "target", "stop")), key=lambda x: (x["exec"] != "taker", order_t[x["target"]], order_s[x["stop"]]))
    signals = sorted(tab("signal", ("exec", "signal", "target")), key=lambda x: (x["exec"] != "taker", x["signal"], order_t[x["target"]]))
    prices = sorted(tab("price", ("exec", "bucket", "target")), key=lambda x: (x["exec"] != "taker", order_b[x["bucket"]], order_t[x["target"]]))
    minutes = sorted(tab("minute", ("exec", "entry_min", "target")), key=lambda x: (x["exec"] != "taker", x["entry_min"], order_t[x["target"]]))
    series = sorted(tab("series", ("series", "exec", "target")), key=lambda x: (x["series"], x["exec"] != "taker", order_t[x["target"]]))
    # the search: rank on the first half only, then look at the second half
    cells = []
    for key, (h1, h2) in grid.items():
        a, b2 = h1.out(), h2.out()
        if a["entries"] >= SCALP_MIN_N and b2["entries"] >= SCALP_MIN_N:
            cells.append({"exec": key[0], "entry_min": key[1], "bucket": key[2], "signal": key[3], "target": key[4], "stop": key[5], "first_half": a, "second_half": b2})
    pos1 = [c for c in cells if c["first_half"]["ret_c"] > 0]
    held = [c for c in pos1 if c["second_half"]["ret_c"] > 0]
    strong = [c for c in pos1 if (c["first_half"]["t"] or 0) >= 2]
    strong_held = [c for c in strong if c["second_half"]["ret_c"] > 0 and (c["second_half"]["t"] or 0) >= 2]
    top = sorted(cells, key=lambda c: -(c["first_half"]["on_stake_pct"] or -1e9))[:25]
    return {"markets": used, "skipped_too_busy": cut, "from": mk[0]["c"], "to": mk[-1]["c"], "split_at": mid,
            "entry_minutes": list(SCALP_ENTRY_MIN), "maker_wait_sec": SCALP_MAKER_WAIT, "move_c": int(SCALP_MOVE * 100), "min_n": SCALP_MIN_N,
            "exits": exits, "signals": signals, "prices": prices, "minutes": minutes, "by_series": series,
            "search": {"combinations_tested": len(cells), "positive_first_half": len(pos1), "of_those_positive_second_half": len(held),
                       "strong_first_half": len(strong), "of_those_strong_second_half": len(strong_held),
                       "top_by_first_half": top, "confirmed": sorted(strong_held, key=lambda c: -(c["second_half"]["on_stake_pct"] or 0))[:25]}}


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
