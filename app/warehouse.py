"""Data warehouse (Phase 1.4): Kalshi market data for research and backtests.

Source: Kalshi PRODUCTION public market data (no API key, no login). Demo books are nearly empty,
so backtests need real market data. Nothing here touches your account or keys and it never
places orders; trading stays on whatever environment Settings says (demo).

Stored in warehouse.db (separate from the trading ledger):
  wh_series   series -> category, fee_type, fee_multiplier (for fee replay)
  wh_markets  every market in scope: times, status, result, settlement value, volume
  wh_trades   every public trade in those markets (price, size, taker side, time)
  wh_candles  hourly candles: YES bid / YES ask / trade price OHLC, volume, open interest
  wh_ticks    top-of-book changes recorded live from the WebSocket feed (watched markets)

Kalshi splits data into live and historical tiers (GET /historical/cutoff). Markets settled before
`market_settled_ts`, and trades before `trades_created_ts`, are only on /historical/* endpoints,
which also use slightly different field names; both are normalised here.

Scope (Research tab): categories (default "Climate and Weather") filtered by tags (default
"Daily temperature" - the city high/low markets; the category alone is ~500 series of hurricanes,
ski resorts, one-offs...) + extra series,
`lookback_days` (default 120). Collection runs in the background at `req_per_sec` (default 3/s).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
import traceback
from pathlib import Path

from .kalshi_client import KalshiClient, KalshiError, TokenBucket, d

SCHEMA = """
CREATE TABLE IF NOT EXISTS wh_series (
    series_ticker TEXT PRIMARY KEY, category TEXT, title TEXT, fee_type TEXT, fee_multiplier REAL,
    frequency TEXT, raw TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS wh_markets (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, category TEXT, title TEXT, sub_title TEXT,
    market_type TEXT, status TEXT, open_ts REAL, close_ts REAL, expected_exp_ts REAL, settled_ts REAL,
    result TEXT, settlement_value REAL, volume REAL, open_interest REAL, strike_type TEXT,
    floor_strike REAL, cap_strike REAL, tier TEXT, raw TEXT, updated REAL,
    trades_synced_to REAL DEFAULT 0, candles_synced_to REAL DEFAULT 0, complete INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS wh_markets_series ON wh_markets(series_ticker, close_ts);
CREATE INDEX IF NOT EXISTS wh_markets_cat ON wh_markets(category, close_ts);
CREATE TABLE IF NOT EXISTS wh_trades (
    trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL, count REAL,
    taker_outcome_side TEXT, taker_book_side TEXT, is_block INTEGER
);
CREATE INDEX IF NOT EXISTS wh_trades_tk ON wh_trades(ticker, ts);
CREATE TABLE IF NOT EXISTS wh_candles (
    ticker TEXT, period INTEGER, end_ts INTEGER,
    bid_o REAL, bid_h REAL, bid_l REAL, bid_c REAL, ask_o REAL, ask_h REAL, ask_l REAL, ask_c REAL,
    px_o REAL, px_h REAL, px_l REAL, px_c REAL, px_mean REAL, volume REAL, open_interest REAL,
    PRIMARY KEY (ticker, period, end_ts)
);
CREATE TABLE IF NOT EXISTS wh_ticks (
    ticker TEXT, ts REAL, yes_bid REAL, yes_ask REAL, bid_size REAL, ask_size REAL
);
CREATE INDEX IF NOT EXISTS wh_ticks_tk ON wh_ticks(ticker, ts);
CREATE TABLE IF NOT EXISTS wh_state (k TEXT PRIMARY KEY, v TEXT);
"""

PROD_URL = "https://external-api.kalshi.com/trade-api/v2"


def iso_ts(x) -> float | None:
    if x in (None, ""):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def f(x) -> float | None:
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ohlc(o: dict | None, k: str):
    """Live candles use open_dollars, historical use open."""
    o = o or {}
    return f(o.get(f"{k}_dollars", o.get(k)))


class WarehouseDB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def executemany(self, sql, rows) -> int:
        with self.lock:
            cur = self.conn.executemany(sql, rows)
            self.conn.commit()
            return cur.rowcount

    def query(self, sql, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, k, default=None):
        r = self.query("SELECT v FROM wh_state WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, k, v):
        self.execute("INSERT OR REPLACE INTO wh_state VALUES(?,?)", (k, json.dumps(v)))

    # ------------------------------------------------------------ upserts --
    def upsert_series(self, s: dict, category=None):
        self.execute("INSERT OR REPLACE INTO wh_series VALUES(?,?,?,?,?,?,?,?)",
                     (s.get("ticker"), s.get("category") or category, s.get("title"), s.get("fee_type"),
                      f(s.get("fee_multiplier")) if s.get("fee_multiplier") is not None else 1.0,
                      s.get("frequency"), json.dumps(s), time.time()))

    def upsert_market(self, m: dict, series_ticker=None, category=None, tier="live"):
        t = m.get("ticker")
        old = self.query("SELECT trades_synced_to, candles_synced_to, complete FROM wh_markets WHERE ticker=?", (t,))
        keep = old[0] if old else {"trades_synced_to": 0, "candles_synced_to": 0, "complete": 0}
        sv = m.get("settlement_value_dollars")
        self.execute(
            "INSERT OR REPLACE INTO wh_markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, m.get("event_ticker"), series_ticker, category, m.get("title"), m.get("yes_sub_title") or m.get("subtitle"),
             m.get("market_type"), m.get("status"), iso_ts(m.get("open_time")), iso_ts(m.get("close_time")),
             iso_ts(m.get("expected_expiration_time")), iso_ts(m.get("settlement_ts")), m.get("result") or None,
             f(sv), f(m.get("volume_fp") or m.get("volume")), f(m.get("open_interest_fp") or m.get("open_interest")),
             m.get("strike_type"), f(m.get("floor_strike")), f(m.get("cap_strike")), tier,
             json.dumps({k: m.get(k) for k in ("rules_primary", "yes_sub_title", "no_sub_title", "subtitle",
                                               "last_price_dollars", "previous_price_dollars", "custom_strike")}),
             time.time(), keep["trades_synced_to"], keep["candles_synced_to"], keep["complete"]))

    def add_trades(self, trades: list[dict]) -> int:
        rows = [(t.get("trade_id"), t.get("ticker"), iso_ts(t.get("created_time")) or iso_ts(t.get("ts")),
                 f(t.get("yes_price_dollars")), f(t.get("count_fp") or t.get("count")),
                 t.get("taker_outcome_side") or t.get("taker_side"), t.get("taker_book_side"),
                 int(bool(t.get("is_block_trade")))) for t in trades if t.get("trade_id")]
        return max(0, self.executemany("INSERT OR IGNORE INTO wh_trades VALUES(?,?,?,?,?,?,?,?)", rows))

    def add_candles(self, ticker: str, period: int, candles: list[dict]) -> int:
        rows = []
        for c in candles:
            if c.get("end_period_ts") is None:
                continue
            yb, ya, px = c.get("yes_bid"), c.get("yes_ask"), c.get("price")
            rows.append((ticker, period, int(c["end_period_ts"]),
                         _ohlc(yb, "open"), _ohlc(yb, "high"), _ohlc(yb, "low"), _ohlc(yb, "close"),
                         _ohlc(ya, "open"), _ohlc(ya, "high"), _ohlc(ya, "low"), _ohlc(ya, "close"),
                         _ohlc(px, "open"), _ohlc(px, "high"), _ohlc(px, "low"), _ohlc(px, "close"), _ohlc(px, "mean"),
                         f(c.get("volume_fp", c.get("volume"))), f(c.get("open_interest_fp", c.get("open_interest")))))
        self.executemany("INSERT OR REPLACE INTO wh_candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def counts(self) -> dict:
        q = lambda s: self.query(s)[0]["n"]
        return {"series": q("SELECT COUNT(*) n FROM wh_series"),
                "markets": q("SELECT COUNT(*) n FROM wh_markets"),
                "settled_markets": q("SELECT COUNT(*) n FROM wh_markets WHERE result IN ('yes','no')"),
                "complete_markets": q("SELECT COUNT(*) n FROM wh_markets WHERE complete=1"),
                "trades": q("SELECT COUNT(*) n FROM wh_trades"),
                "candles": q("SELECT COUNT(*) n FROM wh_candles"),
                "ticks": q("SELECT COUNT(*) n FROM wh_ticks")}


class Warehouse:
    """Background collector. Uses its own unauthenticated client on production public data."""

    def __init__(self, engine, db: WarehouseDB, client: KalshiClient | None = None):
        self.e = engine
        self.db = db
        self.client = client or KalshiClient(engine.cfg.data.get("warehouse", {}).get("base_url_override") or PROD_URL)
        rate = float(self.cfg.get("req_per_sec", 3))
        self.client.read_bucket = TokenBucket(rate=rate * 10, capacity=max(10, rate * 10))
        self._stop = threading.Event()
        self._now = threading.Event()
        self.state = {"running": False, "phase": "idle", "last_sync": db.get("last_sync"), "error": None,
                      "progress": "", "requests": 0, "last_duration": db.get("last_duration")}
        self._last_tick: dict[str, tuple] = {}

    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.get("warehouse", {})

    # --------------------------------------------------------- live ticks --
    def on_feed(self, kind, msg):
        """Record top-of-book changes for markets the live feed is watching."""
        if kind != "book" or not self.cfg.get("record_ticks", True):
            return
        t = msg.get("ticker")
        top = self.e.feed.top(t)
        if not top:
            return
        key = (top["yes_bid"], top["yes_ask"])
        if self._last_tick.get(t) == key:
            return
        self._last_tick[t] = key
        self.db.execute("INSERT INTO wh_ticks VALUES(?,?,?,?,?,?)",
                        (t, time.time(), float(top["yes_bid"]), float(top["yes_ask"]),
                         float(top["yes_bid_size"]), float(top["no_bid_size"])))

    # ------------------------------------------------------------- scope --
    def scope_series(self) -> list[dict]:
        out, seen = [], set()
        tags = set(self.cfg.get("tags") or [])
        for cat in self.cfg.get("categories") or []:
            for s in self.client.series_list(category=cat):
                if tags and not tags & set(s.get("tags") or []):
                    continue          # category member without any of the wanted tags
                if s.get("ticker") and s["ticker"] not in seen:
                    seen.add(s["ticker"])
                    self.db.upsert_series(s, cat)
                    out.append({"ticker": s["ticker"], "category": s.get("category") or cat})
        for st in self.cfg.get("series") or []:
            if st in seen:
                continue
            try:
                s = self.client.series(st) or {"ticker": st}
            except KalshiError:
                s = {"ticker": st}
            self.db.upsert_series(s)
            seen.add(st)
            out.append({"ticker": st, "category": s.get("category")})
        return out

    # ---- broad sample: a slice of every other category ------------------------------------------------
    @staticmethod
    def _series_volume(s: dict) -> float:
        best = 0.0
        for k, v in s.items():
            if "volume" in k.lower():
                try:
                    best = max(best, float(v or 0))
                except (TypeError, ValueError):
                    pass
        return best

    def _sample_opt(self, category, key: str, default, series=None):
        """A sample setting, with an optional override per category or per series (sample.overrides = {name: {key: value}})."""
        sc = self.cfg.get("sample") or {}
        ovs = sc.get("overrides") or {}
        ov = {**(ovs.get(category or "") or {}), **(ovs.get(series or "") or {})}     # a series entry beats its category's
        v = ov.get(key, sc.get(key))
        return default if v in (None, "") else v

    def sample_series(self, exclude: set) -> list[dict]:
        """The busiest series of every category that is not already collected in full."""
        sc = self.cfg.get("sample") or {}
        if not sc.get("enabled"):
            return []
        per = max(1, int(sc.get("series_per_category") or 12))
        skip = {c.lower() for c in (sc.get("skip_categories") or [])} | {c.lower() for c in (self.cfg.get("categories") or [])}
        cats = sorted((self._retry(self.client.tags_by_category) or {}).get("tags_by_categories") or {})
        out, info = [], {}
        for cat in cats:
            if cat.lower() in skip or "test" in cat.lower() or self._stop.is_set():
                continue
            try:
                ser = self._retry(self.client.series_list, cat) or []
            except Exception as e:
                info[cat] = f"error: {e}"[:120]
                continue
            ser = [s for s in ser if s.get("ticker") and s["ticker"] not in exclude]
            vols = [(self._series_volume(s), s) for s in ser]
            if any(v > 0 for v, _ in vols):
                vols.sort(key=lambda x: -x[0])
                how = "by volume"
            else:                               # no volume figures: an arbitrary but repeatable spread
                import hashlib
                vols.sort(key=lambda x: hashlib.md5(x[1]["ticker"].encode()).hexdigest())
                how = "no volume data, arbitrary pick"
            n = max(1, int(self._sample_opt(cat, "series_per_category", per)))
            for _, s in vols[:n]:
                self.db.upsert_series(s, cat)
                out.append({"ticker": s["ticker"], "category": s.get("category") or cat, "sample_cat": cat})
            info[cat] = f"{min(n, len(vols))} of {len(ser)} series ({how})"
        # series named one by one in the overrides (e.g. the 15-minute contracts) are collected whatever their volume rank
        have = {o["ticker"] for o in out}
        for name in (sc.get("overrides") or {}):
            if name in cats or name in have or name in exclude or self._stop.is_set():
                continue
            try:
                s = self._retry(self.client.series, name)
            except Exception as e:
                info[name] = f"error: {e}"[:120]
                continue
            if not s or not s.get("ticker"):
                info[name] = "not found"
                continue
            self.db.upsert_series(s, s.get("category"))
            out.append({"ticker": s["ticker"], "category": s.get("category"), "sample_cat": s.get("category"), "named": True})
            info[name] = "named series"
        self.db.put("sample_info", info)
        return out

    def _sync_sample_markets(self, s, days: float, per: int) -> int:
        """The most recently settled markets of one sampled series (they have a result; that is what the studies need)."""
        st, cat = s["ticker"], s.get("category")
        since = time.time() - days * 86400
        got = []

        def pull(params):
            for m in self.client.paginate("/markets", "markets", params, max_items=max(3000, per * 3)):
                ts = iso_ts(m.get("settlement_ts")) or iso_ts(m.get("close_time")) or 0
                if ts >= since and (m.get("result") in ("yes", "no")):
                    got.append((ts, m))
        base = {"series_ticker": st, "status": "settled", "limit": 1000}
        try:                                    # ask only for the window (a series of 15-minute contracts has thousands of older ones)
            pull({**base, "min_close_ts": int(since)})
        except Exception:
            got.clear()
            pull(base)
        got.sort(key=lambda x: -x[0])
        for _, m in got[:per]:
            self.db.upsert_market(m, st, cat, "live")
        return min(per, len(got))

    def cutoff(self) -> dict:
        try:
            c = self.client.request("GET", "/historical/cutoff")
            return {k: iso_ts(v) for k, v in c.items()}
        except Exception:
            return {}

    # -------------------------------------------------------------- sync --
    def sync(self) -> dict:
        t0 = time.time()
        stats = {"series": 0, "markets": 0, "trades": 0, "candles": 0, "errors": 0}
        lookback = float(self.cfg.get("lookback_days", 120)) * 86400
        since = time.time() - lookback
        self.state.update(running=True, error=None, phase="series")
        try:
            series = self._retry(self.scope_series)
            stats["series"] = len(series)
            cut = self.cutoff()
            self.db.put("cutoff", cut)
            # 1) markets: live tier (open + settled) and historical tier
            for i, s in enumerate(series):
                if self._stop.is_set():
                    break
                self.state["progress"] = f"markets {i + 1}/{len(series)} {s['ticker']}"
                try:
                    stats["markets"] += self._retry(self._sync_markets, s, since, cut)
                except Exception as e:              # one series failing must not abort the whole sync
                    stats["errors"] += 1
                    self.state["error"] = f"{s['ticker']}: {e}"[:300]
            # 1b) broad sample of the other categories (for the edge studies)
            full = {s["ticker"] for s in series}
            self._full_scope = full
            sc = self.cfg.get("sample") or {}
            if sc.get("enabled"):
                self.state["phase"] = "sample"
                samp = self.sample_series(full)
                stats["sample_series"] = len(samp)
                for i, s in enumerate(samp):
                    if self._stop.is_set():
                        break
                    self.state["progress"] = f"sample markets {i + 1}/{len(samp)} {s['ticker']}"
                    try:
                        stats["markets"] += self._retry(self._sync_sample_markets, s, float(self._sample_opt(s.get("sample_cat"), "days", 45, s["ticker"])),
                                                        int(self._sample_opt(s.get("sample_cat"), "markets_per_series", 60, s["ticker"])))
                    except Exception as e:
                        stats["errors"] += 1
                        self.state["error"] = f"{s['ticker']}: {e}"[:300]
                # markets cut at an earlier, lower trade cap are fetched again when their category's cap has been raised
                for r in self.db.query("SELECT m.ticker, m.category, m.series_ticker, (SELECT COUNT(*) FROM wh_trades t WHERE t.ticker = m.ticker) n "
                                       "FROM wh_markets m WHERE m.complete = 2"):
                    if r["n"] < int(self._sample_opt(r["category"], "max_trades_per_market", 5000, r["series_ticker"])):
                        self.db.execute("UPDATE wh_markets SET complete=0, trades_synced_to=0 WHERE ticker=?", (r["ticker"],))
            # 2) trades + candles for markets that still need them
            todo = self.db.query(
                "SELECT ticker, series_ticker, category, tier, open_ts, close_ts, settled_ts, status, volume, trades_synced_to, "
                "candles_synced_to FROM wh_markets WHERE complete=0 AND COALESCE(close_ts, 0) >= ? "
                "ORDER BY close_ts DESC", (since,))
            self.state["phase"] = "trades+candles"
            for i, m in enumerate(todo):
                if self._stop.is_set():
                    break
                if i % 25 == 0:
                    self.state["progress"] = f"market data {i + 1}/{len(todo)}"
                try:
                    a, b = self._retry(self._sync_market_data, m, cut)
                    stats["trades"] += a
                    stats["candles"] += b
                except Exception as e:
                    stats["errors"] += 1
                    self.state["error"] = f"{m['ticker']}: {e}"
            self.state["last_sync"] = time.time()
            self.state["last_duration"] = round(time.time() - t0, 1)
            self.db.put("last_sync", self.state["last_sync"])
            self.db.put("last_duration", self.state["last_duration"])
            self.db.put("last_stats", stats)
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"
            self.e.db.audit("warehouse_error", traceback.format_exc()[-800:])
        finally:
            self.state.update(running=False, phase="idle", progress="")
        return stats

    RETRY_429 = (5, 15, 45)

    def _retry(self, fn, *a):
        """Kalshi's rate limit is shared with the scanner, the quoter and the paper test: on HTTP 429 wait and try
        again (writes are idempotent upserts) instead of failing the whole sync."""
        for w in self.RETRY_429 + (None,):
            try:
                return fn(*a)
            except KalshiError as e:
                if e.status != 429 or w is None:
                    raise
                if self._stop.wait(w):
                    raise

    def _sync_markets(self, s, since, cut) -> int:
        st, cat = s["ticker"], s.get("category")
        n = 0
        for status in ("open", "closed", "settled"):
            params = {"series_ticker": st, "status": status, "limit": 1000}
            for m in self.client.paginate("/markets", "markets", params, max_items=20000):
                if status == "settled" and (iso_ts(m.get("settlement_ts")) or iso_ts(m.get("close_time")) or 0) < since:
                    continue
                self.db.upsert_market(m, st, cat, "live")
                n += 1
        # historical tier: settled before the cutoff (only needed if the cutoff is inside our lookback)
        if cut.get("market_settled_ts") and cut["market_settled_ts"] > since:
            pages, params = 0, {"series_ticker": st, "limit": 1000}
            while pages < int(self.cfg.get("max_hist_pages", 20)):
                page = self.client.request("GET", "/historical/markets", params)
                ms = page.get("markets") or []
                pages += 1
                fresh = 0
                for m in ms:
                    ts = iso_ts(m.get("settlement_ts")) or iso_ts(m.get("close_time")) or 0
                    if ts >= since:
                        self.db.upsert_market(m, st, cat, "historical")
                        n += 1
                        fresh += 1
                if not page.get("cursor") or not ms or fresh == 0:
                    break
                params["cursor"] = page["cursor"]
        return n

    def _sync_market_data(self, m, cut) -> tuple[int, int]:
        t = m["ticker"]
        now = time.time()
        start = int(m["open_ts"] or ((m["close_ts"] or now) - 7 * 86400))
        end = int(min(now, (m["close_ts"] or now) + 3600))
        nt = nc = 0
        sampled = m["series_ticker"] not in getattr(self, "_full_scope", {m["series_ticker"]})
        cap = int(self._sample_opt(m["category"], "max_trades_per_market", 5000, m["series_ticker"])) if sampled else 200000
        truncated = False
        # trades (skip markets that never traded)
        if (m["volume"] or 0) > 0 or m["volume"] is None:
            since = max(start, int(m["trades_synced_to"] or 0) - 300)   # 5-min overlap; duplicates are ignored
            hist_cut = cut.get("trades_created_ts")
            routes = []
            if hist_cut and since < hist_cut:
                routes.append(("/historical/trades", since, int(min(end, hist_cut))))
            if not hist_cut or end > hist_cut:
                routes.append(("/markets/trades", int(max(since, hist_cut or 0)), end))
            for path, a, b in routes:
                params = {"ticker": t, "min_ts": a, "max_ts": b, "limit": 1000}
                batch, got = [], 0
                for tr in self.client.paginate(path, "trades", params, max_items=cap + 1):
                    got += 1
                    if got > cap:
                        truncated = True         # busier than the sample allows: keep quotes, leave its trades out of trade studies
                        break
                    batch.append(tr)
                    if len(batch) >= 2000:
                        nt += self.db.add_trades(batch)
                        batch = []
                if batch:
                    nt += self.db.add_trades(batch)
            self.db.execute("UPDATE wh_markets SET trades_synced_to=? WHERE ticker=?", (end, t))
        # hourly candles
        period = int(self.cfg.get("candle_period", 60))
        cstart = max(start, int(m["candles_synced_to"] or 0) - period * 60)
        if sampled:                              # the studies look at the last days before the close, not a market's whole life
            cstart = max(cstart, end - int(self._sample_opt(m["category"], "candle_days", 30)) * 86400)
        if cstart < end:
            hist = m["tier"] == "historical" or (cut.get("market_settled_ts") and m["settled_ts"]
                                                  and m["settled_ts"] < cut["market_settled_ts"])
            path = f"/historical/markets/{t}/candlesticks" if hist else f"/series/{m['series_ticker']}/markets/{t}/candlesticks"
            step = 4000 * period * 60            # Kalshi refuses more than 5000 candles per request
            a = cstart
            while a < end:
                b = min(end, a + step)
                r = self.client.request("GET", path, {"start_ts": a, "end_ts": b, "period_interval": period})
                nc += self.db.add_candles(t, period, r.get("candlesticks") or [])
                a = b
            self.db.execute("UPDATE wh_markets SET candles_synced_to=? WHERE ticker=?", (end, t))
        if m["status"] in ("settled", "finalized") or (m["settled_ts"] and m["settled_ts"] < now):
            # complete = 2: settled and stored, but its trade list was cut at the sample cap (trade-level studies skip it)
            self.db.execute("UPDATE wh_markets SET complete=? WHERE ticker=?", (2 if truncated else 1, t))
        return nt, nc

    # -------------------------------------------------------------- loop --
    def status(self) -> dict:
        return {**self.state, "counts": self.db.counts(), "config": self.cfg, "cutoff": self.db.get("cutoff"), "sample_info": self.db.get("sample_info"),
                "last_stats": self.db.get("last_stats")}

    def trigger(self):
        self._now.set()

    def loop(self):
        self._now.wait(60)   # let the app settle after startup
        while not self._stop.is_set():
            if self.cfg.get("enabled", True):
                self.sync()
            self._now.clear()
            self._now.wait(max(10, float(self.cfg.get("sync_interval_min", 60))) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="warehouse").start()

    def stop(self):
        self._stop.set()
        self._now.set()
