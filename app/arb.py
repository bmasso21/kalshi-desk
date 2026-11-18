"""Arbitrage scan: Kalshi priced against itself (read-only public data; places no orders).

Three checks over every open event:
  * NO basket  - an event whose outcomes are mutually exclusive (at most one can be YES): buying NO on every outcome
                 pays at least (n - 1) dollars. If that costs less, after fees, the profit is locked in.
  * YES basket - the same event: buying YES on every outcome pays $1 only if exactly one outcome MUST win. Kalshi's
                 flag says "at most one", not "exactly one", so these are recorded as CANDIDATES needing a check, never
                 as locked profit.
  * Ladder     - "above X" contracts of one event must get cheaper as X rises. If a higher strike's YES bid exceeds a
                 lower strike's YES ask, buy YES low + NO high: every outcome pays at least $1.

Candidates from the market listing are re-checked against live order books (top of book and its size) before
being recorded. Fees use Kalshi's rule (7% x price x (1 - price) x contracts, rounded UP to the cent per order, times
the series' fee multiplier). After the event settles, the real payout is computed from the results, so every
recorded opportunity is checked against what would actually have happened."""
from __future__ import annotations

import json
import math
import threading
import time

from .sports import ensure_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS arb_opps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, kind TEXT, event TEXT, title TEXT, legs TEXT,
    first_seen REAL, last_seen REAL, seen INTEGER DEFAULT 1, sets INTEGER, cost REAL, payout_min REAL, fees REAL,
    profit_per_set REAL, profit_total REAL, locked INTEGER, status TEXT DEFAULT 'open', payout_real REAL, pnl_real REAL,
    settled_ts REAL
);
CREATE TABLE IF NOT EXISTS arb_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, events INTEGER, markets INTEGER, candidates INTEGER, confirmed INTEGER,
    seconds REAL, error TEXT
);
"""
FREE_FEES = ("zero", "free", "no_fee")


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def fee(price: float, count: float, mult: float = 1.0, fee_type: str | None = "quadratic") -> float:
    """Kalshi taker fee for one order: 7% x C x P x (1 - P), rounded up to the cent."""
    if (fee_type or "").lower() in FREE_FEES or price <= 0 or price >= 1:
        return 0.0
    return math.ceil(round(100 * 0.07 * mult * count * price * (1 - price), 6)) / 100


def _quotes(m):
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    return (yb or 0.0), (ya if ya and 0 < ya <= 1 else None)


def candidates(event: dict, fee_info: dict) -> list[dict]:
    """Opportunities in ONE event from the market listing's quotes. Each leg: {ticker, side, price} (price = the ask paid)."""
    ms = [m for m in event.get("markets") or [] if (m.get("status") or "active") in ("active", "open")]
    if len(ms) < 2:
        return []
    mult, ftype = fee_info.get(event.get("series_ticker"), (1.0, "quadratic"))
    out = []
    if event.get("mutually_exclusive"):
        # NO on any k of the outcomes pays at least k - 1 (at most one is YES), so profit = sum of their YES bids - 1 - fees:
        # every outcome whose YES bid is worth more than its fee belongs in the basket.
        legs = []
        for m in ms:
            yb, _ = _quotes(m)
            if yb > 0 and yb > fee(1 - yb, 1, mult, ftype):
                legs.append({"ticker": m["ticker"], "side": "no", "price": round(1 - yb, 4)})
        if len(legs) >= 2:
            cost = sum(l["price"] for l in legs)
            fees = sum(fee(l["price"], 1, mult, ftype) for l in legs)
            payout = len(legs) - 1
            if payout - cost - fees > 0.0001:
                out.append({"kind": "no_basket", "legs": legs, "payout_min": payout, "locked": True})
        if all(_quotes(m)[1] for m in ms):
            legs = [{"ticker": m["ticker"], "side": "yes", "price": _quotes(m)[1]} for m in ms]
            cost = sum(l["price"] for l in legs)
            fees = sum(fee(l["price"], 1, mult, ftype) for l in legs)
            if 1 - cost - fees > 0.0001:
                out.append({"kind": "yes_basket", "legs": legs, "payout_min": 0.0, "locked": False})
    # a ladder is ONE underlying at several strikes: the same ticker apart from its strike part and the same close.
    # (An event can hold strikes for different things, e.g. each player's season average: those are not a ladder.)
    groups = {}
    for m in ms:
        if (m.get("strike_type") or "").lower() in ("greater", "greater_or_equal") and fnum(m.get("floor_strike")) is not None:
            groups.setdefault((m["ticker"].rsplit("-", 1)[0], m.get("close_time")), []).append((fnum(m.get("floor_strike")), m))
    for g in groups.values():
        out += _ladder(sorted(g, key=lambda x: x[0]), mult, ftype)
    return out


def _ladder(lad, mult, ftype) -> list[dict]:
    out = []
    for i in range(len(lad)):
        lo_k, lo = lad[i]
        _, lo_ask = _quotes(lo)
        if not lo_ask:
            continue
        for j in range(i + 1, len(lad)):
            hi_k, hi = lad[j]
            hi_bid, _ = _quotes(hi)
            if hi_k <= lo_k or hi_bid <= 0:
                continue
            legs = [{"ticker": lo["ticker"], "side": "yes", "price": lo_ask}, {"ticker": hi["ticker"], "side": "no", "price": round(1 - hi_bid, 4)}]
            fees = sum(fee(l["price"], 1, mult, ftype) for l in legs)
            if 1 - sum(l["price"] for l in legs) - fees > 0.0001:
                out.append({"kind": "ladder", "legs": legs, "payout_min": 1.0, "locked": True})
    return out


def confirm(cand: dict, books: dict, mult: float = 1.0, ftype: str = "quadratic") -> dict | None:
    """Re-price a candidate at live top of book. books: {ticker: {"yes_asks": [(p, size)], "no_asks": [(p, size)]}}."""
    legs = []
    for l in cand["legs"]:
        b = books.get(l["ticker"]) or {}
        lvl = (b.get("yes_asks") if l["side"] == "yes" else b.get("no_asks")) or []
        if not lvl:
            return None
        p, size = float(lvl[0][0]), float(lvl[0][1])
        if size <= 0 or p <= 0 or p >= 1:
            return None
        legs.append({**l, "price": round(p, 4), "size": size})
    sets = int(min(l["size"] for l in legs))
    if sets < 1:
        return None
    cost = sum(l["price"] for l in legs)
    fees_1 = sum(fee(l["price"], 1, mult, ftype) for l in legs)
    fees_n = sum(fee(l["price"], sets, mult, ftype) for l in legs)
    per = cand["payout_min"] - cost - fees_1
    total = sets * (cand["payout_min"] - cost) - fees_n
    if cand["locked"] and per <= 0.0001 and total <= 0.0001:
        return None
    if not cand["locked"] and 1 - cost - fees_1 <= 0.0001:
        return None
    return {**cand, "legs": legs, "sets": sets, "cost": round(cost, 4), "fees": round(fees_n, 2),
            "profit_per_set": round(per if cand["locked"] else 1 - cost - fees_1, 4),
            "profit_total": round(total if cand["locked"] else sets * (1 - cost) - fees_n, 2)}


class ArbScanner:
    def __init__(self, cfg_getter, wdb, kalshi_client):
        self._cfg = cfg_getter
        self.db = wdb
        self.k = kalshi_client            # public production market data only: no order methods are used
        ensure_schema(wdb, SCHEMA)
        self._stop = threading.Event()
        self._fees = (0.0, {})
        self.state = {"running": False, "last_scan": None, "error": None, "note": ""}
        for r in wdb.query("SELECT id, legs FROM arb_opps WHERE kind='ladder'"):     # 3.21.0 paired different underlyings: drop those
            lg = json.loads(r["legs"])
            if len({l["ticker"].rsplit("-", 1)[0] for l in lg}) > 1:
                wdb.execute("DELETE FROM arb_opps WHERE id=?", (r["id"],))

    def opt(self, k, default):
        v = (self._cfg() or {}).get(k)
        return default if v in (None, "") else v

    def fee_info(self) -> dict:
        if time.time() - self._fees[0] > 6 * 3600 or not self._fees[1]:
            info = {s.get("ticker"): (fnum(s.get("fee_multiplier")) or 1.0, s.get("fee_type") or "quadratic")
                    for s in self.k.paginate("/series", "series", {}, max_items=50000)}
            self._fees = (time.time(), info)
        return self._fees[1]

    def scan(self) -> dict:
        t0 = time.time()
        self.state.update(running=True, error=None)
        n_ev = n_mk = n_c = n_ok = 0
        err = None
        try:
            fi = self.fee_info()
            cands = []
            for ev in self.k.paginate("/events", "events", {"status": "open", "with_nested_markets": "true", "limit": 200}, max_items=100000):
                n_ev += 1
                n_mk += len(ev.get("markets") or [])
                for c in candidates(ev, fi):
                    cands.append((ev, c))
            n_c = len(cands)
            # locked kinds first, best listed profit first; YES baskets (never locked) get only a few checks
            cands.sort(key=lambda x: (not x[1]["locked"], -(x[1]["payout_min"] - sum(l["price"] for l in x[1]["legs"]))))
            locked = [x for x in cands if x[1]["locked"]][: int(self.opt("arb_max_confirm", 150))]
            loose = [x for x in cands if not x[1]["locked"]][: int(self.opt("arb_max_yes_baskets", 15))]
            seen = set()
            for ev, c in locked + loose:
                time.sleep(float(self.opt("arb_pause_sec", 0.5)))           # gentle on Kalshi: the collector shares the limit
                tickers = [l["ticker"] for l in c["legs"]]
                try:
                    from .kalshi_client import book_from_orderbook
                    obs = self.k.orderbooks(tickers)
                    books = {t: book_from_orderbook(ob) for t, ob in obs.items()}
                except Exception:
                    continue
                mult, ftype = fi.get(ev.get("series_ticker"), (1.0, "quadratic"))
                ok = confirm(c, books, mult, ftype)
                if not ok:
                    continue
                n_ok += 1
                key = f"{ok['kind']}|{ev.get('event_ticker')}|" + ",".join(f"{l['ticker']}:{l['side']}" for l in ok["legs"])
                seen.add(key)
                now = time.time()
                old = self.db.query("SELECT id FROM arb_opps WHERE key=?", (key,))
                if old:
                    self.db.execute("UPDATE arb_opps SET last_seen=?, seen=seen+1, legs=?, sets=?, cost=?, fees=?, profit_per_set=?, profit_total=?, status='open' WHERE id=?",
                                    (now, json.dumps(ok["legs"]), ok["sets"], ok["cost"], ok["fees"], ok["profit_per_set"], ok["profit_total"], old[0]["id"]))
                else:
                    self.db.execute("INSERT INTO arb_opps(key, kind, event, title, legs, first_seen, last_seen, sets, cost, payout_min, fees, profit_per_set, profit_total, locked) "
                                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (key, ok["kind"], ev.get("event_ticker"), (ev.get("title") or "")[:200], json.dumps(ok["legs"]), now, now, ok["sets"], ok["cost"],
                                     ok["payout_min"], ok["fees"], ok["profit_per_set"], ok["profit_total"], 1 if ok["locked"] else 0))
            for r in self.db.query("SELECT id, key FROM arb_opps WHERE status='open'"):
                if r["key"] not in seen:
                    self.db.execute("UPDATE arb_opps SET status='gone' WHERE id=?", (r["id"],))
            self.settle()
        except Exception as e:
            err = str(e)[:300]
        secs = round(time.time() - t0, 1)
        self.db.execute("INSERT INTO arb_scans(ts, events, markets, candidates, confirmed, seconds, error) VALUES(?,?,?,?,?,?,?)",
                        (t0, n_ev, n_mk, n_c, n_ok, secs, err))
        self.state.update(running=False, last_scan=t0, error=err,
                          note=f"{n_ev:,} events, {n_mk:,} markets: {n_c} candidates from listed prices, {n_ok} still there at live order-book prices")
        return {"events": n_ev, "markets": n_mk, "candidates": n_c, "confirmed": n_ok, "seconds": secs, "error": err}

    def settle(self):
        """For opportunities whose markets have all settled: what would one set really have paid?"""
        rows = self.db.query("SELECT id, legs, cost FROM arb_opps WHERE status IN ('open', 'gone') AND settled_ts IS NULL")
        for r in rows[:200]:
            legs = json.loads(r["legs"])
            try:
                ms = {m["ticker"]: m for m in self.k.markets(status=None, tickers=[l["ticker"] for l in legs], max_items=100)}
            except Exception:
                continue
            res = [(ms.get(l["ticker"]) or {}).get("result") for l in legs]
            if not all(x in ("yes", "no") for x in res):
                continue
            payout = sum(1.0 for l, x in zip(legs, res) if l["side"] == x)
            fees1 = sum(fee(l["price"], 1) for l in legs)
            self.db.execute("UPDATE arb_opps SET status='settled', payout_real=?, pnl_real=?, settled_ts=? WHERE id=?",
                            (payout, round(payout - r["cost"] - fees1, 4), time.time(), r["id"]))

    def loop(self):
        self._stop.wait(300)
        while not self._stop.is_set():
            if self.opt("arb_enabled", True):
                try:
                    self.scan()
                except Exception as e:
                    self.state["error"] = str(e)[:300]
            self._stop.wait(float(self.opt("arb_every_min", 60)) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="arb").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {"state": self.state, "every_min": float(self.opt("arb_every_min", 60)),
                "open": self.db.query("SELECT * FROM arb_opps WHERE status='open' AND locked=1 ORDER BY profit_total DESC LIMIT 50"),
                "open_unlocked": self.db.query("SELECT * FROM arb_opps WHERE status='open' AND locked=0 ORDER BY profit_total DESC LIMIT 30"),
                "recent": self.db.query("SELECT * FROM arb_opps ORDER BY last_seen DESC LIMIT 50"),
                "scans": self.db.query("SELECT * FROM arb_scans ORDER BY id DESC LIMIT 12"),
                "totals": self.db.query("SELECT kind, COUNT(*) n, SUM(status='settled') settled, SUM(pnl_real) pnl_real, SUM(profit_per_set) locked_per_set "
                                        "FROM arb_opps GROUP BY kind")}
