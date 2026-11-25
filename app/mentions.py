"""Mentions markets, paper test: would a resting order at the BACK of the queue have made money?

The trade study (185 events, Aug-Nov 2026) says people who buy at the asking price in "Mentions" markets (will a speaker
say a word) overpay by 1-2c, mostly in the day before the event, and the resting orders they hit collect it. Those are
averages over everyone already resting; a newcomer joins the back of the queue and is filled more often when the price
is about to move against him. This measures exactly that, at no risk:

  1. Once an hour: every open Mentions market closing in 1 to 24 hours that has not been quoted yet gets one virtual
     resting bid per side, AT the current best bid (never improving it), 10 contracts, behind everything already
     showing at that price (the displayed size is recorded as the queue ahead).
  2. Nothing is polled while it rests. After the market settles, its real trades since the order was placed are read
     once and replayed: a taker trade at our price first uses up the queue ahead, then fills us; a trade THROUGH our
     price fills us completely. Orders ahead that were cancelled are still counted as ahead (so fills are understated),
     and the bid is never moved. Unfilled size is cancelled one hour before the close.
  3. Filled contracts are held to settlement: payout - price - maker fee (the series' real fee type).
Read-only public market data. Places no orders."""
from __future__ import annotations

import datetime as dt
import threading
import time

from .fees import fee as kalshi_fee
from .mm import book_levels, series_fees
from .sports import ensure_schema

SIZE = 10
WINDOW_LO_H, WINDOW_HI_H = 1, 24
PRICE_LO, PRICE_HI = 0.03, 0.97
MAX_NEW_PER_SCAN = 300
MAX_SETTLE_PER_PASS = 80
MAX_SERIES = 800                   # Mentions series walked in each discovery (one request each)
WALK_H = 4                         # hours between full walks: speech markets are often listed only hours ahead
EPS = 1e-9

SCHEMA = """
CREATE TABLE IF NOT EXISTS men_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, event TEXT, series TEXT, side TEXT,
    price REAL, queue_ahead REAL, size REAL, close_ts REAL, cancel_ts REAL, status TEXT DEFAULT 'resting', filled REAL DEFAULT 0,
    result TEXT, fee REAL, pnl REAL, settled_ts REAL, UNIQUE(ticker, side));
CREATE INDEX IF NOT EXISTS men_orders_status ON men_orders(status, close_ts);
"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def end_time(m: dict):
    """When a market is expected to be decided: the earlier of its expected expiration and its scheduled close (Mentions
    markets keep a late scheduled close and shut early once the speech is over)."""
    xs = [x for x in (_ts(m.get("expected_expiration_time")), _ts(m.get("close_time"))) if x]
    return min(xs) if xs else None


def is_mention(ticker: str) -> bool:
    return "MENTION" in (ticker or "").split("-")[0].upper()


def taker_side(tr: dict) -> str | None:
    """Which outcome the taker BOUGHT ('yes' lifts the YES ask = hits a resting NO bid)."""
    s = tr.get("taker_outcome_side") or tr.get("taker_side")
    if s in ("yes", "no"):
        return s
    b = tr.get("taker_book_side")
    return "yes" if b == "bid" else "no" if b == "ask" else None


def replay(side: str, price: float, queue_ahead: float, size: float, placed: float, cancel: float, trades: list[dict]) -> float:
    """Contracts of our resting bid that real trades would have filled (see the module note)."""
    level = price if side == "yes" else 1 - price               # our bid as a YES price
    ahead, filled = float(queue_ahead or 0), 0.0
    for tr in sorted(trades, key=lambda t: t["ts"]):
        if tr["ts"] <= placed or tr["ts"] >= cancel or filled >= size:
            continue
        yp, n, who = tr["yes_price"], tr["count"], tr["taker"]
        if yp is None or not n or who is None or who == side:   # a taker buying OUR side trades with the other book
            continue
        through = yp < level - EPS if side == "yes" else yp > level + EPS
        if through:                                             # the market traded past our price: the whole level was taken
            return float(size)
        if abs(yp - level) <= EPS:
            use = min(ahead, n)
            ahead -= use
            filled += min(size - filled, n - use)
    return round(filled, 4)


class MentionsPaper:
    TABLE = "men_orders"
    CATEGORY = "Mentions"

    def __init__(self, cfg_getter, wdb, kalshi_client):
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client                 # public production market data only
        ensure_schema(wdb, SCHEMA.replace("men_orders", self.TABLE))
        self._stop = threading.Event()
        self._fees: dict = {}
        self._series = (0.0, [], False)            # (when listed, tickers, True while a full walk is pending)
        self.state = {"running": False, "last": None, "error": None, "note": ""}

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    # ------------------------------------------------------------- placing --
    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        have = {r["ticker"] for r in self.db.query(f"SELECT DISTINCT ticker FROM {self.TABLE}")}
        new, seen, ends, live = {}, 0, [], set()
        # One call per Mentions series lists its open events with their markets; walking ALL markets closing in a day
        # (thousands of crypto and currency ladders) never reached them and cost dozens of requests an hour.
        for ser in self.series(now):
            for e in self.k.paginate("/events", "events", {"series_ticker": ser, "status": "open", "with_nested_markets": "true", "limit": 200}, max_items=400):
                for m in e.get("markets") or []:
                    t = m.get("ticker")
                    c = end_time(m)
                    seen += 1
                    live.add(ser)
                    if c is not None and c > now:
                        ends.append((c, t))
                    if not t or t in have or t in new or (m.get("market_type") or "binary") != "binary" or c is None:
                        continue
                    if (m.get("status") or "active") not in ("active", "open") or not now + WINDOW_LO_H * 3600 <= c <= now + WINDOW_HI_H * 3600:
                        continue
                    new[t] = ({**m, "event_ticker": m.get("event_ticker") or e.get("event_ticker")}, c)
            if len(new) >= MAX_NEW_PER_SCAN:
                break
        if self._series[2]:                                     # that was the full walk of every series: remember which have open markets
            self._series = (self._series[0], sorted(live) or self._series[1], False)
        self.state["markets_seen"] = seen
        self.state["open_series"] = len(live)
        self.state["soonest"] = [{"ticker": t, "ends_in_h": round((c - now) / 3600, 1)} for c, t in sorted(ends)[:5]]
        placed = 0
        size = float(self.opt("mentions_size", SIZE))
        books = self.k.orderbooks(list(new)) if new else {}
        for t, (m, c) in new.items():
            ob = books.get(t) or {}
            for side, raw in (("yes", ob.get("yes_dollars")), ("no", ob.get("no_dollars"))):
                lv = book_levels(raw)
                if not lv:
                    continue
                p, q = lv[0]
                if not PRICE_LO - EPS <= p <= PRICE_HI + EPS:
                    continue
                cur = self.db.execute(f"INSERT OR IGNORE INTO {self.TABLE}(ts, ticker, event, series, side, price, queue_ahead, size, close_ts, cancel_ts) "
                                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (now, t, m.get("event_ticker"), t.split("-")[0], side, round(p, 4), q, size, c, c - WINDOW_LO_H * 3600))
                placed += cur.rowcount if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        done = self.settle(now)
        self.state.update(last=now, error=None, note=f"{len(live)} {self.CATEGORY} series with open markets ({seen} markets); {len(new)} new ones ending in {WINDOW_LO_H}-{WINDOW_HI_H} h, "
                                                     f"{placed} resting bids placed on paper, {done} settled")
        return {"markets": len(new), "placed": placed, "settled": done}

    def series(self, now: float) -> list[str]:
        """The Mentions series to look at. Every WALK_H hours EVERY series of the category is walked (about 500 requests)
        and the ones with open markets are kept; the hourly scans in between look only at those. (A daily walk missed
        series whose markets are listed the same day they end.) (Picking the busiest by
        lifetime volume missed most of the live ones: the big names had no open event at all.)"""
        if now - self._series[0] > float(self.opt("mentions_walk_h", WALK_H)) * 3600 or not self._series[1]:
            ss = [s["ticker"] for s in (self.k.series_list(self.CATEGORY) or []) if s.get("ticker")]
            self._series = (now, ss[:MAX_SERIES], True)
        self.state["series"] = len(self._series[1])
        return self._series[1]

    # ------------------------------------------------------------ settling --
    def _fee_info(self, series: str) -> dict:
        if series not in self._fees:
            self._fees[series] = series_fees(self.k, series)
        return self._fees[series]

    def _trades(self, ticker: str, since: float) -> list[dict]:
        out = []
        for tr in self.k.paginate("/markets/trades", "trades", {"ticker": ticker, "min_ts": int(since) - 1, "limit": 1000}, max_items=20000):
            ts = _ts(tr.get("created_time"))
            if ts is None:
                continue
            out.append({"ts": ts, "yes_price": fnum(tr.get("yes_price_dollars")), "count": fnum(tr.get("count_fp") or tr.get("count")) or 0.0,
                        "taker": taker_side(tr)})
        return out

    def settle(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.query(f"SELECT * FROM {self.TABLE} WHERE status='resting' AND close_ts<? ORDER BY close_ts", (now - 120,))
        tickers = list(dict.fromkeys(r["ticker"] for r in rows))[:MAX_SETTLE_PER_PASS]
        if not tickers:
            return 0
        res = {}
        for i in range(0, len(tickers), 100):
            for m in self.k.markets(status=None, tickers=tickers[i:i + 100], max_items=200):
                res[m["ticker"]] = (m.get("result") or "").lower()
        n = 0
        for t in tickers:
            x = res.get(t)
            mine = [r for r in rows if r["ticker"] == t]
            if x not in ("yes", "no"):
                if x in ("void", "scalar") or now - mine[0]["close_ts"] > 7 * 86400:       # never settled yes/no: drop it
                    self.db.execute(f"UPDATE {self.TABLE} SET status='void', settled_ts=? WHERE ticker=? AND status='resting'", (now, t))
                continue
            trades = self._trades(t, min(r["ts"] for r in mine))
            fi = self._fee_info(mine[0]["series"])
            for r in mine:
                f = replay(r["side"], r["price"], r["queue_ahead"], r["size"], r["ts"], r["cancel_ts"], trades)
                if f <= 0:
                    self.db.execute(f"UPDATE {self.TABLE} SET status='unfilled', filled=0, result=?, settled_ts=? WHERE id=?", (x, now, r["id"]))
                else:
                    fee = float(kalshi_fee(r["price"], f, fi.get("fee_type"), fi.get("fee_multiplier") or 1, maker=True))
                    pnl = f * ((1.0 if r["side"] == x else 0.0) - r["price"]) - fee
                    self.db.execute(f"UPDATE {self.TABLE} SET status='filled', filled=?, result=?, fee=?, pnl=?, settled_ts=? WHERE id=?",
                                    (f, x, round(fee, 4), round(pnl, 4), now, r["id"]))
                n += 1
        return n

    # ---------------------------------------------------------------- loop --
    def loop(self):
        self._stop.wait(240)
        while not self._stop.is_set():
            if self.opt("mentions_enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("mentions_every_min", 60)) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name=self.TABLE).start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        q = self.db.query
        tot = q("SELECT COUNT(*) n, COALESCE(SUM(status='resting'), 0) resting, COALESCE(SUM(status='unfilled'), 0) unfilled, COALESCE(SUM(status='filled'), 0) filled, "
                "COALESCE(SUM(CASE WHEN status='filled' THEN filled END), 0) contracts, COALESCE(SUM(pnl), 0) pnl, "
                "COALESCE(SUM(CASE WHEN status='filled' THEN filled * price END), 0) staked, COALESCE(SUM(fee), 0) fees, "
                "COALESCE(SUM(status='filled' AND pnl > 0), 0) won, COUNT(DISTINCT CASE WHEN status='filled' THEN event END) events "
                f"FROM {self.TABLE}")[0]
        by = q("SELECT side, CASE WHEN price < 0.10 THEN 'under 10c' WHEN price < 0.40 THEN '10-39c' WHEN price < 0.70 THEN '40-69c' ELSE '70c+' END band, "
               "COUNT(*) orders, SUM(status='filled') filled, COALESCE(SUM(pnl), 0) pnl, COALESCE(SUM(CASE WHEN status='filled' THEN filled * price END), 0) staked "
               f"FROM {self.TABLE} WHERE status IN ('filled', 'unfilled') GROUP BY side, band ORDER BY side, MIN(price)")
        return {"state": self.state, "size": float(self.opt("mentions_size", SIZE)), "window_h": [WINDOW_LO_H, WINDOW_HI_H], "totals": tot, "by": by,
                "recent": q(f"SELECT ts, ticker, side, price, queue_ahead, size, status, filled, result, pnl, close_ts FROM {self.TABLE} "
                            "ORDER BY CASE status WHEN 'resting' THEN 1 ELSE 0 END, settled_ts DESC, ts DESC LIMIT 60")}
