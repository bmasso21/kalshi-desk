"""Gas prices: were Kalshi's AAA gas contracts ever wrong given what AAA had already published?

Kalshi's gas ladders (KXAAAGASD daily, KXAAAGASW weekly, KXAAAGASM monthly) settle on AAA's national average for regular
gas on one named day: "strictly greater than $X on <date>". AAA publishes one figure a day and the figure moves slowly
and in streaks, so near the settlement day most of the answer is already public.

The history of AAA's daily figure is rebuilt from Kalshi's own settlement values (each settled contract carries the AAA
figure it settled on). A day's figure is treated as public from 8:00 a.m. Eastern that day (when the daily contracts
for the next day open); before that only the previous day's is used.

Two simple models, both using only figures already public at the moment of each trade:
  no trend : the settlement figure = the latest public figure, +- how far the figure has typically moved in that many days
  trend    : the latest public figure + (days left) x (average of the last 3 daily changes), +- that model's own past misses
P(above X) = Phi((expected - X) / typical miss). Compared with the prices takers ACTUALLY paid (stored trades), fees included.

What this does NOT use: wholesale gasoline futures or live pump-price trackers, which sharper traders will have.
Places no orders. Stored market data only."""
from __future__ import annotations

import datetime as dt
import math
from .goldfv import EDGE_BUCKETS, THRESHOLDS, _bucket, phi

TAKER = 0.07
KNOWN_HOUR = 8            # a day's AAA figure counts as public from 8:00 ET that day
TREND_N = 3               # daily changes averaged for the trend
ERR_WIN = 45              # days of past misses used to size the uncertainty
MIN_ERRS = 5
MAX_GAP = 3               # a missing day or two is bridged; more and the trade is skipped
SIGMA_FLOOR = 0.002       # $: AAA figures are published to a tenth of a cent
DAY_BUCKETS = ((1, 2, "1 day"), (2, 3, "2 days"), (3, 5, "3-4 days"), (5, 11, "5-10 days"), (11, 10 ** 6, "11+ days"))
MODELS = ("no_trend", "trend")


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    return d + dt.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))


def et(ts: float) -> dt.datetime:
    """US Eastern wall-clock time, worked out by the rule (second Sunday of March to first Sunday of November is
    daylight time) so it needs no time-zone database (Windows ships none with Python)."""
    u = dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None)
    start = dt.datetime.combine(_nth_sunday(u.year, 3, 2), dt.time(7))       # 2:00 EST = 07:00 UTC
    end = dt.datetime.combine(_nth_sunday(u.year, 11, 1), dt.time(6))        # 2:00 EDT = 06:00 UTC
    return u - dt.timedelta(hours=4 if start <= u < end else 5)


def value_date(close_ts: float) -> dt.date:
    """The day whose AAA figure settles a contract: contracts close at 11:59 p.m. ET the evening before."""
    return et(close_ts + 60).date()


def known_date(ts: float) -> dt.date:
    """The newest day whose AAA figure is public at ts."""
    d = et(ts)
    return d.date() if d.hour >= KNOWN_HOUR else d.date() - dt.timedelta(days=1)


class Gas:
    """AAA's daily figure by date."""
    def __init__(self, values: dict):
        self.v = {d: float(x) for d, x in values.items() if x is not None}

    def latest(self, d: dt.date):
        """(date, figure) of the newest figure on or before d, at most MAX_GAP days old."""
        for k in range(MAX_GAP + 1):
            x = d - dt.timedelta(days=k)
            if x in self.v:
                return x, self.v[x]
        return None, None

    def drift(self, L: dt.date, trend: bool):
        """Average of the last TREND_N daily changes ending at L (0 without the trend); None if those days are missing."""
        if not trend:
            return 0.0
        ch = []
        for k in range(TREND_N):
            a, b = self.v.get(L - dt.timedelta(days=k + 1)), self.v.get(L - dt.timedelta(days=k))
            if a is None or b is None:
                return None
            ch.append(b - a)
        return sum(ch) / len(ch)

    def forecast(self, L: dt.date, h: int, trend: bool):
        """Expected figure h days after L and the typical miss of that same forecast over the ERR_WIN days up to L
        (every past forecast is scored only against figures public by L)."""
        base, dr = self.v.get(L), self.drift(L, trend)
        if base is None or dr is None or h < 1:
            return None, None
        errs = []
        for k in range(ERR_WIN):
            tgt = L - dt.timedelta(days=k)
            org = tgt - dt.timedelta(days=h)
            if tgt not in self.v or org not in self.v:
                continue
            d0 = self.drift(org, trend)
            if d0 is None:
                continue
            errs.append(self.v[tgt] - (self.v[org] + h * d0))
        if len(errs) < MIN_ERRS:
            return None, None
        sd = math.sqrt(sum(e * e for e in errs) / len(errs))
        return base + h * dr, max(sd, SIGMA_FLOOR)


def core(markets: list, trades: dict, gas: Gas, Acc) -> dict:
    """markets: [{ticker, close, strike, result}], trades: {ticker: [(ts, yes_price, taker_side)]}."""
    closes = sorted({m["close"] for m in markets})
    if not closes or len(gas.v) < MIN_ERRS + TREND_N + 2:
        return {"trades": 0, "note": "too little AAA history stored yet (it is rebuilt from settled gas contracts)", "aaa_days": len(gas.v)}
    mid = closes[len(closes) // 2]
    res = {k: {"by_edge": {}, "by_days": {}, "strat": {}, "calib": {}, "used": 0} for k in MODELS}
    skipped = 0
    for m in sorted(markets, key=lambda m: m["close"]):
        if m["result"] not in ("yes", "no") or m["strike"] is None:
            continue
        D = value_date(m["close"])
        half = 0 if m["close"] < mid else 1
        g = int(m["close"])
        first = {k: {} for k in MODELS}
        for ts, yp, side in trades.get(m["ticker"], []):
            if ts >= m["close"] or side not in ("yes", "no") or not 0 < yp < 1:
                continue
            L, _ = gas.latest(known_date(ts))
            if L is None or L >= D:
                skipped += 1
                continue
            h = (D - L).days
            price = yp if side == "yes" else 1 - yp
            fee = TAKER * price * (1 - price)
            ret = (1.0 if m["result"] == side else 0.0) - price - fee
            for k in MODELS:
                mu, sd = gas.forecast(L, h, k == "trend")
                if mu is None:
                    if k == "no_trend":
                        skipped += 1
                    continue
                p_yes = phi((mu - m["strike"]) / sd)
                edge = (p_yes if side == "yes" else 1 - p_yes) - price - fee
                r = res[k]
                r["used"] += 1
                a = r["by_edge"].setdefault(_bucket(edge, EDGE_BUCKETS), [Acc(), Acc(), Acc()])
                a[half].add(g, ret, price)
                a[2].add(g, ret, price)
                if edge >= 0.02:
                    a = r["by_days"].setdefault(_bucket(h, DAY_BUCKETS), [Acc(), Acc(), Acc()])
                    a[half].add(g, ret, price)
                    a[2].add(g, ret, price)
                cb = min(9, int(p_yes * 10))
                c2 = r["calib"].setdefault(cb, [0, 0, 0.0])
                c2[0] += 1
                c2[1] += 1 if m["result"] == "yes" else 0
                c2[2] += p_yes
                for th in THRESHOLDS:                          # the rule: the FIRST trade per contract and side clearing the bar
                    if edge >= th and (th, side) not in first[k]:
                        first[k][(th, side)] = (ret, price)
        for k in MODELS:
            for (th, side), (ret, price) in first[k].items():
                a = res[k]["strat"].setdefault(th, [Acc(), Acc(), Acc()])
                a[half].add(g, ret, price)
                a[2].add(g, ret, price)

    def row(a):
        return {"all": a[2].out(), "first_half": a[0].out(), "second_half": a[1].out()}
    oe = {lab: i for i, (_, _, lab) in enumerate(EDGE_BUCKETS)}
    od = {lab: i for i, (_, _, lab) in enumerate(DAY_BUCKETS)}
    days = sorted(gas.v)
    ch = [abs(gas.v[b] - gas.v[a]) for a, b in zip(days, days[1:]) if (b - a).days == 1]
    out = {"trades": res["no_trend"]["used"], "skipped_no_aaa_figure": skipped, "markets": len(markets), "settlement_days": len(closes),
           "from": closes[0], "to": closes[-1], "split_at": mid,
           "aaa": {"days": len(days), "first": days[0].isoformat(), "last": days[-1].isoformat(),
                   "typical_daily_move_c": round(100 * sum(ch) / len(ch), 2) if ch else None,
                   "largest_daily_move_c": round(100 * max(ch), 2) if ch else None}}
    for k in MODELS:
        r = res[k]
        out[k] = {"trades": r["used"],
                  "by_edge": sorted(({"edge": e, **row(v)} for e, v in r["by_edge"].items()), key=lambda x: oe[x["edge"]]),
                  "edge2_by_days_left": sorted(({"days": d, **row(v)} for d, v in r["by_days"].items()), key=lambda x: od[x["days"]]),
                  "strategy_first_trade": [{"min_edge_c": int(th * 100), **row(r["strat"][th])} for th in THRESHOLDS if th in r["strat"]],
                  "calibration": [{"model_pct": f"{10 * b}-{10 * b + 10}%", "n": v[0], "model_avg_pct": round(100 * v[2] / v[0], 1),
                                   "actual_yes_pct": round(100 * v[1] / v[0], 1)} for b, v in sorted(r["calib"].items())]}
    return out
