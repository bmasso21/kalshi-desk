"""Combo price requests (RFQs), read-only log: is answering them worth it?

Anyone on Kalshi can ask market makers for a price on a combo (several legs that must all win). Kalshi broadcasts
the request to every maker; a maker who quotes has seconds to confirm. Before quoting with real money, this logs the
requests on PRODUCTION for a while (owner's approval, 24 Nov 2026) to answer:
  - how many requests, how big, on which combos;
  - what the requester is asking to pay per contract, against the legs' own prices on Kalshi at that moment
    (product of the legs' mid prices: assumes the legs are independent, which same-game legs are not);
  - once every leg has settled, whether the combo won. Over many requests: do requesters overpay (their combos hit
    less often than the price they ask implies)? If so, the side a quoter takes - selling the combo - earns the gap.

The production key is used ONLY to sign GET /communications/rfqs (the list of requests). RfqReader refuses every other
path and every non-GET before a request is built. Leg prices and results come from public market data.
Places no orders, sends no quotes."""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time

from .kalshi_client import KalshiClient
from .sports import ensure_schema

READ_PATH = "/communications/rfqs"
EVERY = 60
PAGE = 100
MAX_LIST = 3000                   # the list holds every OPEN request (over 1,000 at a time); it is read in full each minute
SAMPLE_PCT = 1                    # a fixed 1% sample, chosen by id, is stored (10% grew by 280,000 rows in a day)
MAX_LEGS = 12                     # legs are priced and followed only for combos this size or smaller

SCHEMA = """
CREATE TABLE IF NOT EXISTS rfq_log (id TEXT PRIMARY KEY, created_ts REAL, seen_ts REAL, market TEXT, collection TEXT, contracts REAL,
    target_cost REAL, excludes_fees INTEGER, n_legs INTEGER, legs TEXT, legs_mid REAL, legs_priced INTEGER, status TEXT,
    closed_ts REAL, reason TEXT, outcome TEXT, settled_ts REAL);
CREATE INDEX IF NOT EXISTS rfq_log_open ON rfq_log(outcome, created_ts);
CREATE TABLE IF NOT EXISTS rfq_rate (ts REAL PRIMARY KEY, listed INTEGER, oldest REAL, newest REAL, legs_hist TEXT);
"""


class RfqReader(KalshiClient):
    """Production client that can only read the list of RFQs."""
    def request(self, method: str, path: str, params=None, body=None, auth: bool = False, retries: int = 4) -> dict:
        if method.upper() != "GET" or path.split("?")[0] != READ_PATH or body is not None:
            raise PermissionError(f"RFQ log is read-only: {method} {path} is not allowed")
        return super().request("GET", path, params=params, body=None, auth=True, retries=retries)


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(s):
    """ISO time -> epoch seconds. Kalshi's RFQ times can carry nanoseconds ('...:59.123456789Z'), which Python's parser
    rejects: the fraction is cut to microseconds first."""
    import re
    if not s:
        return None
    t = re.sub(r"(\.\d{6})\d+", r"\1", str(s).strip()).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def sampled(rid: str) -> bool:
    import hashlib
    return int(hashlib.sha1(str(rid).encode()).hexdigest()[:8], 16) % 100 < SAMPLE_PCT


def mid(m: dict):
    b, a = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    if b is None or a is None or a <= 0 or a < b:
        return None
    return (b + a) / 2


def legs_price(legs: list, quotes: dict):
    """Product over legs of the chosen side's mid price; None if any leg has no usable quote."""
    p = 1.0
    for lg in legs:
        q = quotes.get(lg.get("market_ticker"))
        m = mid(q) if q else None
        if m is None:
            return None
        p *= m if (lg.get("side") or "yes").lower() == "yes" else 1 - m
    return p


def outcome(legs: list, results: dict):
    """'won' when every leg settled on its chosen side, 'lost' when any leg settled against it, None while undecided."""
    pending = False
    for lg in legs:
        r = results.get(lg.get("market_ticker"))
        if r not in ("yes", "no"):
            pending = True
            continue
        if r != (lg.get("side") or "yes").lower():
            return "lost"
    return None if pending else "won"


class RfqLog:
    def __init__(self, cfg_getter, wdb, public_client, reader_factory=None):
        self._cfg = cfg_getter                 # the whole config (prod key id/path, rfq section)
        self.db = wdb
        self.k = public_client                 # public production market data
        self._reader = None
        self._reader_factory = reader_factory
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self._prev = None                       # (time, ids) of the previous full listing
        self.state = {"running": False, "last": None, "error": None, "note": "", "seen_last_scan": 0}

    def opt(self, k, default):
        v = ((self._cfg() or {}).get("rfq") or {}).get(k)
        return default if v in (None, "") else v

    def reader(self):
        if self._reader is None:
            if self._reader_factory:
                self._reader = self._reader_factory()
            else:
                from .config import BASE_URLS
                c = self._cfg() or {}
                kid, kp = c.get("prod_key_id"), c.get("prod_private_key_path")
                if not kid or not kp:
                    raise RuntimeError("no production API key is set up in Settings > Connection")
                self._reader = RfqReader(c.get("base_url_override") or BASE_URLS["prod"], kid, kp)
        return self._reader

    def scan(self, now: float | None = None) -> dict:
        now = now or time.time()
        new, closed = 0, 0
        known = {}
        rows = list(self.reader().paginate(READ_PATH, "rfqs", {"limit": PAGE}, max_items=MAX_LIST))
        listed = len(rows)
        # arrival rate: requests not in the previous full listing (the first listing after a start is the backlog)
        ids_now = {r.get("id") for r in rows if r.get("id")}
        arrivals, hist, rate = None, {}, None
        if self._prev is not None:
            arr = [r for r in rows if r.get("id") not in self._prev[1]]
            arrivals = len(arr)
            for r in arr:
                n = len(r.get("mve_selected_legs") or [])
                b = "0" if n == 0 else "2-3" if n <= 3 else "4-6" if n <= 6 else "7-12" if n <= 12 else "13+"
                hist[b] = hist.get(b, 0) + 1
            if now > self._prev[0]:
                rate = round(arrivals / (now - self._prev[0]) * 3600)
                self.db.execute("INSERT OR REPLACE INTO rfq_rate VALUES(?,?,?,?,?)", (now, arrivals, self._prev[0], now, json.dumps(hist)))
        self._prev = (now, ids_now)
        ages = sorted(now - c for c in (_ts(r.get("created_ts")) for r in rows) if c)
        self.state["open_age_s"] = {"median": round(ages[len(ages) // 2]) if ages else None, "max": round(ages[-1]) if ages else None,
                                    "unparsed": listed - len(ages)}
        rows = [r for r in rows if sampled(r.get("id"))]
        if rows:
            ids = [r.get("id") for r in rows if r.get("id")]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in self.db.query(f"SELECT id, status FROM rfq_log WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                    known[r["id"]] = r["status"]
        fresh = []
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            st = (r.get("status") or "").lower()
            if rid in known:
                if st == "closed" and known[rid] != "closed":
                    self.db.execute("UPDATE rfq_log SET status='closed', closed_ts=?, reason=? WHERE id=?",
                                    (_ts(r.get("cancelled_ts")) or _ts(r.get("updated_ts")) or now, r.get("cancellation_reason"), rid))
                    closed += 1
                continue
            fresh.append(r)
        # leg prices at the moment the request is seen (public data, one call per 100 leg markets)
        tickers = sorted({lg.get("market_ticker") for r in fresh if len(r.get("mve_selected_legs") or []) <= MAX_LEGS
                          for lg in (r.get("mve_selected_legs") or []) if lg.get("market_ticker")})
        quotes = {}
        for i in range(0, len(tickers), 100):
            for m in self.k.markets(status=None, tickers=tickers[i:i + 100], max_items=100):
                quotes[m.get("ticker")] = m
        for r in fresh:
            legs = [{"market_ticker": lg.get("market_ticker"), "event_ticker": lg.get("event_ticker"), "side": (lg.get("side") or "yes").lower()}
                    for lg in (r.get("mve_selected_legs") or [])]
            lp = legs_price(legs, quotes) if legs and len(legs) <= MAX_LEGS else None
            cur = self.db.execute("INSERT OR IGNORE INTO rfq_log(id, created_ts, seen_ts, market, collection, contracts, target_cost, excludes_fees, "
                                  "n_legs, legs, legs_mid, legs_priced, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (r["id"], _ts(r.get("created_ts")), now, r.get("market_ticker"), r.get("mve_collection_ticker"),
                                   fnum(r.get("contracts_fp")), fnum(r.get("target_cost_dollars")), 1 if r.get("target_cost_excludes_fees") else 0,
                                   len(legs), json.dumps(legs) if len(legs) <= MAX_LEGS else "[]", lp, 1 if lp is not None else 0, (r.get("status") or "open").lower()))
            new += 1 if cur is not None and cur.rowcount and cur.rowcount > 0 else 0
        settled = self.settle(now)
        self.state.update(last=now, error=None, seen_last_scan=listed, per_hour_est=rate,
                          note=f"{listed} open requests listed{'' if arrivals is None else f', {arrivals} new since the last read (about {rate} an hour)'}"
                               f"{' (list capped: some missed)' if listed >= MAX_LIST else ''}; {new} sampled and stored, {closed} newly closed, {settled} combos decided")
        return {"listed": len(rows), "new": new, "closed": closed, "settled": settled}

    def settle(self, now: float, batch: int = 300) -> int:
        """Combos whose legs have all settled (or one leg lost): won / lost, from the legs' public results."""
        rows = self.db.query("SELECT id, legs FROM rfq_log WHERE outcome IS NULL AND n_legs > 0 AND n_legs <= ? AND COALESCE(created_ts, seen_ts) < ? ORDER BY seen_ts LIMIT ?",
                             (MAX_LEGS, now - 3600, batch))
        if not rows:
            return 0
        legs = {r["id"]: json.loads(r["legs"] or "[]") for r in rows}
        tickers = sorted({lg["market_ticker"] for ls in legs.values() for lg in ls if lg.get("market_ticker")})
        res = {}
        for i in range(0, len(tickers), 100):
            for m in self.k.markets(status=None, tickers=tickers[i:i + 100], max_items=100):
                res[m.get("ticker")] = (m.get("result") or "").lower()
        n = 0
        for rid, ls in legs.items():
            o = outcome(ls, res)
            if o:
                self.db.execute("UPDATE rfq_log SET outcome=?, settled_ts=? WHERE id=?", (o, now, rid))
                n += 1
        return n

    def prune(self) -> int:
        """Keep only the sampled requests and combos small enough to follow (the first version stored every request)."""
        ids = [r["id"] for r in self.db.query("SELECT id FROM rfq_log")]
        drop = [i for i in ids if not sampled(i)]
        for i in range(0, len(drop), 500):
            ch = drop[i:i + 500]
            self.db.execute(f"DELETE FROM rfq_log WHERE id IN ({','.join('?' * len(ch))})", tuple(ch))
        self.db.execute("UPDATE rfq_log SET legs='[]' WHERE n_legs > ?", (MAX_LEGS,))
        return len(drop)

    def loop(self):
        try:
            self.prune()
        except Exception as e:
            self.state["error"] = f"prune: {e}"[:300]
        self._stop.wait(480)
        while not self._stop.is_set():
            if self.opt("enabled", True):
                try:
                    self.state["running"] = True
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
                finally:
                    self.state["running"] = False
            self._stop.wait(float(self.opt("every_sec", EVERY)))

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="rfqlog").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        q = self.db.query
        tot = q("SELECT COUNT(*) n, MIN(created_ts) a, MAX(created_ts) b, SUM(contracts) c, SUM(target_cost) tc, "
                "SUM(CASE WHEN n_legs > 0 THEN 1 ELSE 0 END) combos FROM rfq_log")[0]
        span_d = max(1 / 24, ((tot["b"] or 0) - (tot["a"] or 0)) / 86400) if tot["n"] else None
        sizes = sorted(r["c"] for r in q("SELECT contracts c FROM rfq_log WHERE contracts IS NOT NULL"))
        costs = sorted(r["t"] for r in q("SELECT target_cost t FROM rfq_log WHERE target_cost IS NOT NULL"))
        med = lambda xs: xs[len(xs) // 2] if xs else None

        # decided combos: did the requester's combo hit as often as the price they asked for implies?
        dec = q("SELECT COALESCE(created_ts, seen_ts) created_ts, contracts, target_cost, legs_mid, outcome FROM rfq_log WHERE outcome IN ('won','lost')")

        def block(rs, price_of):
            rs = [r for r in rs if price_of(r) is not None and 0 < price_of(r) < 1]
            if not rs:
                return {"n": 0}
            days = {}
            for r in rs:
                d = days.setdefault(int(r["created_ts"] // 86400), [0, 0.0])
                d[0] += 1
                d[1] += (1.0 if r["outcome"] == "won" else 0.0) - price_of(r)     # requester's result per contract, before fees
            n = len(rs)
            mean = sum(v[1] for v in days.values()) / n
            k = len(days)
            var = sum((v[1] - mean * v[0]) ** 2 for v in days.values())
            se = math.sqrt(var * k / (k - 1)) / n if k > 1 else None
            return {"n": n, "days": k, "hit_pct": round(100 * sum(1 for r in rs if r["outcome"] == "won") / n, 2),
                    "avg_price_c": round(100 * sum(price_of(r) for r in rs) / n, 2),
                    "requester_edge_c": round(100 * mean, 2), "t": round(mean / se, 2) if se else None}
        per_ct = lambda r: (r["target_cost"] / r["contracts"]) if r["target_cost"] and r["contracts"] else None
        rt = q("SELECT listed, oldest, newest, legs_hist FROM rfq_rate WHERE newest > oldest ORDER BY ts DESC LIMIT 1440")
        rates = sorted(r["listed"] / (r["newest"] - r["oldest"]) * 3600 for r in rt)
        lh = {}
        for r in rt:
            for b, n in json.loads(r["legs_hist"] or "{}").items():
                lh[b] = lh.get(b, 0) + n
        return {"state": self.state, "enabled": bool(self.opt("enabled", True)), "sample_pct": SAMPLE_PCT, "max_legs": MAX_LEGS,
                "rate": {"per_hour_median": round(rates[len(rates) // 2]) if rates else None, "reads": len(rt),
                         "legs_share": {b: round(100 * n / sum(lh.values()), 1) for b, n in sorted(lh.items())} if lh else {}},
                "totals": {"requests": tot["n"], "combos": tot["combos"], "per_day": round(tot["n"] / span_d, 1) if span_d else None,
                           "median_contracts": med(sizes), "median_target_cost": med(costs),
                           "since": tot["a"], "decided": len(dec)},
                "by_collection": q("SELECT collection, COUNT(*) n, ROUND(AVG(n_legs), 1) legs, ROUND(SUM(target_cost), 2) dollars FROM rfq_log "
                                   "GROUP BY collection ORDER BY n DESC LIMIT 15"),
                "by_legs": q("SELECT n_legs legs, COUNT(*) n FROM rfq_log GROUP BY n_legs ORDER BY n_legs"),
                "requested_price": block(dec, per_ct),
                "legs_price": block(dec, lambda r: r["legs_mid"]),
                "recent": q("SELECT id, created_ts, market, collection, contracts, target_cost, n_legs, legs_mid, status, outcome FROM rfq_log "
                            "ORDER BY created_ts DESC LIMIT 60")}
