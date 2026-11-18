"""Hourly gold fair value: were Kalshi's hourly gold prices ever wrong by more than the fee?

Kalshi settles KXGOLDH on Pyth's 1OZGOLD/USD price (the close of the 1-minute candle ending at the hour). Pyth's history
needs a paid key, so it is rebuilt from COMEX gold futures 1-minute closes (free, Yahoo Finance) minus an offset learned
from Kalshi's own published settlement values (futures sit a steady ~$2.60 above Pyth for the December contract).

The model: with gold at S, strike K, t minutes left and sigma = typical one-minute move (from the PREVIOUS two hours only),
P(finish above K) = Phi((S - K) / (sigma * sqrt(t))). It is compared with the prices Kalshi takers ACTUALLY paid (every
stored trade: the taker's side and price), so fills are real, not assumed. Gold is read one full minute late (the last
completed one-minute close), so nothing here relies on being faster than Kalshi.

Places no orders. Read-only market data."""
from __future__ import annotations

import bisect
import datetime as dt
import math
import statistics
import time

TAKER = 0.07
EDGE_BUCKETS = ((-1e9, -0.05, "< -5c"), (-0.05, -0.02, "-5 to -2c"), (-0.02, 0.0, "-2 to 0c"), (0.0, 0.02, "0 to 2c"),
                (0.02, 0.05, "2 to 5c"), (0.05, 0.10, "5 to 10c"), (0.10, 1e9, "10c+"))
MIN_BUCKETS = ((0, 5, "0-5 min"), (5, 15, "5-15 min"), (15, 30, "15-30 min"), (30, 61, "30-60 min"))
THRESHOLDS = (0.02, 0.05, 0.10)
SIGMA_WIN = 120          # one-minute moves used for sigma (the two hours BEFORE the moment)
OFFSET_N = 24            # settlements (before the moment) whose median sets the futures -> Pyth offset


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Gold:
    """1-minute futures closes. bars: {start_ts: close}; a bar starting at s closes at s + 60."""
    def __init__(self, bars: dict):
        self.bars = {int(k): float(v) for k, v in bars.items() if v is not None}
        self.starts = sorted(self.bars)

    def at(self, t: float, max_age: int = 180):
        """The last close completed at or before t (and no older than max_age seconds), else None."""
        i = bisect.bisect_right(self.starts, t - 60) - 1
        if i < 0:
            return None
        s = self.starts[i]
        return self.bars[s] if t - (s + 60) <= max_age else None

    def sigma(self, t: float, win: int = SIGMA_WIN):
        """Std of one-minute changes over the window ending at t (consecutive bars only), dollars per sqrt(minute)."""
        i = bisect.bisect_right(self.starts, t - 60)
        lo = max(0, i - win - 1)
        ch = [self.bars[b] - self.bars[a] for a, b in zip(self.starts[lo:i], self.starts[lo + 1:i]) if b - a == 60]
        return statistics.pstdev(ch) if len(ch) >= 30 else None


def offsets(gold: Gold, settles: dict) -> list:
    """[(close_ts, futures - Pyth)] for every settlement the futures data covers."""
    out = []
    for c, v in sorted(settles.items()):
        f = gold.bars.get(int(c) - 60)
        if f is not None and v:
            out.append((float(c), f - float(v)))
    return out


def offset_at(offs: list, t: float, n: int = OFFSET_N):
    i = bisect.bisect_left([c for c, _ in offs], t)          # strictly before t: no peeking at this hour's settlement
    prev = [o for _, o in offs[max(0, i - n):i]]
    return statistics.median(prev) if len(prev) >= 3 else None


def _bucket(x, table):
    return next((lab for lo, hi, lab in table if lo <= x < hi), table[-1][2])


def core(markets: list, trades: dict, gold: Gold, settles: dict, Acc) -> dict:
    """markets: [{ticker, close, strike, result}], trades: {ticker: [(ts, yes_price, taker_side)]}, settles: {close: value}.
    Acc: the clustered accumulator class (studies._Acc)."""
    offs = offsets(gold, settles)
    if len(offs) < 4:
        return {"trades": 0, "note": "too few Kalshi settlements overlap the gold futures data to learn the offset", "offsets": len(offs)}
    closes = sorted({m["close"] for m in markets})
    mid = closes[len(closes) // 2] if closes else 0
    by_edge, by_min, strat, calib = {}, {}, {}, {}
    used = skipped = 0
    resid = []
    for c, o in offs:                                          # how well futures-minus-offset reproduces Pyth (out of sample)
        oo = offset_at(offs, c)
        if oo is not None:
            resid.append(o - oo)
    for m in sorted(markets, key=lambda m: m["close"]):
        if m["result"] not in ("yes", "no") or m["strike"] is None:
            continue
        first = {}
        for ts, yp, side in trades.get(m["ticker"], []):
            tau = (m["close"] - ts) / 60
            if tau < 0.5 or tau > 60 or side not in ("yes", "no") or not 0 < yp < 1:
                continue
            f, sg, off = gold.at(ts), gold.sigma(ts), offset_at(offs, ts)
            if f is None or not sg or off is None:
                skipped += 1
                continue
            S = f - off
            p_yes = phi((S - m["strike"]) / (sg * math.sqrt(tau)))
            price = yp if side == "yes" else 1 - yp
            q = p_yes if side == "yes" else 1 - p_yes
            fee = TAKER * price * (1 - price)
            edge = q - price - fee
            ret = (1.0 if m["result"] == side else 0.0) - price - fee
            half = 0 if m["close"] < mid else 1
            g = int(m["close"])
            used += 1
            for key, tab in ((_bucket(edge, EDGE_BUCKETS), by_edge),):
                a = tab.setdefault(key, [Acc(), Acc(), Acc()])
                a[half].add(g, ret, price)
                a[2].add(g, ret, price)
            if edge >= 0.02:
                a = by_min.setdefault(_bucket(tau, MIN_BUCKETS), [Acc(), Acc(), Acc()])
                a[half].add(g, ret, price)
                a[2].add(g, ret, price)
            cb = min(9, int(p_yes * 10))
            c2 = calib.setdefault(cb, [0, 0, 0.0])
            c2[0] += 1
            c2[1] += 1 if m["result"] == "yes" else 0
            c2[2] += p_yes
            for th in THRESHOLDS:                              # a strategy: the FIRST trade per contract and side clearing the bar
                if edge >= th and (th, side) not in first:
                    first[(th, side)] = (g, ret, price, half)
        for (th, side), (g, ret, price, half) in first.items():
            a = strat.setdefault(th, [Acc(), Acc(), Acc()])
            a[half].add(g, ret, price)
            a[2].add(g, ret, price)

    def row(a):
        return {"all": a[2].out(), "first_half": a[0].out(), "second_half": a[1].out()}
    order_e = {lab: i for i, (_, _, lab) in enumerate(EDGE_BUCKETS)}
    order_m = {lab: i for i, (_, _, lab) in enumerate(MIN_BUCKETS)}
    return {"trades": used, "skipped_no_gold_data": skipped, "markets": len(markets), "hours": len(closes),
            "from": closes[0] if closes else None, "to": closes[-1] if closes else None, "split_at": mid,
            "offset": {"settlements_matched": len(offs), "median": round(statistics.median([o for _, o in offs]), 3),
                       "tracking_error_usd": round(statistics.pstdev(resid), 3) if len(resid) > 1 else None,
                       "max_miss_usd": round(max(abs(x) for x in resid), 3) if resid else None},
            "by_edge": sorted(({"edge": k, **row(v)} for k, v in by_edge.items()), key=lambda x: order_e[x["edge"]]),
            "edge2_by_minutes_left": sorted(({"minutes": k, **row(v)} for k, v in by_min.items()), key=lambda x: order_m[x["minutes"]]),
            "strategy_first_trade": [{"min_edge_c": int(th * 100), **row(strat[th])} for th in THRESHOLDS if th in strat],
            "calibration": [{"model_pct": f"{10 * b}-{10 * b + 10}%", "n": v[0], "model_avg_pct": round(100 * v[2] / v[0], 1),
                             "actual_yes_pct": round(100 * v[1] / v[0], 1)} for b, v in sorted(calib.items())]}


# ------------------------------------------------------------------ data --
def fetch_futures(session, symbol: str, start: float, end: float) -> dict:
    """Yahoo Finance 1-minute closes, in chunks of 7 days (Yahoo keeps about 30 days of 1-minute data)."""
    bars = {}
    t = int(start)
    while t < end:
        t2 = int(min(end, t + 7 * 86400))
        r = session.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                        params={"interval": "1m", "period1": t, "period2": t2},
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if res:
            ts = res[0].get("timestamp") or []
            cl = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            for a, b in zip(ts, cl):
                if b is not None:
                    bars[int(a)] = float(b)
        t = t2
        time.sleep(0.5)
    return bars


def fetch_settles(client, series: str, since: float) -> dict:
    """{close_ts: settlement value} from Kalshi's public settled markets (one value per hour)."""
    out = {}
    for m in client.paginate("/markets", "markets", {"series_ticker": series, "status": "settled", "limit": 1000,
                                                      "min_close_ts": int(since)}, max_items=40000):
        v = m.get("expiration_value")
        c = m.get("close_time")
        if v not in (None, "") and c:
            try:
                out[dt.datetime.fromisoformat(c.replace("Z", "+00:00")).timestamp()] = float(v)
            except ValueError:
                pass
    return out
