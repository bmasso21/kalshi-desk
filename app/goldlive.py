"""Live hourly gold fair value (read-only; places no orders).

Gold right now = PAXG/USD on Kraken (tokenized gold, real-time, free, no key) minus an offset learned from Kalshi's own
published settlements (Kalshi settles on Pyth 1OZGOLD/USD; Pyth's live feed now needs a key, and free futures quotes are
10 minutes late). PAXG tracks Kalshi's settlement price to roughly +-$1.6 per reading, noisier than futures (+-$0.7),
so the live model is a little blunter than the backtested one.

Used two ways: a board of every strike for the next close (fair chance vs Kalshi's real bid/ask, with overpriced
flags) to check a trade before placing it by hand, and the paper rule in the cushion watch."""
from __future__ import annotations

import datetime as dt
import math
import statistics
import threading
import time

from .goldfv import phi

TAKER = 0.07
KRAKEN = "https://api.kraken.com/0/public/OHLC"


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _iso(v):
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class GoldLive:
    def __init__(self, kalshi_client, session=None, pair: str = "PAXGUSD", series: str = "KXGOLDH"):
        import requests
        self.k = kalshi_client                    # public production market data only
        self.http = session or requests.Session()
        self.pair, self.series = pair, series
        self.lock = threading.Lock()
        self._bars = (0.0, [])                    # (fetched_at, [(start, close)])
        self._off = (0.0, None, 0)                # (computed_at, offset, n)

    # -------------------------------------------------------------- gold --
    def bars(self, now: float | None = None) -> list:
        """Completed one-minute PAXG closes [(start, close)] (Kraken keeps the last 720), cached 15 s."""
        now = now or time.time()
        with self.lock:
            if now - self._bars[0] < 15 and self._bars[1]:
                return self._bars[1]
        r = self.http.get(KRAKEN, params={"pair": self.pair, "interval": 1}, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"Kraken: {j['error']}")
        res = j.get("result") or {}
        rows = next((v for k, v in res.items() if k != "last"), [])
        out = [(int(x[0]), float(x[4])) for x in rows if int(x[0]) + 60 <= now]     # the running minute is not complete
        with self.lock:
            self._bars = (now, out)
        return out

    @staticmethod
    def sigma(bars: list, win: int = 120):
        ch = [b[1] - a[1] for a, b in zip(bars[-win - 1:-1], bars[-win:]) if b[0] - a[0] == 60]
        return statistics.pstdev(ch) if len(ch) >= 30 else None

    def offset(self, now: float | None = None):
        """median(PAXG close at the settling minute - Kalshi's settlement value) over the hours PAXG data covers. Cached 10 min."""
        now = now or time.time()
        with self.lock:
            if now - self._off[0] < 600 and self._off[1] is not None:
                return self._off[1], self._off[2]
        bars = dict(self.bars(now))
        diffs = {}
        for m in self.k.paginate("/markets", "markets", {"series_ticker": self.series, "status": "settled", "limit": 1000,
                                                         "min_close_ts": int(now - 13 * 3600)}, max_items=3000):
            c, v = _iso(m.get("close_time")), fnum(m.get("expiration_value"))
            if c and v and c not in diffs and int(c) - 60 in bars:
                diffs[c] = bars[int(c) - 60] - v
        off = statistics.median(diffs.values()) if len(diffs) >= 3 else None
        with self.lock:
            self._off = (now, off, len(diffs))
        return off, len(diffs)

    # ------------------------------------------------------------- board --
    def board(self, close_ts: float | None = None, now: float | None = None) -> dict:
        now = now or time.time()
        close_ts = close_ts or math.ceil(now / 3600) * 3600
        bars = self.bars(now)
        off, n_off = self.offset(now)
        sg = self.sigma(bars)
        last = bars[-1] if bars else None
        out = {"close": close_ts, "minutes_left": round((close_ts - now) / 60, 1), "paxg": last[1] if last else None,
               "paxg_age_sec": round(now - last[0] - 60) if last else None, "offset": round(off, 2) if off is not None else None,
               "offset_from_hours": n_off, "sigma_per_min": round(sg, 3) if sg else None, "rows": []}
        if not last or off is None or not sg or now - last[0] - 60 > 180:
            out["note"] = "no usable gold price right now (market closed, Kraken stale, or too few settlements to learn the offset)"
            return out
        S = last[1] - off
        tau = max((close_ts - now) / 60, 0.5)
        out["gold"] = round(S, 2)
        mk = [m for m in self.k.markets(status="open", series_ticker=self.series, max_items=500) if abs((_iso(m.get("close_time")) or 0) - close_ts) < 90]
        for m in mk:
            if (m.get("strike_type") or "").lower() not in ("greater", "greater_or_equal"):
                continue
            k = fnum(m.get("floor_strike"))
            yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
            if k is None or yb is None or ya is None:
                continue
            p = phi((S - k) / (sg * math.sqrt(tau)))
            row = {"ticker": m.get("ticker"), "strike": k, "fair_yes": round(p, 4), "yes_bid": yb, "yes_ask": ya}
            for side, ask, q in (("yes", ya, p), ("no", round(1 - yb, 4) if yb > 0 else None, 1 - p)):
                if ask is None or not 0 < ask < 1:
                    row[f"{side}_edge"] = None
                    continue
                row[f"{side}_ask"] = ask
                row[f"{side}_edge"] = round(q - ask - TAKER * ask * (1 - ask), 4)
            out["rows"].append(row)
        out["rows"].sort(key=lambda r: r["strike"])
        return out
